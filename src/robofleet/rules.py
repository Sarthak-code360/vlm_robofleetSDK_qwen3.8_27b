"""Simple rule-based dispatcher.

Two jobs: (1) the safe fallback when the model fails, and (2) the baseline
we compare the model against. If the model can't beat this, it isn't helping.
"""

from __future__ import annotations

import math


def distance(site: dict, zone_a: str, zone_b: str) -> float:
    a, b = site["zones"][zone_a], site["zones"][zone_b]
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def rule_based_plan(site: dict, fleet: dict) -> dict:
    rules = site["rules"]
    plan = {"assignments": [], "robot_messages": [], "summary": "Rule-based plan."}
    taken: set[str] = set()

    # Low battery robots go charge and take no new work.
    for robot in fleet["robots"]:
        if robot["battery"] < rules["charge_threshold_pct"] and robot["status"] != "charging":
            taken.add(robot["id"])
            plan["robot_messages"].append({
                "robot_id": robot["id"],
                "instruction": "go_to_charger",
                "target_zone": site["chargers"][0],
                "reason": f"Battery {robot['battery']}% is below {rules['charge_threshold_pct']}%.",
            })

    # Urgent first, then nearest robot that has battery and queue room.
    by_priority = sorted(fleet["pending_tasks"], key=lambda t: t["priority"] != "urgent")
    for task in by_priority:
        candidates = [
            r for r in fleet["robots"]
            if r["id"] not in taken
            and r["status"] not in {"fault", "charging"}
            and len(r.get("queued_tasks", [])) < rules["max_queued_tasks_per_robot"]
            and r["battery"] - task["battery_cost"] >= rules["battery_reserve_pct"]
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda r: distance(site, r["zone"], task["from_zone"]))
        taken.add(best["id"])
        plan["assignments"].append({
            "task_id": task["id"],
            "robot_id": best["id"],
            "reason": f"Nearest available robot ({distance(site, best['zone'], task['from_zone']):.0f} m).",
        })
    return plan
