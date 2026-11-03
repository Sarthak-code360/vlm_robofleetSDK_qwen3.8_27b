"""Turns a plain-English request into a 3D model.

The model writes OpenSCAD code (a text language for 3D shapes), and the
OpenSCAD program turns that code into an STL mesh the slicer can read.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..model import ModelClient

CAD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "openscad", "expected_size_mm", "notes"],
    "properties": {
        "name": {"type": "string", "description": "short_snake_case name"},
        "openscad": {"type": "string"},
        "expected_size_mm": {
            "type": "object",
            "additionalProperties": False,
            "required": ["x", "y", "z"],
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
        },
        "notes": {"type": "string"},
    },
}

SYSTEM_PROMPT = """You design parts for an FDM 3D printer by writing OpenSCAD code.
Rules:
- Units are millimetres. The part must fit in {x} x {y} x {z} mm with at least 10 mm spare.
- Sit the part flat on z=0 and centre it on x=0, y=0.
- Make it printable without supports: flat bottom, overhangs no steeper than 45 degrees, walls at least 1.2 mm.
- Use $fn=64 for round shapes.
- Do not use import(), include, use, or surface(). Write everything in one file.
Return the code, the size you expect, and short notes on design choices. JSON only."""

FORBIDDEN = ("import(", "include <", "use <", "surface(")


class CadError(RuntimeError):
    pass


def design_part(model: ModelClient, request: str, build_volume: list[float]) -> tuple[dict, dict]:
    x, y, z = build_volume
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(x=x, y=y, z=z)},
        {"role": "user", "content": request},
    ]
    design, stats = model.chat_json(messages, CAD_SCHEMA, "cad_design", timeout=180)
    code = design.get("openscad", "")
    for bad in FORBIDDEN:
        if bad in code:
            raise CadError(f"Generated OpenSCAD uses forbidden '{bad}'.")
    return design, stats


def render_stl(openscad_bin: str, scad_path: Path, stl_path: Path) -> None:
    """Runs OpenSCAD to turn ``scad_path`` into ``stl_path``."""
    result = subprocess.run([openscad_bin, "-o", str(stl_path), str(scad_path)],
                            capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or not stl_path.exists():
        raise CadError(f"OpenSCAD failed:\n{result.stderr[-1500:]}")
