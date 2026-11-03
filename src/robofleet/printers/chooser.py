"""The model picks which ready-made (already sliced) file to print."""

from __future__ import annotations

from pathlib import Path

from ..model import ModelClient

NO_FILE = "none"


def list_print_files(prints_dir: str | Path) -> list[str]:
    """Names of the sliced ``*.gcode.3mf`` files in ``prints_dir``."""
    prints_dir = Path(prints_dir)
    return sorted(p.name for p in prints_dir.glob("*.gcode.3mf")) if prints_dir.is_dir() else []


def choose_print_file(model: ModelClient, request: str, files: list[str]) -> tuple[dict, dict]:
    """Returns ``({"file": name-or-"none", "reason": ...}, stats)``. The model can only pick from ``files``."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["file", "reason"],
        "properties": {
            "file": {"type": "string", "enum": list(files) + [NO_FILE]},
            "reason": {"type": "string"},
        },
    }
    messages = [
        {"role": "system", "content": (
            "You choose which ready-made file a 3D printer should print. "
            f"Pick the file that matches the request. If none matches, or the request is not about printing, answer \"{NO_FILE}\". "
            "Keep the reason under 15 words.")},
        {"role": "user", "content": "Files:\n" + "\n".join(f"- {f}" for f in files) + f"\n\nRequest: {request}"},
    ]
    choice, stats = model.chat_json(messages, schema, "print_choice", timeout=300)
    if choice.get("file") not in files:
        choice["file"] = NO_FILE
    return choice, stats
