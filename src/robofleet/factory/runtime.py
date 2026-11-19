"""The factory brain: user requests -> machine jobs -> robot tasks.

    request "print the rocket nose cone"
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

import ipaddress
import json
import os
import re
import threading
import time
from pathlib import Path

from ..coordinator import Coordinator
from ..model import ModelClient, ModelError
from .assistant import DEFAULT_ASSISTANT, AssistantMixin
from .catalog import MACHINE_CATALOG, PREFIX, accepted_extensions, machine_label
from .machines import (DRIVERS, MACHINE_TYPES, Machine, MachineError, SimulatedPrinter, SimulatedStation, build_machine,
                       is_live, machine_stage)

ROBOT_STATUSES = {"idle", "busy", "charging", "fault"}
ROBOT_KINDS = {"mobile_manipulator", "robotic_arm", "amr", "humanoid", "other"}
CONNECTIONS = {"simulated", "agent"}
PRIORITIES = {"normal", "urgent"}
ACTIVE_JOB = {"awaiting_confirmation", "starting", "running"}
HOLDING_JOB = ACTIVE_JOB | {"done"}  # "done" = the part is still on the machine, waiting for a robot
RUN_ACTIVE = {"running", "paused"}
MAX_CONSECUTIVE_FAILURES = 3
NONE = "none"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
NAME_RE = re.compile(r"^[\w .,()/+#-]{1,60}$")
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
HISTORY_S = 3600


class Factory(AssistantMixin):
    """Holds the live state of one factory and runs its decisions.

    Args:
        config: Factory description (zones, chargers, rules, machines, robots); see examples/rocket_factory.
        machines: Machine drivers by id (see ``machines.build_machine``).
        model: The central model. ``None`` = keyword matching for requests and rules for dispatch.
        log_path: Optional JSONL file for every event.
        data_dir: Where robots, machines and zones added from the dashboard, and uploaded documents, are kept.
            ``None`` = nothing is saved and documents cannot be uploaded.
        base_dir: Folder that relative paths of dashboard-added machines (e.g. ``prints``) are resolved against.
        simulate_machines: Build dashboard-added real machines as simulated ones.
    """

    def __init__(self, config: dict, machines: dict[str, Machine], model: ModelClient | None = None,
                 log_path: str | Path | None = None, data_dir: str | Path | None = None,
                 base_dir: str | Path | None = None, simulate_machines: bool = False):
        self.config = config
        self.machines = machines
        self.model = model
        self.log_path = Path(log_path) if log_path else None
        self.data_dir = Path(data_dir) if data_dir else None
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.simulate_machines = simulate_machines
        self.rules = config["rules"]
        self.site = {"type": config.get("type", "factory"), "zones": config["zones"],
                     "chargers": config["chargers"], "rules": self.rules}
        self.robot_timeout_s = float(config.get("robot_timeout_s", 15))
        self.require_confirmation = bool(config.get("require_confirmation", True))
        self.default_output_zone = config.get("default_output_zone")
        self.dispatch_with_model = config.get("dispatch", "model") == "model"
        self.assistant_name = str(config.get("assistant_name") or DEFAULT_ASSISTANT)
        instructions = config.get("assistant_instructions") or []
        self.assistant_instructions = [str(i)[:500] for i in ([instructions] if isinstance(instructions, str) else instructions)][:30]
        self.products: dict[str, dict] = {str(k): {"parts": dict(v.get("parts", {})), "description": str(v.get("description", ""))}
                                          for k, v in (config.get("products") or {}).items()}
        self.chat: list[dict] = []
        self._chat_seq = 0
        self._alerted: dict[str, float] = {}
        self.robot_wait_alert_s = float(config.get("robot_wait_alert_s", 90))
        self.showcase = dict(config.get("showcase") or {})  # 3D model on the Overview: model (path), title, caption

        self.robots: dict[str, dict] = {}
        for r in config.get("robots", []):
            self._register_robot(r, source="file")
        self.machine_source = {mid: "file" for mid in machines}
        for m in machines.values():
            self._attach_upload_dir(m)
        self.machine_history: dict[str, list[list]] = {}
        self.documents: dict[str, dict] = {}
        self.registry = {"zones": {}, "machines": [], "robots": []}
        self._agents: dict[str, threading.Event] = {}
        self.sim_speed_m_s, self.sim_handling_s, self.sim_interval_s = 1.0, 2.0, 2.0  # built-in simulated robots
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.outbox: dict[str, list[dict]] = {}
        self._last_message: dict[str, tuple] = {}
        self.events: list[dict] = []
        self.lock = threading.RLock()
        self._counter = 0
        self._dispatching = threading.Lock()
        self._run_stepping = threading.Lock()
        if self.data_dir:
            self._load_registry()

    # ------------------------------------------------------------------ helpers

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    def _event(self, kind: str, text: str, **data) -> None:
        event = {"ts": round(time.time(), 3), "kind": kind, "text": text, **data}
        self.events.append(event)
        del self.events[:-200]
        if kind in {"job", "task", "dispatch", "run", "alert"}:
            self._chat_follow(kind, text, data)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(event, default=str) + "\n")

    def _register_robot(self, spec: dict, source: str) -> dict:
        zone = spec.get("home_zone") or spec.get("zone")
        robot = {"id": spec["id"], "name": spec.get("name") or spec["id"], "kind": spec.get("kind", "other"),
                 "model": spec.get("model", ""), "home_zone": zone if zone in self.site["zones"] else None,
                 "payload_kg": spec.get("payload_kg"), "capabilities": list(spec.get("capabilities") or []),
                 "connection": spec.get("connection", "agent"), "source": source,
                 "zone": zone if zone in self.site["zones"] else None,
                 "battery": 0, "status": "offline", "last_seen": None, "info": {}}
        self.robots[robot["id"]] = robot
        return robot

    def _active_tasks(self, robot_id: str) -> list[str]:
        return [t["id"] for t in self.tasks.values()
                if t["robot_id"] == robot_id and t["status"] in {"assigned", "in_progress"}]

    # ------------------------------------------------------------------ robots

    def heartbeat(self, robot_id: str, data: dict) -> dict:
        """A robot reports its state; the reply carries its next task and any instructions."""
        with self.lock:
            robot = self.robots.get(robot_id) or self._register_robot(
                {"id": robot_id, "name": data.get("name")}, source="heartbeat")
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

    def submit_request(self, text: str, document_id: str | None = None, deliver_to: str | None = None,
                       priority: str = "normal") -> dict:
        """Turns a plain-English request into a machine job or a robot task. Returns what was created.

        Args:
            text: What the person wants, e.g. "print the rocket nose cone".
            document_id: An uploaded reference PDF (work instruction) handed to the robot that does the work.
            deliver_to: Zone for the finished part. ``None`` = the model decides (default output zone).
            priority: ``normal`` or ``urgent``.
        """
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {', '.join(sorted(PRIORITIES))}")
        if deliver_to and deliver_to not in self.site["zones"]:
            raise ValueError(f"unknown zone {deliver_to!r}")
        document = None
        if document_id:
            if document_id not in self.documents:
                raise KeyError(document_id)
            document = {k: self.documents[document_id][k] for k in ("id", "name")}
        with self.lock:
            machines = [m.describe() | {"files": m.list_files()} for m in self.machines.values()]
        note = f"(Attached work instruction: {document['name']})" if document else ""
        choice, stats = self._interpret(text, machines, note)
        if deliver_to:
            choice = dict(choice, to_zone=deliver_to)
        with self.lock:
            result = self._apply_choice(text, choice, machines, document=document, priority=priority)
            result["model"] = stats
            self._event("request", f"Request {text!r} -> {result['summary']}", result=result)
        if result.get("job") and not self.require_confirmation:
            self.confirm_job(result["job"]["id"])
        return result

    def _interpret(self, text: str, machines: list[dict], note: str = "") -> tuple[dict, dict]:
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
            {"role": "user", "content": "Factory:\n" + json.dumps(briefing, separators=(",", ":")) + f"\n\nRequest: {text}" + (f"\n{note}" if note else "")},
        ]
        if self.model is not None:
            try:
                return self.model.chat_json(messages, schema, "factory_action", timeout=300)
            except ModelError as e:
                self._event("model", f"Model failed, using keyword matching: {e}")
        return _keyword_choice(text, machines, self.default_output_zone), {"model": "keywords", "latency_s": 0}

    def _apply_choice(self, text: str, choice: dict, machines: list[dict], document: dict | None = None,
                      priority: str = "normal") -> dict:
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
            if any(j["machine_id"] == machine["id"] and j["status"] in ACTIVE_JOB for j in self.jobs.values()):
                return {"ok": False, "summary": f"rejected: {machine['id']} already has a job", "choice": choice}
            if any(j["machine_id"] == machine["id"] and j["status"] == "done" for j in self.jobs.values()):
                return {"ok": False, "summary": f"rejected: {machine['id']} still holds the last part, waiting for a robot",
                        "choice": choice}
            job = self._new_job(text, machine["id"], choice["file"], to_zone, reason, document, priority,
                                self.require_confirmation)
            return {"ok": True, "summary": f"{job['id']}: {machine['id']} will run {job['file']}, then deliver to {to_zone}",
                    "job": dict(job), "choice": choice}
        if action == "move_item":
            src, dst = choice.get("from_zone"), choice.get("to_zone")
            if src not in self.site["zones"] or dst not in self.site["zones"] or src == dst:
                return {"ok": False, "summary": f"rejected: bad zones {src!r} -> {dst!r}", "choice": choice}
            task = self._new_task(src, dst, f"Carry the item requested ({text}) from {src} to {dst}.",
                                  document=document, priority=priority)
            return {"ok": True, "summary": f"{task['id']}: move item {src} -> {dst}", "task": dict(task), "choice": choice}
        return {"ok": False, "summary": f"nothing to do ({reason or 'no matching machine or zone'})", "choice": choice}

    def _new_job(self, request: str, machine_id: str, file: str, deliver_to: str, reason: str = "",
                 document: dict | None = None, priority: str = "normal", needs_approval: bool = True,
                 run_id: str | None = None) -> dict:
        job = {"id": self._next_id("JOB"), "request": request, "machine_id": machine_id, "file": file,
               "deliver_to": deliver_to, "reason": reason, "created": time.time(), "task_id": None,
               "document": document, "priority": priority, "run_id": run_id,
               "status": "awaiting_confirmation" if needs_approval else "starting"}
        self.jobs[job["id"]] = job
        return job

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

    def cancel_job(self, job_id: str) -> dict:
        """Cancels a job that has not started yet."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] != "awaiting_confirmation":
                raise ValueError(f"{job_id} is {job['status']}; only jobs awaiting approval can be cancelled")
            job["status"] = "cancelled"
            self._event("job", f"{job_id} cancelled", job_id=job_id)
            return dict(job)

    def _start_job(self, job_id: str) -> None:
        job = self.jobs[job_id]
        machine = self.machines[job["machine_id"]]
        try:
            machine.start(job["file"])
        except Exception as e:  # upload/connection errors from real machines
            with self.lock:
                job.update(status="failed", error=str(e), finished=time.time())
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
                                          f"and place it in {job['deliver_to']}.", job_id=job["id"],
                                          document=job.get("document"), priority=job.get("priority", "normal"))
                    task.update(origin=job.get("origin"), preferred_robot=job.get("preferred_robot"))
                    job.update(status="done", finished=time.time(), task_id=task["id"])
                    self._event("job", f"{job['id']} finished on {machine.id}; created {task['id']} to collect it",
                                job_id=job["id"], task_id=task["id"])
                else:
                    job.update(status="failed", finished=time.time())
                    self._event("job", f"{job['id']} FAILED on {job['machine_id']}", job_id=job["id"])
        self._run_step()
        self.dispatch()

    def _new_task(self, from_zone: str, to_zone: str, instruction: str, job_id: str | None = None,
                  document: dict | None = None, priority: str = "normal") -> dict:
        if document:
            instruction += f" Follow the work instruction '{document['name']}'."
            document = dict(document, url=f"/api/documents/{document['id']}")
        task = {"id": self._next_id("TASK"), "type": "pick_and_place", "from_zone": from_zone, "to_zone": to_zone,
                "instruction": instruction, "priority": priority, "battery_cost": self.rules.get("task_battery_cost", 5),
                "job_id": job_id, "document": document, "robot_id": None, "status": "pending", "created": time.time()}
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
            self._dispatch_preferred()
            self._alert_waiting_tasks()
            fleet = self.fleet_snapshot()
            with self.lock:
                chosen = {t["id"] for t in self.tasks.values() if t.get("preferred_robot")}
            fleet["pending_tasks"] = [t for t in fleet["pending_tasks"] if t["id"] not in chosen]
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
                    if (self.robots.get(m["robot_id"]) or {}).get("status") == "offline":
                        continue  # it cannot hear us; it gets fresh instructions when it reconnects
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

    def _dispatch_preferred(self) -> None:
        """Tasks where the person chose the robot go to that robot as soon as it can take them."""
        with self.lock:
            reserve = self.rules.get("battery_reserve_pct", 20)
            for task in self.tasks.values():
                rid = task.get("preferred_robot")
                if task["status"] != "pending" or not rid:
                    continue
                robot = self.robots.get(rid)
                if robot is None:
                    task["preferred_robot"] = None  # removed meanwhile: let the dispatcher choose
                    continue
                if robot["status"] != "idle" or self._active_tasks(rid) or robot["battery"] - task["battery_cost"] < reserve:
                    self._alert(f"wait:{task['id']}:{rid}", f"{task['id']} is waiting for {rid}, which is "
                                f"{robot['status']} ({robot['battery']}% battery).", repeat_s=300)
                    continue
                task.update(status="assigned", robot_id=rid, assigned=time.time(), reason="chosen by the operator")
                self._event("dispatch", f"{task['id']} -> {rid} (chosen by the operator)", task_id=task["id"], robot_id=rid)

    def _alert_waiting_tasks(self) -> None:
        """Warns when finished parts cannot be collected: no robots, or a task waiting too long."""
        with self.lock:
            pending = [t for t in self.tasks.values() if t["status"] == "pending"]
            if not pending:
                return
            available = [r for r in self.robots.values() if r["status"] not in {"offline", "fault", "charging"}]
            if not available:
                self._alert("no-robots", f"No robot is free to collect {len(pending)} finished part(s). "
                            "Add a robot in Settings → Robots, or connect one.",
                            repeat_s=120)
                return
            now = time.time()
            for t in pending:
                if now - t["created"] > self.robot_wait_alert_s:
                    self._alert(f"slow:{t['id']}", f"A finished part has waited {int(now - t['created'])} s for a robot. "
                                "All robots are busy or low on battery. Consider adding a robot.", repeat_s=600)

    def _alert(self, key: str, text: str, repeat_s: float = 120) -> None:
        now = time.time()
        if now - self._alerted.get(key, 0) >= repeat_s:
            self._alerted[key] = now
            self._event("alert", text)

    def run_forever(self, interval_s: float = 2.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep the factory running; the error is visible in the event log
                self._event("error", f"tick failed: {e!r}")
            stop.wait(interval_s)

    # ------------------------------------------------------------------ production runs

    def start_run(self, spec: dict) -> dict:
        """Starts a production run: make ``target`` units, each needing ``parts`` ({job file: quantity}).

        The factory keeps every free machine busy with the parts still needed, robots deliver each finished part,
        and the run completes when enough parts for ``target`` units are delivered.

        spec: name, target, parts ({file: qty}; empty = the model picks from name/description), description,
              document_id (process PDF for the robots), deliver_to, approve_each_job (a person approves every
              machine start; default: only when a real machine is involved).
        """
        name = str(spec.get("name") or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("product name: 1-60 letters, digits, spaces or .,()/+#-")
        try:
            target = int(spec.get("target") or 0)
        except (TypeError, ValueError):
            raise ValueError("target must be a whole number") from None
        if not 1 <= target <= 100_000:
            raise ValueError("target must be between 1 and 100000")
        deliver_to = spec.get("deliver_to") or self.default_output_zone
        self._check_zone(deliver_to)
        document = None
        if spec.get("document_id"):
            if spec["document_id"] not in self.documents:
                raise KeyError(spec["document_id"])
            document = {k: self.documents[spec["document_id"]][k] for k in ("id", "name")}

        steps = self._run_steps(spec.get("steps")) if spec.get("steps") else None
        with self.lock:
            files_by_machine = {m.id: set(m.list_files()) for m in self.machines.values()}
            if steps:  # only the chosen machines take part, each with its own job
                files_by_machine = {s["machine_id"]: {s["job"]} for s in steps}
        available = set().union(*files_by_machine.values()) if files_by_machine else set()
        parts = {s["job"]: 1 for s in steps} if steps else (spec.get("parts") or {})
        if not isinstance(parts, dict):
            raise ValueError("parts must be an object {job file: quantity per unit}")
        if not parts:
            text = " ".join(str(spec.get(k) or "") for k in ("name", "description")).strip()
            product = self.match_product(text)
            if product:
                name, parts = product, dict(self.products[product]["parts"])
            else:
                parts = self._plan_parts(text, sorted(available), document)
        clean = {}
        for file, qty in parts.items():
            if file not in available:
                raise ValueError(f"no machine can make {file!r}")
            try:
                qty = int(qty)
            except (TypeError, ValueError):
                raise ValueError(f"quantity for {file} must be a whole number") from None
            if not 1 <= qty <= 100:
                raise ValueError(f"quantity for {file} must be between 1 and 100")
            clean[file] = qty
        if not clean:
            raise ValueError("choose at least one part")
        machines_used = sorted(mid for mid, files in files_by_machine.items() if files & set(clean))
        real = any(not isinstance(self.machines[mid], (SimulatedPrinter, SimulatedStation)) for mid in machines_used)
        # build order: each part's stage, and where its finished pieces go (the next stage's station, or deliver_to)
        stages = {f: min(machine_stage(self.machines[mid]) for mid in machines_used if f in files_by_machine[mid])
                  for f in clean}
        route = {}
        for st in sorted(set(stages.values())):
            later = sorted(x for x in set(stages.values()) if x > st)
            if later:
                nxt = sorted(mid for mid in machines_used
                             if machine_stage(self.machines[mid]) == later[0] and files_by_machine[mid] & set(clean))
                route[str(st)] = self.machines[nxt[0]].zone
            else:
                route[str(st)] = deliver_to
        approve = bool(spec["approve_each_job"]) if "approve_each_job" in spec else real

        with self.lock:
            if any(r["status"] in RUN_ACTIVE for r in self.runs.values()):
                raise ValueError("a production run is already active; stop it first")
            run = {"id": self._next_id("RUN"), "name": name, "description": str(spec.get("description") or "")[:500],
                   "target": target, "parts": clean, "document": document, "deliver_to": deliver_to,
                   "approve_each_job": approve, "machines": machines_used, "status": "running",
                   "stages": stages, "route": route, "steps": steps or [],
                   "created": time.time(), "finished": None, "reason": ""}
            self.runs[run["id"]] = run
            self._event("run", f"Production run {run['id']} started: {target} × {name} "
                               f"({', '.join(f'{q}× {f}' for f, q in clean.items())} per unit)", run_id=run["id"])
        self.tick_soon()
        return self.run_view(run)

    def _run_steps(self, raw) -> list[dict]:
        """Validates the machines chosen for a run and their inputs. Each step = one machine and its job per unit."""
        if not isinstance(raw, list) or not raw or len(raw) > 30:
            raise ValueError("choose between 1 and 30 machines")
        steps, jobs = [], set()
        for item in raw:
            mtype = (item or {}).get("type")
            mid = (item or {}).get("machine_id") or self.ensure_machine(mtype)["id"]
            machine = self.machines.get(mid)
            if machine is None:
                raise ValueError(f"unknown machine {mid}")
            catalog = MACHINE_CATALOG.get(machine.type, {"label": machine.type, "inputs": []})
            inputs = {k: v for k, v in dict(item.get("inputs") or {}).items() if v not in (None, "")}
            for field in catalog["inputs"]:
                if field.get("required") and field["key"] not in inputs:
                    raise ValueError(f"{machine.spec.get('name') or mid}: {field['label']} is required")
                if field["kind"] == "number" and field["key"] in inputs:
                    try:
                        inputs[field["key"]] = float(inputs[field["key"]])
                    except (TypeError, ValueError):
                        raise ValueError(f"{field['label']} must be a number") from None
                if field["kind"] == "select" and field["key"] in inputs and inputs[field["key"]] not in field["options"]:
                    raise ValueError(f"{field['label']}: choose one of {', '.join(field['options'])}")
            job_field = next((f for f in catalog["inputs"] if f.get("job") and f["key"] in inputs), None)
            job = str(inputs[job_field["key"]]) if job_field else f"{machine.type}_job"
            if job_field and job_field["kind"] == "file" and job not in machine.list_files():
                raise ValueError(f"{job} is not on {machine.spec.get('name') or mid}; upload it first")
            if not is_live(machine):
                job = re.sub(r"[^\w .()+-]", "_", job)[:100]
                if job not in machine.spec.setdefault("files", []):
                    machine.spec["files"].append(job)  # the simulated machine learns this job
            base, n = job, 2
            while job in jobs:  # two machines with the same job name: keep them apart
                job = f"{base} ({n})"; n += 1
                if not is_live(machine):
                    machine.spec["files"].append(job)
            jobs.add(job)
            steps.append({"machine_id": mid, "type": machine.type, "label": machine_label(machine.type),
                          "live": is_live(machine), "job": job, "inputs": inputs})
        return steps

    def match_product(self, text: str) -> str | None:
        """The catalog product a request is about ("make 2 rockets" -> "Sounding Rocket"), if exactly one fits."""
        words = {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", str(text).lower())}
        full, head = [], []
        for name in self.products:
            key = [w.rstrip("s") for w in re.findall(r"[a-z0-9]+", name.lower())]
            if key and set(key) <= words:
                full.append(name)
            elif key and key[-1] in words:
                head.append(name)
        matches = full or head
        return matches[0] if len(matches) == 1 else None

    def _plan_parts(self, text: str, files: list[str], document: dict | None) -> dict:
        """The model picks which job files make one unit of the product; keyword matching as fallback."""
        if not files:
            raise ValueError("no machine has job files")
        if self.model is not None and text:
            schema = {"type": "object", "additionalProperties": False, "required": ["parts"],
                      "properties": {"parts": {"type": "array", "items": {
                          "type": "object", "additionalProperties": False, "required": ["file", "quantity"],
                          "properties": {"file": {"type": "string", "enum": files},
                                         "quantity": {"type": "integer", "minimum": 1, "maximum": 20}}}}}}
            messages = [
                {"role": "system", "content": "You plan production in an autonomous factory. Choose which job files "
                 "(and how many of each) are needed to make ONE unit of the product. Use only the listed files. JSON only."},
                {"role": "user", "content": f"Product: {text}\nJob files: {json.dumps(files)}"
                 + (f"\nProcess document: {document['name']}" if document else "")}]
            try:
                answer, _ = self.model.chat_json(messages, schema, "production_parts", timeout=300)
                parts = {}
                for p in answer.get("parts", []):
                    if p.get("file") in files:
                        parts[p["file"]] = parts.get(p["file"], 0) + int(p.get("quantity") or 1)
                if parts:
                    return parts
            except (ModelError, TypeError, ValueError) as e:
                self._event("model", f"Model could not plan the parts, using keyword matching: {e}")
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        parts = {}
        for f in files:
            stem = set(re.findall(r"[a-z0-9]+", f.lower().replace(".gcode.3mf", ""))) - {"gcode", "3mf"}
            if stem and stem & words - {"the", "a", "an", "and", "of"} and (stem <= words or len(stem & words) >= 2):
                parts[f] = 1
        if not parts:
            raise ValueError("could not tell which parts make this product; select them")
        return parts

    def set_run_status(self, run_id: str, action: str) -> dict:
        """pause, resume or stop a run. Jobs already on machines finish and are delivered."""
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            allowed = {"pause": ({"running"}, "paused"), "resume": ({"paused"}, "running"),
                       "stop": (RUN_ACTIVE, "stopped")}
            if action not in allowed:
                raise ValueError("action must be pause, resume or stop")
            sources, new = allowed[action]
            if run["status"] not in sources:
                raise ValueError(f"{run_id} is {run['status']}")
            run["status"], run["reason"] = new, ""
            if new == "stopped":
                run["finished"] = time.time()
                for job in self.jobs.values():  # jobs nobody approved yet will not start
                    if job.get("run_id") == run_id and job["status"] == "awaiting_confirmation":
                        job["status"] = "cancelled"
            self._event("run", f"Production run {run_id} {new}", run_id=run_id)
            return self.run_view(run)

    def run_view(self, run: dict) -> dict:
        """A run with its live counts: parts delivered, in production, units produced, progress."""
        jobs = [j for j in self.jobs.values() if j.get("run_id") == run["id"]]
        delivered = {f: 0 for f in run["parts"]}
        in_flight = {f: 0 for f in run["parts"]}
        for j in jobs:
            if j["status"] == "delivered":
                delivered[j["file"]] += 1
            elif j["status"] in HOLDING_JOB:
                in_flight[j["file"]] += 1
        units = min(delivered[f] // q for f, q in run["parts"].items())
        needed = {f: run["target"] * q for f, q in run["parts"].items()}
        parts_done = sum(min(delivered[f], needed[f]) for f in needed)
        stages = run.get("stages") or {}
        order = sorted(run["parts"], key=lambda f: (stages.get(f, 1), f))
        return dict(run, units=min(units, run["target"]), progress_pct=round(100 * parts_done / sum(needed.values()), 1),
                    part_status=[{"file": f, "per_unit": run["parts"][f], "delivered": delivered[f], "stage": stages.get(f, 1),
                                  "in_production": in_flight[f], "needed": needed[f]} for f in order],
                    jobs_total=len(jobs), failures=sum(j["status"] in {"failed", "delivery_failed"} for j in jobs))

    def tick_soon(self) -> None:
        """Starts the first jobs of a run without waiting for the next control-loop tick."""
        threading.Thread(target=self._run_step, daemon=True).start()

    def _run_step(self) -> None:
        """Keeps free machines busy with the parts the active run still needs."""
        if not self._run_stepping.acquire(blocking=False):
            return
        try:
            self._run_step_locked()
        finally:
            self._run_stepping.release()

    def _run_step_locked(self) -> None:
        with self.lock:
            run = next((r for r in self.runs.values() if r["status"] == "running"), None)
            if run is None:
                return
            view = self.run_view(run)
            if view["units"] >= run["target"]:
                run.update(status="completed", finished=time.time())
                self._event("run", f"Production run {run['id']} completed: {run['target']} × {run['name']}", run_id=run["id"])
                return
            finished = sorted((j for j in self.jobs.values() if j.get("run_id") == run["id"] and j.get("finished")),
                              key=lambda j: j["finished"])
            streak = 0
            for j in reversed(finished):
                if j["status"] not in {"failed", "delivery_failed"}:
                    break
                streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                run.update(status="paused", reason=f"{streak} jobs in a row failed; check the machines and robots")
                self._event("run", f"Production run {run['id']} paused: {run['reason']}", run_id=run["id"])
                return
            # a later-stage part (assembly, paint, inspection) of unit k starts only when every earlier-stage part
            # of unit k has been delivered
            done = {p["file"]: p["delivered"] for p in view["part_status"]}
            stages = run.get("stages") or {}

            def units_ready(stage: int) -> int:
                earlier = [done[f] // q for f, q in run["parts"].items() if stages.get(f, 1) < stage]
                return min(earlier) if earlier else run["target"]

            remaining = {p["file"]: min(p["needed"], units_ready(p["stage"]) * p["per_unit"]) - p["delivered"] - p["in_production"]
                         for p in view["part_status"]}
            busy = {j["machine_id"] for j in self.jobs.values() if j["status"] in HOLDING_JOB}
            candidates = [m for mid, m in sorted(self.machines.items()) if mid not in busy and mid in run["machines"]]
        started = []
        for machine in candidates:
            if machine.state() != "idle":
                continue
            with self.lock:
                if run["status"] != "running" or machine.id in {j["machine_id"] for j in self.jobs.values()
                                                                  if j["status"] in HOLDING_JOB}:
                    continue
                files = [f for f in machine.list_files() if remaining.get(f, 0) > 0]
                if not files:
                    continue
                file = max(files, key=lambda f: remaining[f])
                remaining[file] -= 1
                to_zone = (run.get("route") or {}).get(str(stages.get(file, 1)), run["deliver_to"])
                job = self._new_job(f"{run['name']} ({run['id']})", machine.id, file, to_zone,
                                    f"production run {run['id']}", run["document"], "normal",
                                    run["approve_each_job"], run_id=run["id"])
                self._event("job", f"{job['id']} queued for {run['id']}: {machine.id} → {file}"
                                   + (" (awaiting approval)" if run["approve_each_job"] else ""), job_id=job["id"])
                if not run["approve_each_job"]:
                    started.append(job["id"])
        for job_id in started:
            self.confirm_job(job_id)

    # ------------------------------------------------------------------ registry (dashboard)

    def _free_ids(self, prefix: str, count: int, taken) -> list[str]:
        numbers = [int(m.group(1)) for i in taken if (m := re.fullmatch(rf"{re.escape(prefix)}-(\d+)", i))]
        start = max(numbers, default=0) + 1
        return [f"{prefix}-{n:03d}" for n in range(start, start + count)]

    def _check_zone(self, zone) -> str:
        if zone not in self.site["zones"]:
            raise ValueError(f"unknown zone {zone!r}")
        return zone

    def add_zone(self, name: str, x: float, y: float) -> dict:
        """Adds a named place on the floor (x/y in metres)."""
        name = str(name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("zone name: 1-60 letters, digits, spaces or .,()/+#-")
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            raise ValueError("x and y must be numbers (metres)") from None
        if not (-10_000 <= x <= 10_000 and -10_000 <= y <= 10_000):
            raise ValueError("x and y must be within ±10000 m")
        with self.lock:
            if name in self.site["zones"]:
                raise ValueError(f"zone {name!r} already exists")
            self.site["zones"][name] = {"x": x, "y": y}
            self.registry["zones"][name] = {"x": x, "y": y}
            self._save_registry()
            self._event("config", f"Zone '{name}' added at ({x:g}, {y:g})")
            return {"name": name, "x": x, "y": y}

    def add_robots(self, spec: dict) -> list[dict]:
        """Registers one robot, or ``count`` robots with generated IDs.

        spec: id (optional when count > 1), name, kind, model, home_zone, payload_kg, capabilities,
              connection ("simulated" = built-in virtual robot, "agent" = robot runs ``robofleet robot run``),
              battery (starting charge of simulated robots), count.
        """
        try:
            count = int(spec.get("count") or 1)
        except (TypeError, ValueError):
            raise ValueError("count must be a whole number") from None
        if not 1 <= count <= 50:
            raise ValueError("count must be between 1 and 50")
        kind = spec.get("kind") or "mobile_manipulator"
        if kind not in ROBOT_KINDS:
            raise ValueError(f"kind must be one of {', '.join(sorted(ROBOT_KINDS))}")
        connection = spec.get("connection") or "simulated"
        if connection not in CONNECTIONS:
            raise ValueError(f"connection must be one of {', '.join(sorted(CONNECTIONS))}")
        zone = self._check_zone(spec.get("home_zone"))
        name = str(spec.get("name") or "").strip()
        if name and not NAME_RE.match(name):
            raise ValueError("name: up to 60 letters, digits, spaces or .,()/+#-")
        model = str(spec.get("model") or "").strip()[:60]
        payload = spec.get("payload_kg")
        if payload not in (None, ""):
            try:
                payload = float(payload)
            except (TypeError, ValueError):
                raise ValueError("payload_kg must be a number") from None
            if not 0 <= payload <= 10_000:
                raise ValueError("payload_kg must be between 0 and 10000")
        else:
            payload = None
        caps = spec.get("capabilities") or ["pick_and_place"]
        if isinstance(caps, str):
            caps = [c.strip() for c in caps.split(",")]
        caps = [c[:40] for c in caps if c][:12]
        try:
            battery = max(0.0, min(100.0, float(spec.get("battery", 100))))
        except (TypeError, ValueError):
            raise ValueError("battery must be a number") from None

        with self.lock:
            if count == 1 and spec.get("id"):
                ids = [str(spec["id"]).strip()]
            else:
                ids = self._free_ids("RBT", count, self.robots)
            for rid in ids:
                if not ID_RE.match(rid):
                    raise ValueError("robot ID: letters, digits, '.', '_' or '-' (max 40)")
                if rid in self.robots:
                    raise ValueError(f"robot {rid} already exists")
            added = []
            for i, rid in enumerate(ids, 1):
                rname = name if count == 1 else f"{name or 'Robot'} {rid.split('-')[-1]}"
                entry = {"id": rid, "name": rname or rid, "kind": kind, "model": model, "home_zone": zone,
                         "payload_kg": payload, "capabilities": caps, "connection": connection, "battery": battery}
                self._register_robot(entry, source="dashboard")
                self.registry["robots"].append(entry)
                if connection == "simulated":
                    self._start_simulated_robot(rid, zone, battery)
                added.append(dict(self.robots[rid]))
            self._save_registry()
            self._event("config", f"Registered {len(ids)} robot(s): {', '.join(ids)} ({connection})")
            return added

    def remove_robot(self, robot_id: str) -> dict:
        with self.lock:
            robot = self.robots.get(robot_id)
            if robot is None:
                raise KeyError(robot_id)
            if robot["source"] == "file":
                raise ValueError(f"{robot_id} is defined in the factory file; remove it there")
            if self._active_tasks(robot_id):
                raise ValueError(f"{robot_id} has an active task")
            stop = self._agents.pop(robot_id, None)
            if stop:
                stop.set()
            del self.robots[robot_id]
            self.outbox.pop(robot_id, None)
            self.registry["robots"] = [r for r in self.registry["robots"] if r["id"] != robot_id]
            self._save_registry()
            self._event("config", f"Robot {robot_id} removed")
            return robot

    def add_machine(self, spec: dict) -> dict:
        """Registers a machine.

        spec: id, name, type ("3d_printer"), driver ("simulated" or "bambu"), zone,
              simulated: files (list or comma-separated), print_seconds
              bambu: ip, serial, access_code, use_ams, prints_dir (default "prints"), poll_seconds
        """
        mid = str(spec.get("id") or "").strip()
        if not ID_RE.match(mid):
            raise ValueError("machine ID: letters, digits, '.', '_' or '-' (max 40)")
        mtype = spec.get("type") or "3d_printer"
        if mtype not in MACHINE_TYPES:
            raise ValueError(f"type must be one of {', '.join(sorted(MACHINE_TYPES))}")
        driver = spec.get("driver") or "simulated"
        if driver not in DRIVERS:
            raise ValueError(f"driver must be one of {', '.join(sorted(DRIVERS))}")
        name = str(spec.get("name") or "").strip()
        if name and not NAME_RE.match(name):
            raise ValueError("name: up to 60 letters, digits, spaces or .,()/+#-")
        entry = {"id": mid, "name": name or mid, "type": mtype, "driver": driver,
                 "zone": self._check_zone(spec.get("zone")),
                 "prints_dir": str(spec.get("prints_dir") or "prints")}
        if driver == "bambu" and mtype != "3d_printer":
            raise ValueError("Bambu Lab is a 3D printer connection; use simulated for other machine types")
        if driver == "simulated":
            files = spec.get("files") or []
            if isinstance(files, str):
                files = [f.strip() for f in re.split(r"[,\n]", files)]
            files = [f for f in files if f]
            if not files or any(len(f) > 100 or "/" in f or "\\" in f for f in files):
                raise ValueError("list at least one job it can do (no folders), e.g. rocket_fin.gcode.3mf or engine_mount")
            try:
                seconds = float(spec.get("print_seconds") or 20)
            except (TypeError, ValueError):
                raise ValueError("cycle time must be a number of seconds") from None
            entry.update(files=files[:50], print_seconds=max(1.0, min(seconds, 86_400.0)))
            if mtype != "3d_printer":
                entry["cycle_seconds"] = entry["print_seconds"]
        else:
            printer = {k: str(spec.get(k) or "").strip() for k in ("ip", "serial", "access_code")}
            missing = [k for k, v in printer.items() if not v]
            if missing:
                raise ValueError(f"Bambu Lab printers need {', '.join(missing)}")
            try:
                ipaddress.ip_address(printer["ip"])
            except ValueError:
                raise ValueError("ip must be an IP address, e.g. 192.168.1.50") from None
            if not re.fullmatch(r"[A-Za-z0-9]{4,32}", printer["serial"]):
                raise ValueError("serial: 4-32 letters and digits")
            printer["use_ams"] = bool(spec.get("use_ams"))
            entry.update(printer=printer, poll_seconds=10)
        with self.lock:
            if mid in self.machines:
                raise ValueError(f"machine {mid} already exists")
            try:
                machine = build_machine(entry, self.base_dir, self.simulate_machines)
            except (ImportError, MachineError, KeyError, OSError) as e:
                raise ValueError(f"cannot connect {mid}: {e}") from e
            self.machines[mid] = machine
            self._attach_upload_dir(machine)
            self.machine_source[mid] = "dashboard"
            self.registry["machines"].append(entry)
            self._save_registry()
            self._event("config", f"Machine {mid} ({entry['name']}, {driver}) added at {entry['zone']}")
            return {k: v for k, v in entry.items() if k != "printer"}

    def remove_machine(self, machine_id: str) -> dict:
        with self.lock:
            if machine_id not in self.machines:
                raise KeyError(machine_id)
            if self.machine_source.get(machine_id) == "file":
                raise ValueError(f"{machine_id} is defined in the factory file; remove it there")
            if any(j["machine_id"] == machine_id and j["status"] in ACTIVE_JOB for j in self.jobs.values()):
                raise ValueError(f"{machine_id} has an active job")
            machine = self.machines.pop(machine_id)
            self.machine_source.pop(machine_id, None)
            self.machine_history.pop(machine_id, None)
            self.registry["machines"] = [m for m in self.registry["machines"] if m["id"] != machine_id]
            self._save_registry()
            self._event("config", f"Machine {machine_id} removed")
            return {"id": machine.id, "zone": machine.zone}

    # machine job files (upload from a laptop, print) ---------------------------

    def machine_files(self, machine_id: str) -> list[dict]:
        machine = self.machines.get(machine_id)
        if machine is None:
            raise KeyError(machine_id)
        if not hasattr(machine, "file_details"):
            return [{"name": f, "size": None, "modified": None, "deletable": False} for f in machine.list_files()]
        return machine.file_details()

    def _attach_upload_dir(self, machine: Machine) -> None:
        if isinstance(machine, SimulatedStation) and self.data_dir:
            machine.upload_dir = self.data_dir / "jobs" / machine.id

    def upload_folder(self, machine_id: str) -> Path:
        """Where uploaded job files of this machine are kept (printers: their prints folder)."""
        machine = self.machines.get(machine_id)
        if machine is None:
            raise KeyError(machine_id)
        if hasattr(machine, "prints_dir"):
            return machine.prints_dir()
        if getattr(machine, "upload_dir", None):
            return machine.upload_dir
        raise ValueError(f"{machine_id} cannot store files (start the server with --data-dir)")

    def add_machine_file(self, machine_id: str, filename: str, upload: Path) -> dict:
        """Stores an uploaded job file: a sliced ``.gcode.3mf`` for 3D printers, the catalog's file types for others."""
        machine = self.machines.get(machine_id)
        if machine is None:
            raise KeyError(machine_id)
        folder = self.upload_folder(machine_id)
        name = re.sub(r"[^\w .()+-]", "_", Path(str(filename or "")).name).strip()
        if len(name) > 120 or not name or name.startswith("."):
            raise ValueError("file name is missing or too long")
        if hasattr(machine, "prints_dir"):
            if not name.lower().endswith(".gcode.3mf"):
                raise ValueError("upload a sliced Bambu Studio file ending in .gcode.3mf (File → Export plate sliced file)")
            with open(upload, "rb") as f:
                if f.read(4) != b"PK\x03\x04":
                    raise ValueError("this is not a valid .3mf file")
        else:
            allowed = accepted_extensions(machine.type)
            if allowed and not any(name.lower().endswith(ext) for ext in allowed):
                raise ValueError(f"{machine_label(machine.type)} takes {', '.join(allowed)} files")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        with self.lock:
            if any(j["machine_id"] == machine_id and j["file"] == name and j["status"] in HOLDING_JOB
                   for j in self.jobs.values()):
                raise ValueError(f"{name} is in use right now; upload it under another name")
            Path(upload).replace(target)
            self._event("config", f"File {name} uploaded to {machine_id}")
        return next(d for d in self.machine_files(machine_id) if d["name"] == name)

    def remove_machine_file(self, machine_id: str, name: str) -> dict:
        path = self.upload_folder(machine_id) / Path(name).name
        if not path.is_file():
            raise ValueError(f"{name} is not an uploaded file of {machine_id}")
        with self.lock:
            if any(j["machine_id"] == machine_id and j["file"] == path.name and j["status"] in HOLDING_JOB
                   for j in self.jobs.values()):
                raise ValueError(f"{path.name} is in use right now")
            path.unlink()
            self._event("config", f"File {path.name} removed from {machine_id}")
        return {"name": path.name}

    def ensure_machine(self, machine_type: str) -> dict:
        """A machine of this type: an existing one (live hardware first), or a new simulated one."""
        if machine_type not in MACHINE_CATALOG:
            raise ValueError(f"unknown machine type {machine_type!r}")
        with self.lock:
            same = [m for m in self.machines.values() if m.type == machine_type]
            same.sort(key=lambda m: (not is_live(m), m.id))
            if same:
                return self._machine_view(same[0])
            words = set(re.findall(r"[a-z]+", machine_label(machine_type).lower())) - {"machine", "cnc", "and", "the"}
            zone = next((z for z in self.site["zones"] if words & set(re.findall(r"[a-z]+", z.lower()))), None)
            if zone is None:
                if "Workshop" not in self.site["zones"]:
                    xs = [p["x"] for p in self.site["zones"].values()] or [0]
                    self.add_zone("Workshop", max(xs) + 10, 0)
                zone = "Workshop"
            mid = self._free_ids(PREFIX.get(machine_type, "M"), 1, self.machines)[0]
        self.add_machine({"id": mid, "name": machine_label(machine_type), "type": machine_type, "driver": "simulated",
                          "zone": zone, "files": [f"{machine_type}_job"], "print_seconds": 20})
        self.machine_source[mid] = "auto"
        return self._machine_view(self.machines[mid])

    def camera_frame(self, machine_id: str):
        machine = self.machines.get(machine_id)
        if machine is None:
            raise KeyError(machine_id)
        return machine.camera_frame()

    def _machine_view(self, m: Machine) -> dict:
        info = m.describe() | {"files": m.list_files()}
        info.update(name=m.spec.get("name") or m.id, live=is_live(m), type_label=machine_label(m.type),
                    stage=machine_stage(m), source=self.machine_source.get(m.id, "file"))
        return info

    def print_file(self, machine_id: str, file: str, robot_id: str | None = None) -> dict:
        """Starts one print of ``file`` on that machine; a robot collects it when done."""
        machine = self.machines.get(machine_id)
        if machine is None:
            raise KeyError(machine_id)
        if file not in machine.list_files():
            raise ValueError(f"{file} is not on {machine_id}")
        if robot_id and robot_id not in self.robots:
            raise ValueError(f"unknown robot {robot_id}")
        state = machine.state()
        with self.lock:
            if state != "idle" or any(j["machine_id"] == machine_id and j["status"] in HOLDING_JOB for j in self.jobs.values()):
                raise ValueError(f"{machine.spec.get('name') or machine_id} is busy; try again when it is free")
            job = self._new_job(f"Make {file}", machine_id, file, self.default_output_zone, "started from Settings",
                                needs_approval=self.require_confirmation)
            job.update(origin="chat", preferred_robot=robot_id)  # progress shows up in the StarForge conversation
            self._event("request", f"{job['id']}: print {file} on {machine_id} (from Settings)", job_id=job["id"])
        if not self.require_confirmation:
            self.confirm_job(job["id"])
        return dict(job)

    # documents ---------------------------------------------------------------

    def _documents_dir(self) -> Path:
        if self.data_dir is None:
            raise ValueError("document upload needs a data directory (robofleet factory serve --data-dir)")
        return self.data_dir / "documents"

    def add_document(self, name: str, data: bytes) -> dict:
        """Stores a reference PDF (work instruction, drawing, SOP) that work orders can attach."""
        folder = self._documents_dir()
        if not data.startswith(b"%PDF-"):
            raise ValueError("only PDF files are accepted")
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"PDF is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB")
        name = re.sub(r"[^\w .()-]", "_", Path(str(name or "document.pdf")).name).strip() or "document.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        with self.lock:
            doc_id = self._free_ids("DOC", 1, self.documents)[0]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{doc_id}.pdf").write_bytes(data)
            doc = {"id": doc_id, "name": name[:120], "size": len(data), "uploaded": round(time.time(), 1)}
            self.documents[doc_id] = doc
            self._save_documents()
            self._event("config", f"Document {doc_id} uploaded: {doc['name']}")
            return dict(doc)

    def document_path(self, doc_id: str) -> Path:
        if doc_id not in self.documents:
            raise KeyError(doc_id)
        return self._documents_dir() / f"{doc_id}.pdf"

    def remove_document(self, doc_id: str) -> dict:
        with self.lock:
            path = self.document_path(doc_id)
            doc = self.documents.pop(doc_id)
            path.unlink(missing_ok=True)
            self._save_documents()
            self._event("config", f"Document {doc_id} removed")
            return doc

    # persistence -------------------------------------------------------------

    def _write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)  # may hold printer access codes
        tmp.replace(path)

    def _save_registry(self) -> None:
        if self.data_dir:
            self._write_json(self.data_dir / "registry.json", self.registry)

    def _save_documents(self) -> None:
        if self.data_dir:
            self._write_json(self._documents_dir() / "index.json", list(self.documents.values()))

    def _load_registry(self) -> None:
        path = self.data_dir / "registry.json"
        docs = self._documents_dir() / "index.json"
        if docs.exists():
            self.documents = {d["id"]: d for d in json.loads(docs.read_text())}
        if not path.exists():
            return
        saved = json.loads(path.read_text())
        for name, pos in saved.get("zones", {}).items():
            if name not in self.site["zones"]:
                self.site["zones"][name] = pos
                self.registry["zones"][name] = pos
        for entry in saved.get("machines", []):
            if entry["id"] in self.machines or entry.get("zone") not in self.site["zones"]:
                continue
            try:
                self.machines[entry["id"]] = build_machine(entry, self.base_dir, self.simulate_machines)
                self._attach_upload_dir(self.machines[entry["id"]])
            except (ImportError, MachineError, KeyError, OSError) as e:
                self._event("error", f"Machine {entry['id']} from the registry could not be loaded: {e}")
                continue
            self.machine_source[entry["id"]] = "dashboard"
            self.registry["machines"].append(entry)
        for entry in saved.get("robots", []):
            if entry["id"] in self.robots:
                continue
            self._register_robot(entry, source="dashboard")
            self.registry["robots"].append(entry)
            if entry.get("connection") == "simulated":
                self._start_simulated_robot(entry["id"], entry.get("home_zone"), entry.get("battery", 100))

    # built-in simulated robots ----------------------------------------------

    def _start_simulated_robot(self, robot_id: str, zone: str | None, battery: float) -> None:
        from .robot_agent import RobotAgent, SimulatedExecutor

        stop = threading.Event()
        self._agents[robot_id] = stop
        agent = RobotAgent(_LocalClient(self, robot_id, stop), robot_id, SimulatedExecutor(self.site["zones"], self.sim_speed_m_s, self.sim_handling_s),
                           zone=zone, battery=battery, interval_s=self.sim_interval_s, verbose=False)
        threading.Thread(target=agent.run_forever, args=(stop,), daemon=True, name=f"sim-{robot_id}").start()

    def simulate_robots(self, seed: int = 1) -> list[str]:
        """Brings every offline robot online as a built-in simulated robot (for demos without hardware)."""
        import random

        rng = random.Random(seed)
        started = []
        with self.lock:
            for robot in self.robots.values():
                if robot["status"] == "offline" and robot["id"] not in self._agents:
                    robot["connection"] = "simulated"
                    self._start_simulated_robot(robot["id"], robot["home_zone"] or robot["zone"], rng.randint(60, 100))
                    started.append(robot["id"])
        return started

    def stop_simulated_robots(self) -> None:
        for stop in self._agents.values():
            stop.set()

    # ------------------------------------------------------------------ view

    def showcase_path(self) -> Path | None:
        """The showcase 3D model (.glb) if the factory file names one and it exists."""
        model = self.showcase.get("path") or self.showcase.get("model")
        path = Path(model) if model else None
        return path if path and path.suffix.lower() == ".glb" and path.is_file() else None

    def _note_machine_state(self, machine_id: str, state: str, now: float) -> None:
        history = self.machine_history.setdefault(machine_id, [])
        if not history or history[-1][1] != state:
            history.append([round(now, 1), state])
        while len(history) > 1 and history[1][0] < now - HISTORY_S:
            history.pop(0)

    def state(self) -> dict:
        machines = [self._machine_view(m) | {"driver": type(m).__name__} for m in list(self.machines.values())]
        with self.lock:
            now = time.time()
            for info in machines:
                self._note_machine_state(info["id"], info["state"], now)
            robots = [dict(r, active_tasks=self._active_tasks(r["id"]),
                           seen_s_ago=None if r["last_seen"] is None else round(now - r["last_seen"], 1))
                      for r in self.robots.values()]
            return {
                "now": now,
                "factory": {k: self.config.get(k) for k in ("name", "type", "default_output_zone")}
                | {"require_confirmation": self.require_confirmation, "model": getattr(self.model, "model", None),
                   "dispatch": "model" if self.dispatch_with_model and self.model else "rules",
                   "persistent": self.data_dir is not None, "assistant": self.assistant_name,
                   "showcase": {"title": self.showcase.get("title") or "", "caption": self.showcase.get("caption") or ""}
                   if self.showcase_path() else None},
                "zones": self.site["zones"], "chargers": self.site["chargers"],
                "machines": machines, "robots": robots,
                "machine_history": {k: list(v) for k, v in self.machine_history.items() if k in self.machines},
                "documents": sorted(self.documents.values(), key=lambda d: d["uploaded"], reverse=True),
                "options": {"robot_kinds": sorted(ROBOT_KINDS), "connections": sorted(CONNECTIONS),
                            "machine_types": {k: v[0] for k, v in MACHINE_TYPES.items()}, "drivers": sorted(DRIVERS),
                            "machine_catalog": MACHINE_CATALOG, "machine_prefix": PREFIX,
                            "priorities": sorted(PRIORITIES)},
                "chat": list(self.chat[-150:]),
                "products": self.products,
                "runs": [self.run_view(r) for r in sorted(self.runs.values(), key=lambda r: r["created"], reverse=True)],
                "jobs": sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True),
                "tasks": sorted(self.tasks.values(), key=lambda t: t["created"], reverse=True),
                "events": list(reversed(self.events[-100:])),
            }


class _LocalClient:
    """Lets a built-in simulated robot talk to the factory directly instead of over HTTP."""

    base_url = "built-in"

    def __init__(self, factory: Factory, robot_id: str, stop: threading.Event):
        self.factory, self.robot_id, self.stop = factory, robot_id, stop

    def call(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        try:
            with self.factory.lock:
                if self.stop.is_set():
                    raise RuntimeError(f"{self.robot_id} was removed")
                if path.endswith("/heartbeat"):
                    return self.factory.heartbeat(self.robot_id, body or {})
                match = re.fullmatch(r"/api/tasks/([\w.-]+)/complete", path)
                if match:
                    body = body or {}
                    return self.factory.complete_task(match.group(1), self.robot_id, bool(body.get("ok", True)),
                                                      str(body.get("note", "")))
        except (KeyError, ValueError) as e:
            raise RuntimeError(str(e)) from e
        raise RuntimeError(f"unsupported call {path}")

    def fetch_document(self, document: dict, folder=None) -> str:
        return str(self.factory.document_path(document["id"]))


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
