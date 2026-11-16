"""Runs on (or next to) each robot: connects it to the factory brain.

Every few seconds the agent sends a heartbeat (zone, battery, status). When the
reply contains a task, the agent hands it to an executor and reports the result.

Executors:
  SimulatedExecutor  drives a virtual robot (travel time from zone distance). For demos.
  CommandExecutor    runs a shell command per task, e.g. the robot's own local AI:
                     'bash real/run_fast.sh {instruction}'   (so101-vlm-agent)
                     Placeholders: {instruction} {from_zone} {to_zone} {task_id}; exit code 0 = success.
"""

from __future__ import annotations

import json
import math
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request


class FactoryClient:
    """Minimal HTTP client for the factory API."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base_url + path, data=None if body is None else json.dumps(body).encode(),
                                         headers=headers, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e

    def state(self) -> dict:
        return self.call("/api/state")

    def request(self, text: str) -> dict:
        return self.call("/api/requests", {"text": text}, timeout=330)

    def confirm(self, job_id: str) -> dict:
        return self.call(f"/api/jobs/{job_id}/confirm", {})


class SimulatedExecutor:
    """Pretends to drive to the pickup zone, pick, drive to the drop zone and place."""

    def __init__(self, zones: dict, speed_m_s: float = 1.0, handling_s: float = 2.0):
        self.zones, self.speed, self.handling_s = zones, speed_m_s, handling_s

    def _travel(self, a: str | None, b: str) -> float:
        if a not in self.zones or b not in self.zones:
            return 0.0
        pa, pb = self.zones[a], self.zones[b]
        return math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"]) / self.speed

    def run(self, task: dict, robot: "RobotAgent") -> tuple[bool, str]:
        robot.log(f"driving to {task['from_zone']}")
        time.sleep(self._travel(robot.zone, task["from_zone"]))
        robot.zone = task["from_zone"]
        robot.log("picking the part")
        time.sleep(self.handling_s)
        robot.log(f"driving to {task['to_zone']}")
        time.sleep(self._travel(robot.zone, task["to_zone"]))
        robot.zone = task["to_zone"]
        time.sleep(self.handling_s / 2)
        robot.battery = max(0.0, robot.battery - task.get("battery_cost", 5))
        return True, "placed (simulated)"


class CommandExecutor:
    """Runs a command for each task; the robot's local controller/VLA does the physical work."""

    def __init__(self, template: str, timeout_s: float = 1800):
        self.template, self.timeout_s = template, timeout_s

    def run(self, task: dict, robot: "RobotAgent") -> tuple[bool, str]:
        values = {k: shlex.quote(str(task.get(k, ""))) for k in ("instruction", "from_zone", "to_zone")}
        values["task_id"] = shlex.quote(task["id"])
        command = self.template.format(**values)
        robot.log(f"running: {command}")
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return False, f"timed out after {self.timeout_s:.0f} s"
        tail = (result.stdout + result.stderr).strip()[-300:]
        if result.returncode == 0:
            robot.zone = task["to_zone"]
            return True, tail or "done"
        return False, f"exit {result.returncode}: {tail}"


class RobotAgent:
    def __init__(self, client: FactoryClient, robot_id: str, executor, zone: str | None = None,
                 battery: float = 100.0, name: str | None = None, interval_s: float = 2.0, verbose: bool = True):
        self.client, self.id, self.executor = client, robot_id, executor
        self.zone, self.battery, self.name = zone, float(battery), name
        self.interval_s, self.verbose = interval_s, verbose
        self.status = "idle"
        self._worker: threading.Thread | None = None
        self._result: tuple = ()

    def log(self, text: str) -> None:
        if self.verbose:
            print(f"[{time.strftime('%H:%M:%S')}] {self.id}: {text}", flush=True)

    def heartbeat(self) -> dict:
        body = {"zone": self.zone, "battery": round(self.battery), "status": self.status}
        if self.name:
            body["name"] = self.name
        return self.client.call(f"/api/robots/{self.id}/heartbeat", body)

    def _handle_message(self, m: dict) -> None:
        self.log(f"instruction: {m.get('instruction')} {m.get('target_zone') or ''} ({m.get('reason', '')})")
        if m.get("instruction") == "go_to_charger" and isinstance(self.executor, SimulatedExecutor):
            self.zone, self.status = m.get("target_zone") or self.zone, "charging"
        elif m.get("instruction") == "go_to_zone" and m.get("target_zone") and isinstance(self.executor, SimulatedExecutor):
            self.zone = m["target_zone"]

    def _work(self, task: dict) -> None:
        try:
            ok, note = self.executor.run(task, self)
        except Exception as e:
            ok, note = False, f"executor error: {e!r}"
        self._result = (task, ok, note)

    def step(self) -> None:
        """One heartbeat. Tasks run in a worker thread so heartbeats continue while the robot works."""
        if self._worker and not self._worker.is_alive():
            task, ok, note = self._result
            self._worker = None
            self.status = "idle"
            self.client.call(f"/api/tasks/{task['id']}/complete", {"robot_id": self.id, "ok": ok, "note": note})
            self.log(f"{'done' if ok else 'FAILED'} {task['id']}: {note}")
        if self.status == "charging" and isinstance(self.executor, SimulatedExecutor):
            self.battery = min(100.0, self.battery + 5 * self.interval_s)
            if self.battery >= 100:
                self.status = "idle"
        reply = self.heartbeat()
        for m in reply.get("messages", []):
            self._handle_message(m)
        task = reply.get("task")
        if task and self._worker is None:
            self.log(f"got {task['id']}: {task['instruction']}")
            self.status = "busy"
            self._worker = threading.Thread(target=self._work, args=(task,), daemon=True)
            self._worker.start()

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self.log(f"connecting to {self.client.base_url} (zone {self.zone}, battery {self.battery:.0f}%)")
        while not stop.is_set():
            try:
                self.step()
            except (urllib.error.URLError, RuntimeError, OSError) as e:
                self.log(f"factory unreachable, retrying: {e}")
            stop.wait(self.interval_s)
