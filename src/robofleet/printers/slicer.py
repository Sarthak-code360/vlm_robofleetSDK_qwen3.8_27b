"""Slices an STL into a printer-ready .gcode.3mf using the Bambu Studio CLI."""

from __future__ import annotations

import subprocess
from pathlib import Path


class SlicerError(RuntimeError):
    pass


def slice_stl(slicer_cfg: dict, stl_path: Path, out_path: Path, base_dir: str | Path = ".") -> None:
    """Slices with the Bambu Studio CLI. Profile paths in ``slicer_cfg`` are relative to ``base_dir``."""
    profiles = [Path(base_dir) / slicer_cfg[k] for k in ("machine_profile", "process_profile", "filament_profile")]
    missing = [str(p) for p in profiles if not p.exists()]
    if missing:
        raise SlicerError(f"Missing slicer profiles: {', '.join(missing)}")
    machine, process, filament = profiles

    cmd = [
        *slicer_cfg["command"],
        "--orient", "1",
        "--arrange", "1",
        "--load-settings", f"{machine};{process}",
        "--load-filaments", str(filament),
        "--slice", "0",
        "--export-3mf", str(out_path),
        str(stl_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not out_path.exists():
        raise SlicerError(f"Slicer failed ({result.returncode}):\n{(result.stderr or result.stdout)[-1500:]}")
