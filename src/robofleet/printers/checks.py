"""Checks before anything is sent to the printer: the model proposes, code verifies."""

from __future__ import annotations

from .bambu import IDLE_STATES


def check_printer_ready(status: dict) -> list[str]:
    """Returns the reasons the printer cannot start a job now (empty list = ready)."""
    problems = []
    state = status.get("gcode_state", "UNKNOWN")
    if state not in IDLE_STATES:
        problems.append(f"printer is busy (state {state})")
    if status.get("print_error"):
        problems.append(f"printer reports error code {status['print_error']}")
    return problems
