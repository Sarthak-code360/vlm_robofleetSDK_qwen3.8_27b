"""The factory brain: user requests -> machine jobs -> robot tasks.

    request "print the actuator housing"
      -> model picks machine + file             (interpret, checked in code)
      -> job waits for a person to confirm      (if require_confirmation)
      -> machine runs, progress is watched      (tick)
      -> job done -> "collect the part" task    (tick)
      -> model picks the robot                  (dispatch: Coordinator + checker, rules as fallback)
      -> robot gets the task on its heartbeat   (heartbeat)
      -> robot reports done                     (complete_task)

Robots talk to the factory only through ``heartbeat`` and ``complete_task`` (see server.py / robot_agent.py).
The model never moves anything itself: everything it proposes is checked in code first.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from ..coordinator import Coordinator
from ..model import ModelClient, ModelError
from .machines import Machine, MachineError

ROBOT_STATUSES = {"idle", "busy", "charging", "fault"}
NONE = "none"


class Factory:
    """Holds the live state of one factory and runs its decisions.

    Args:
        config: Factory description (zones, chargers, rules, machines, robots); see examples/actuator_factory.
        machines: Machine drivers by id (see ``machines.build_machine``).
        model: The central model. ``None`` = keyword matching for requests and rules for dispatch.
        log_path: Optional JSONL file for every event.
    """

    def __init__(self, config: dict, machines: dict[str, Machine], model: ModelClient | None = None,
                 log_path: str | Path | None = None):
        self.config = config
        self.machines = machines
        self.model = model
        self.log_path = Path(log_path) if log_path else None
        self.rules = config["rules"]
        self.site = {"type": config.get("type", "factory"), "zones": config["zones"],
                     "chargers": config["chargers"], "rules": self.rules}
        self.robot_timeout_s = float(config.get("robot_timeout_s", 15))
        self.require_confirmation = bool(config.get("require_confirmation", True))
        self.default_output_zone = config.get("default_output_zone")
        self.dispatch_with_model = config.get("dispatch", "model") == "model"

        self.robots: dict[str, dict] = {}
        for r in config.get("robots", []):
            self._register_robot(r["id"], r.get("name"), r.get("home_zone"))
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.outbox: dict[str, list[dict]] = {}
        self._last_message: dict[str, tuple] = {}
        self.events: list[dict] = []
        self.lock = threading.RLock()
        self._counter = 0
        self._dispatching = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    def _event(self, kind: str, text: str, **data) -> None:
        event = {"ts": round(time.time(), 3), "kind": kind, "text": text, **data}
        self.events.append(event)
        del self.events[:-200]
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(event, default=str) + "\n")

    def _register_robot(self, robot_id: str, name: str | None = None, zone: str | None = None) -> dict:
        robot = {"id": robot_id, "name": name or robot_id, "zone": zone if zone in self.site["zones"] else None,
                 "battery": 0, "status": "offline", "last_seen": None, "info": {}}
        self.robots[robot_id] = robot
        return robot

    def _active_tasks(self, robot_id: str) -> list[str]:
        return [t["id"] for t in self.tasks.values()
                if t["robot_id"] == robot_id and t["status"] in {"assigned", "in_progress"}]

    # ------------------------------------------------------------------ robots

    def heartbeat(self, robot_id: str, data: dict) -> dict:
        """A robot reports its state; the reply carries its next task and any instructions."""
        with self.lock:
            robot = self.robots.get(robot_id) or self._register_robot(robot_id, data.get("name"))
            was_offline = robot["status"] == "offline"
            if data.get("zone") in self.site["zones"]:
                robot["zone"] = data["zone"]
            if isinstance(data.get("battery"), (int, float)):
                robot["battery"] = max(0, min(100, round(data["battery"])))
            status = data.get("status", "idle")
            robot["status"] = status if status in ROBOT_STATUSES else "idle"
            robot["info"] = {k: v for k, v in data.items() if k not in {"zone", "battery", "status", "name"}}
            robot["last_seen"] = time.time()
            if was_offline:
                self._event("robot", f"{robot_id} connected at {robot['zone']} ({robot['battery']}%)", robot_id=robot_id)

            task = next((t for t in self.tasks.values() if t["robot_id"] == robot_id and t["status"] == "assigned"), None)
            if task:
                task["status"], task["started"] = "in_progress", time.time()
                self._event("task", f"{robot_id} picked up {task['id']}: {task['instruction']}",
                            task_id=task["id"], robot_id=robot_id)
            return {"task": dict(task) if task else None, "messages": self.outbox.pop(robot_id, [])}

    def complete_task(self, task_id: str, robot_id: str, ok: bool = True, note: str = "") -> dict:
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None or task["robot_id"] != robot_id:
                raise KeyError(f"{task_id} is not assigned to {robot_id}")
            task.update(status="done" if ok else "failed", finished=time.time(), note=note)
            self._event("task", f"{robot_id} {'finished' if ok else 'FAILED'} {task_id}" + (f": {note}" if note else ""),
                        task_id=task_id, robot_id=robot_id)
            job = self.jobs.get(task.get("job_id") or "")
            if job:
                job["status"] = "delivered" if ok else "delivery_failed"
            return dict(task)

    # ------------------------------------------------------------------ requests

    def submit_request(self, text: str) -> dict:
        """Turns a plain-English request into a machine job or a robot task. Returns what was created."""
        with self.lock:
            machines = [m.describe() | {"files": m.list_files()} for m in self.machines.values()]
        choice, stats = self._interpret(text, machines)
        with self.lock:
            result = self._apply_choice(text, choice, machines)
            result["model"] = stats
            self._event("request", f"Request {text!r} -> {result['summary']}", result=result)
        if result.get("job") and not self.require_confirmation:
            self.confirm_job(result["job"]["id"])
        return result

    def _interpret(self, text: str, machines: list[dict]) -> tuple[dict, dict]:
        files = sorted({f for m in machines for f in m["files"]})
        zones = list(self.site["zones"])
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["action", "machine_id", "file", "from_zone", "to_zone", "reason"],
            "properties": {
                "action": {"type": "string", "enum": ["run_machine", "move_item", NONE]},
                "machine_id": {"type": "string", "enum": [m["id"] for m in machines] + [NONE]},
                "file": {"type": "string", "enum": files + [NONE]},
                "from_zone": {"type": "string", "enum": zones + [NONE]},
                "to_zone": {"type": "string", "enum": zones + [NONE]},
                "reason": {"type": "string"},
            },
        }
        briefing = {
            "factory": self.config.get("name"), "type": self.site["type"], "zones": zones,
            "default_output_zone": self.default_output_zone,
            "machines": [{k: m.get(k) for k in ("id", "type", "zone", "state", "files")} for m in machines],
        }
        messages = [
            {"role": "system", "content": (
                "You are the central brain of an autonomous factory. Turn the user's request into ONE action:\n"
                "- run_machine: start a job on a machine. Pick an idle machine and one of ITS files. "
                "to_zone = where a robot should bring the finished part (default_output_zone if not stated).\n"
                "- move_item: a robot carries something from from_zone to to_zone.\n"
                f"- {NONE}: the request cannot be done with these machines and zones.\n"
                f"Use \"{NONE}\" for fields that do not apply. Keep the reason under 20 words. JSON only.")},
            {"role": "user", "content": "Factory:\n" + json.dumps(briefing, separators=(",", ":")) + f"\n\nRequest: {text}"},
        ]
        if self.model is not None:
            try:
                return self.model.chat_json(messages, schema, "factory_action", timeout=300)
            except ModelError as e:
                self._event("model", f"Model failed, using keyword matching: {e}")
        return _keyword_choice(text, machines, self.default_output_zone), {"model": "keywords", "latency_s": 0}

    def _apply_choice(self, text: str, choice: dict, machines: list[dict]) -> dict:
        """Checks the model's choice in code and creates the job or task."""
        by_id = {m["id"]: m for m in machines}
        action, reason = choice.get("action"), choice.get("reason", "")
        if action == "run_machine":
            machine = by_id.get(choice.get("machine_id"))
            to_zone = choice.get("to_zone") if choice.get("to_zone") in self.site["zones"] else self.default_output_zone
            if machine is None:
                return {"ok": False, "summary": f"rejected: unknown machine {choice.get('machine_id')!r}", "choice": choice}
            if choice.get("file") not in machine["files"]:
                return {"ok": False, "summary": f"rejected: {choice.get('file')!r} is not a file on {machine['id']}",
                        "choice": choice}
            if machine["state"] != "idle":
                return {"ok": False, "summary": f"rejected: {machine['id']} is {machine['state']}", "choice": choice}
            if any(j["machine_id"] == machine["id"] and j["status"] in {"awaiting_confirmation", "starting", "running"}
                   for j in self.jobs.values()):
                return {"ok": False, "summary": f"rejected: {machine['id']} already has a job", "choice": choice}
            job = {"id": self._next_id("JOB"), "request": text, "machine_id": machine["id"], "file": choice["file"],
                   "deliver_to": to_zone, "reason": reason, "created": time.time(), "task_id": None,
                   "status": "awaiting_confirmation" if self.require_confirmation else "starting"}
            self.jobs[job["id"]] = job
            return {"ok": True, "summary": f"{job['id']}: {machine['id']} will run {job['file']}, then deliver to {to_zone}",
                    "job": dict(job), "choice": choice}
        if action == "move_item":
            src, dst = choice.get("from_zone"), choice.get("to_zone")
            if src not in self.site["zones"] or dst not in self.site["zones"] or src == dst:
                return {"ok": False, "summary": f"rejected: bad zones {src!r} -> {dst!r}", "choice": choice}
            task = self._new_task(src, dst, f"Carry the item requested ({text}) from {src} to {dst}.")
            return {"ok": True, "summary": f"{task['id']}: move item {src} -> {dst}", "task": dict(task), "choice": choice}
        return {"ok": False, "summary": f"nothing to do ({reason or 'no matching machine or zone'})", "choice": choice}

    def confirm_job(self, job_id: str) -> dict:
        """A person confirmed the machine is safe to start (e.g. the print bed is clear)."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] not in {"awaiting_confirmation", "starting"}:
                return dict(job)
            job["status"] = "starting"
        threading.Thread(target=self._start_job, args=(job_id,), daemon=True).start()
        return dict(job)

    def _start_job(self, job_id: str) -> None:
        job = self.jobs[job_id]
        machine = self.machines[job["machine_id"]]
        try:
            machine.start(job["file"])
        except Exception as e:  # upload/connection errors from real machines
            with self.lock:
                job.update(status="failed", error=str(e))
                self._event("job", f"{job_id} could not start on {machine.id}: {e}", job_id=job_id)
            return
        with self.lock:
            job.update(status="running", started=time.time())
            self._event("job", f"{job_id} started: {machine.id} running {job['file']}", job_id=job_id)

    # ------------------------------------------------------------------ the loop

    def tick(self) -> None:
        """One pass of the control loop: robot timeouts, machine progress, then dispatch."""
        now = time.time()
        with self.lock:
            for robot in self.robots.values():
                if robot["status"] != "offline" and (robot["last_seen"] is None or now - robot["last_seen"] > self.robot_timeout_s):
                    robot["status"] = "offline"
                    self._event("robot", f"{robot['id']} went offline", robot_id=robot["id"])
                    for task in self.tasks.values():
                        if task["robot_id"] == robot["id"] and task["status"] in {"assigned", "in_progress"}:
                            task.update(status="pending", robot_id=None)
                            self._event("task", f"{task['id']} back in the queue ({robot['id']} offline)", task_id=task["id"])
            running = [j for j in self.jobs.values() if j["status"] == "running"]

        for job in running:
            result = self.machines[job["machine_id"]].poll()
            if result == "running":
                continue
            with self.lock:
                if result == "done":
                    machine = self.machines[job["machine_id"]]
                    task = self._new_task(machine.zone, job["deliver_to"],
                                          f"Collect the finished part '{job['file']}' from {machine.id} at {machine.zone} "
                                          f"and place it in {job['deliver_to']}.", job_id=job["id"])
                    job.update(status="done", finished=time.time(), task_id=task["id"])
                    self._event("job", f"{job['id']} finished on {machine.id}; created {task['id']} to collect it",
                                job_id=job["id"], task_id=task["id"])
                else:
                    job.update(status="failed", finished=time.time())
                    self._event("job", f"{job['id']} FAILED on {job['machine_id']}", job_id=job["id"])
        self.dispatch()

    def _new_task(self, from_zone: str, to_zone: str, instruction: str, job_id: str | None = None) -> dict:
        task = {"id": self._next_id("TASK"), "type": "pick_and_place", "from_zone": from_zone, "to_zone": to_zone,
                "instruction": instruction, "priority": "normal", "battery_cost": self.rules.get("task_battery_cost", 5),
                "job_id": job_id, "robot_id": None, "status": "pending", "created": time.time()}
        self.tasks[task["id"]] = task
        return task

    def fleet_snapshot(self) -> dict:
        """What the dispatcher sees: robots with a known zone, and tasks waiting for a robot."""
        with self.lock:
            robots = [{"id": r["id"], "zone": r["zone"], "battery": r["battery"], "status": r["status"],
                       "queued_tasks": self._active_tasks(r["id"])}
                      for r in self.robots.values() if r["zone"] in self.site["zones"]]
            pending = [{k: t[k] for k in ("id", "type", "from_zone", "to_zone", "priority", "battery_cost")}
                       for t in self.tasks.values() if t["status"] == "pending"]
        return {"robots": robots, "pending_tasks": pending, "alerts": []}

    def dispatch(self) -> None:
        """Assigns waiting tasks to robots. The model proposes, the checker verifies, rules are the fallback."""
        if not self._dispatching.acquire(blocking=False):
            return
        try:
            fleet = self.fleet_snapshot()
            if not fleet["pending_tasks"] or not any(r["status"] not in {"offline", "fault", "charging"} for r in fleet["robots"]):
                return
            model = self.model if self.dispatch_with_model else None
            decision = Coordinator(self.site, model).plan(fleet)
            if decision.error:
                self._event("model", f"Model failed, rule-based dispatch used: {decision.error}")
            with self.lock:
                for a in decision.plan["assignments"]:
                    task, robot = self.tasks.get(a["task_id"]), self.robots.get(a["robot_id"])
                    # State may have changed while the model was thinking: check again.
                    if not task or task["status"] != "pending" or not robot or robot["status"] in {"offline", "fault", "charging"}:
                        continue
                    if len(self._active_tasks(robot["id"])) >= self.rules["max_queued_tasks_per_robot"]:
                        continue
                    task.update(status="assigned", robot_id=robot["id"], assigned=time.time(), reason=a.get("reason", ""))
                    self._event("dispatch", f"{task['id']} -> {robot['id']} ({decision.source}: {a.get('reason', '')})",
                                task_id=task["id"], robot_id=robot["id"])
                for m in decision.plan["robot_messages"]:
                    key, last = (m["instruction"], m.get("target_zone")), self._last_message.get(m["robot_id"])
                    if last and last[0] == key and time.time() - last[1] < 60:
                        continue  # same instruction sent less than a minute ago
                    self._last_message[m["robot_id"]] = (key, time.time())
                    self.outbox.setdefault(m["robot_id"], []).append(m)
                    self._event("dispatch", f"{m['robot_id']}: {m['instruction']} {m.get('target_zone') or ''}".strip(),
                                robot_id=m["robot_id"])
                for r in decision.rejected:
                    self._event("checker", f"Rejected from model plan: {r}")
        finally:
            self._dispatching.release()

    def run_forever(self, interval_s: float = 2.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep the factory running; the error is visible in the event log
                self._event("error", f"tick failed: {e!r}")
            stop.wait(interval_s)

    # ------------------------------------------------------------------ view

    def state(self) -> dict:
        machines = [m.describe() | {"files": m.list_files()} for m in self.machines.values()]
        with self.lock:
            now = time.time()
            robots = [dict(r, active_tasks=self._active_tasks(r["id"]),
                           seen_s_ago=None if r["last_seen"] is None else round(now - r["last_seen"], 1))
                      for r in self.robots.values()]
            return {
                "factory": {k: self.config.get(k) for k in ("name", "type", "default_output_zone")}
                | {"require_confirmation": self.require_confirmation, "model": getattr(self.model, "model", None)},
                "zones": self.site["zones"], "chargers": self.site["chargers"],
                "machines": machines, "robots": robots,
                "jobs": sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True),
                "tasks": sorted(self.tasks.values(), key=lambda t: t["created"], reverse=True),
                "events": list(reversed(self.events[-50:])),
            }


def _keyword_choice(text: str, machines: list[dict], default_zone: str | None) -> dict:
    """No-model fallback: run the first idle machine whose file name appears in the request."""
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    for m in machines:
        if m["state"] != "idle":
            continue
        for f in m["files"]:
            stem = re.findall(r"[a-z0-9]+", f.lower().replace(".gcode.3mf", ""))
            if stem and set(stem) <= words:
                return {"action": "run_machine", "machine_id": m["id"], "file": f, "from_zone": NONE,
                        "to_zone": default_zone or NONE, "reason": "file name matches the request"}
    return {"action": NONE, "machine_id": NONE, "file": NONE, "from_zone": NONE, "to_zone": NONE,
            "reason": "no file name matched (no model connected)"}
