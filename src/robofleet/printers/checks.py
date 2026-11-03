"""Checks before anything is sent to the printer. Same idea as the fleet
checker: the model proposes, code verifies."""

from __future__ import annotations

import struct
from pathlib import Path

from .bambu import IDLE_STATES

EDGE_MARGIN_MM = 10


def stl_size(path: str | Path) -> tuple[float, float, float]:
    """Returns the real bounding-box size (x, y, z) of an ASCII or binary STL."""
    data = Path(path).read_bytes()
    points = []
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + count * 50:
            for i in range(count):
                values = struct.unpack_from("<12f", data, 84 + i * 50)
                points += [values[3:6], values[6:9], values[9:12]]
    if not points:
        for line in data.decode(errors="ignore").splitlines():
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                points.append(tuple(float(v) for v in parts[1:]))
    if not points:
        raise ValueError(f"No geometry found in {path}")
    return tuple(max(p[i] for p in points) - min(p[i] for p in points) for i in range(3))


def check_size(size: tuple[float, float, float], build_volume: list[float]) -> list[str]:
    problems = []
    for axis, value, limit in zip("xyz", size, build_volume):
        if value <= 0:
            problems.append(f"{axis} size is {value:.1f} mm: part is flat or empty")
        elif value > limit - EDGE_MARGIN_MM:
            problems.append(f"{axis} size {value:.1f} mm exceeds {limit - EDGE_MARGIN_MM} mm usable")
    return problems


def check_printer_ready(status: dict) -> list[str]:
    problems = []
    state = status.get("gcode_state", "UNKNOWN")
    if state not in IDLE_STATES:
        problems.append(f"printer is busy (state {state})")
    if status.get("print_error"):
        problems.append(f"printer reports error code {status['print_error']}")
    return problems
