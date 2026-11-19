"""Machines connected to the factory (3D printers today; CNC, welding, soldering later).

Every machine driver has the same small interface, so the factory brain does not
care which brand or kind of machine it talks to:

    machine.describe() -> {"id", "type", "zone", "state", ...}   state: idle | busy | offline | error
    machine.list_files() -> job files the machine can run
    machine.start(file)  -> starts a job (blocking until the machine accepted it)
    machine.poll()       -> "running" | "done" | "failed" for the job started last

To add a new kind of machine, subclass ``Machine`` and register it in ``DRIVERS``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..printers.chooser import list_print_files
from .catalog import MACHINE_CATALOG


class MachineError(RuntimeError):
    pass


class Machine:
    type = "machine"

    def __init__(self, spec: dict, base_dir: Path):
        self.id = spec["id"]
        self.zone = spec["zone"]
        self.spec = spec
        self.base_dir = base_dir

    def describe(self) -> dict:
        return {"id": self.id, "type": self.type, "zone": self.zone, "state": self.state()}

    def state(self) -> str:
        raise NotImplementedError

    def list_files(self) -> list[str]:
        return []

    def start(self, file: str) -> None:
        raise NotImplementedError

    def poll(self) -> str:
        raise NotImplementedError


class _PrinterBase(Machine):
    type = "3d_printer"

    def prints_dir(self) -> Path:
        return self.base_dir / self.spec.get("prints_dir", "prints")

    def list_files(self) -> list[str]:
        return list_print_files(self.prints_dir())

    def file_details(self) -> list[dict]:
        """Job files with size and date. ``uploaded`` files live in the prints folder and can be deleted."""
        folder = self.prints_dir()
        details = []
        for name in self.list_files():
            path = folder / name
            if path.is_file():
                st = path.stat()
                details.append({"name": name, "size": st.st_size, "modified": round(st.st_mtime, 1), "deletable": True})
            else:
                details.append({"name": name, "size": None, "modified": None, "deletable": False})
        return details


class _TimedJob:
    """A job that takes ``seconds`` to finish. Shared by the simulated machines."""

    def _init_timer(self, seconds: float) -> None:
        self.print_seconds = float(seconds)
        self._started: float | None = None
        self._file: str | None = None

    def _progress(self) -> float:
        if self._started is None:
            return 0.0
        if self.print_seconds <= 0:
            return 1.0
        return min(1.0, (time.monotonic() - self._started) / self.print_seconds)

    def state(self) -> str:
        return "busy" if self._started is not None and self._progress() < 1 else "idle"

    def describe(self) -> dict:
        info = super().describe()
        if self._started is not None:
            info.update(job=self._file, progress_pct=round(self._progress() * 100))
        return info

    def start(self, file: str) -> None:
        if self.state() != "idle":
            raise MachineError(f"{self.id} is busy")
        self._started, self._file = time.monotonic(), file

    def poll(self) -> str:
        return "done" if self._progress() >= 1 else "running"


class SimulatedPrinter(_TimedJob, _PrinterBase):
    """A printer that 'prints' for ``print_seconds`` (default 20). For demos and tests."""

    def __init__(self, spec: dict, base_dir: Path):
        super().__init__(spec, base_dir)
        self._init_timer(spec.get("print_seconds", 20))

    def list_files(self) -> list[str]:
        return sorted(set(super().list_files()) | set(self.spec.get("files", [])))


class SimulatedStation(_TimedJob, Machine):
    """Any other machine from the catalog (CNC, laser, lathe, welder, press, ...), simulated.

    ``files`` are the jobs/programs it can run, e.g. ``["engine_mount", "nozzle_insert"]``; each takes
    ``cycle_seconds`` (default 20). Files uploaded from the dashboard are kept in ``upload_dir`` (set by the
    factory). A real driver for such a machine implements the same Machine interface.
    """

    def __init__(self, spec: dict, base_dir: Path):
        super().__init__(spec, base_dir)
        self.type = spec.get("type", "workstation")
        self.upload_dir: Path | None = None
        self._init_timer(spec.get("cycle_seconds", spec.get("print_seconds", 20)))

    def list_files(self) -> list[str]:
        uploaded = [p.name for p in self.upload_dir.iterdir() if p.is_file() and not p.name.startswith(".")] \
            if self.upload_dir and self.upload_dir.is_dir() else []
        return sorted(set(self.spec.get("files", [])) | set(uploaded))

    def file_details(self) -> list[dict]:
        details = []
        for name in self.list_files():
            path = self.upload_dir / name if self.upload_dir else None
            if path and path.is_file():
                st = path.stat()
                details.append({"name": name, "size": st.st_size, "modified": round(st.st_mtime, 1), "deletable": True})
            else:
                details.append({"name": name, "size": None, "modified": None, "deletable": False})
        return details


# Production order: a part made at a later stage needs every earlier-stage part of that unit delivered first.
MACHINE_TYPES = {k: (v["label"], v["stage"]) for k, v in MACHINE_CATALOG.items()}


def machine_stage(machine: Machine) -> int:
    return int(machine.spec.get("stage") or MACHINE_TYPES.get(machine.type, ("", 1))[1])


def is_live(machine: Machine) -> bool:
    """True for machines connected to real hardware (not simulated)."""
    return not isinstance(machine, (SimulatedPrinter, SimulatedStation))


class BambuMachine(_PrinterBase):
    """A real Bambu Lab printer over LAN (needs robofleet[printer] and a printer config file)."""

    ACTIVE = {"PREPARE", "RUNNING", "PAUSE", "SLICING"}
    START_TIMEOUT_S = 300  # the printer must report activity within this time after start

    def __init__(self, spec: dict, base_dir: Path):
        super().__init__(spec, base_dir)
        from ..coordinator import load_json
        from ..printers import BambuPrinter

        # Printer details come inline (added from the dashboard) or from a printer config file.
        self.config = spec["printer"] if "printer" in spec else load_json(base_dir / spec["config"])["printer"]
        self.printer = BambuPrinter.from_config(self.config)
        self.min_poll_s = float(spec.get("poll_seconds", 10))
        self._lock = threading.Lock()
        self._status: dict = {}
        self._status_at = 0.0
        self._error: str | None = None
        self._started: float | None = None
        self._seen_active = False
        self._file: str | None = None

    def _refresh(self, force: bool = False) -> dict:
        with self._lock:
            if force or time.monotonic() - self._status_at >= self.min_poll_s:
                try:
                    self._status, self._error = self.printer.status(timeout=10), None
                except Exception as e:  # network down, printer off, ...
                    self._status, self._error = {}, str(e)
                self._status_at = time.monotonic()
            return self._status

    def state(self) -> str:
        status = self._refresh()
        if self._error:
            return "offline"
        gcode = status.get("gcode_state", "UNKNOWN")
        if gcode in self.ACTIVE:
            return "busy"
        if gcode == "FAILED" or status.get("print_error"):
            return "error"
        return "idle"

    def describe(self) -> dict:
        info = super().describe()
        status = self._status
        info.update(printer_state=status.get("gcode_state"), progress_pct=status.get("mc_percent"),
                    remaining_min=status.get("mc_remaining_time"), job=self._file)
        if self._error:
            info["error"] = self._error
        return info

    def start(self, file: str) -> None:
        from ..printers import check_printer_ready

        problems = check_printer_ready(self._refresh(force=True))
        if self._error:
            raise MachineError(f"{self.id} unreachable: {self._error}")
        if problems:
            raise MachineError("; ".join(problems))
        url = self.printer.upload(self.prints_dir() / file)
        self.printer.start_print(url, use_ams=self.config.get("use_ams", False))
        self._started, self._seen_active, self._file = time.monotonic(), False, file
        self._status_at = 0.0

    def poll(self) -> str:
        gcode = self._refresh().get("gcode_state")
        if gcode in self.ACTIVE:
            self._seen_active = True
            return "running"
        if self._seen_active and gcode == "FINISH":
            return "done"
        if self._seen_active and gcode in {"FAILED", "IDLE"}:
            return "failed"
        if not self._seen_active and self._started and time.monotonic() - self._started > self.START_TIMEOUT_S:
            return "failed"
        return "running"


DRIVERS = {"simulated": SimulatedPrinter, "bambu": BambuMachine}


def build_machine(spec: dict, base_dir: Path, simulate: bool = False) -> Machine:
    """Creates the driver named by ``spec["driver"]``. ``simulate=True`` replaces real machines with simulated ones."""
    driver = "simulated" if simulate else spec.get("driver", "simulated")
    if driver not in DRIVERS:
        raise MachineError(f"{spec['id']}: unknown driver {driver!r} (known: {', '.join(DRIVERS)})")
    if driver == "simulated" and spec.get("type", "3d_printer") != "3d_printer":
        return SimulatedStation(spec, base_dir)
    if driver == "bambu" and spec.get("type", "3d_printer") != "3d_printer":
        raise MachineError(f"{spec['id']}: the bambu driver is for 3D printers")
    return DRIVERS[driver](spec, base_dir)
