"""Builds the 'briefing sheet' the model reads on every call.

The model has no memory between calls, so everything it needs to know
(map, rules, robots, tasks, alerts) is packed into each request.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

SYSTEM_PROMPT = """You are the central coordinator for a fleet of robots at a {site_type}.
You do NOT control motors. You decide which robot does which task, and send
high-level instructions (go_to_charger, pause, resume, go_to_zone).

Hard rules (a validator will reject plans that break them):
- Only use robot IDs, task IDs and zone names that appear in the briefing.
- Assign each task to at most one robot, and each robot at most one new task.
- A robot's battery minus the battery_cost of its new task must stay at or above battery_reserve_pct.
- Never assign tasks to robots with status "fault" or "charging".
- target_zone must be a known zone for go_to_zone, otherwise null.

Prefer urgent tasks first, then the nearest suitable robot. Take alerts and
camera images into account (blocked aisles, hazards). If a task cannot be
safely assigned, leave it unassigned and explain why in the summary.
Respond with JSON only."""


def system_prompt(site: dict) -> str:
    return SYSTEM_PROMPT.format(site_type=site.get("type", "site"))


def build_briefing(site: dict, fleet: dict) -> str:
    briefing = {
        "map_zones": site["zones"],
        "chargers": site["chargers"],
        "rules": site["rules"],
        "robots": fleet["robots"],
        "pending_tasks": fleet["pending_tasks"],
        "alerts": fleet.get("alerts", []),
    }
    return "Current fleet briefing:\n" + json.dumps(briefing, indent=2)


def image_part(path: str | Path) -> dict:
    """Encodes a camera snapshot so it can be sent inside the request."""
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def build_messages(site: dict, fleet: dict, images=(), prompt: str | None = None) -> list[dict]:
    content = [{"type": "text", "text": build_briefing(site, fleet)}]
    content += [image_part(p) for p in images]
    return [
        {"role": "system", "content": prompt or system_prompt(site)},
        {"role": "user", "content": content},
    ]
