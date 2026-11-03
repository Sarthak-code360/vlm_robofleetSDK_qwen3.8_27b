"""The checker. Every plan (from the model or the rules) passes through here
before anything reaches a robot. Bad items are dropped, good ones are kept.
"""

from __future__ import annotations

from .schema import INSTRUCTIONS

BLOCKED_STATUSES = {"fault", "charging"}


def validate_plan(plan: dict, site: dict, fleet: dict) -> tuple[dict, list[str]]:
    """Returns ``(approved_plan, rejection_reasons)``."""
    rules = site["rules"]
    zones = site["zones"]
    robots = {r["id"]: r for r in fleet["robots"]}
    tasks = {t["id"]: t for t in fleet["pending_tasks"]}

    approved = {"assignments": [], "robot_messages": [], "summary": plan.get("summary", "")}
    rejected: list[str] = []
    used_tasks: set[str] = set()
    used_robots: set[str] = set()

    for a in plan.get("assignments") or []:
        if not isinstance(a, dict):
            rejected.append(f"assignment is not an object: {a!r}")
            continue
        task_id, robot_id = a.get("task_id"), a.get("robot_id")
        robot, task = robots.get(robot_id), tasks.get(task_id)
        if robot is None:
            rejected.append(f"{task_id}: unknown robot {robot_id!r}")
        elif task is None:
            rejected.append(f"{robot_id}: unknown or already-done task {task_id!r}")
        elif task_id in used_tasks:
            rejected.append(f"{task_id}: assigned more than once")
        elif robot_id in used_robots:
            rejected.append(f"{robot_id}: given more than one new task")
        elif robot["status"] in BLOCKED_STATUSES:
            rejected.append(f"{task_id} -> {robot_id}: robot is {robot['status']}")
        elif len(robot.get("queued_tasks", [])) >= rules["max_queued_tasks_per_robot"]:
            rejected.append(f"{task_id} -> {robot_id}: robot queue is full")
        elif robot["battery"] - task["battery_cost"] < rules["battery_reserve_pct"]:
            left = robot["battery"] - task["battery_cost"]
            rejected.append(
                f"{task_id} -> {robot_id}: battery would drop to {left}% "
                f"(reserve is {rules['battery_reserve_pct']}%)"
            )
        else:
            used_tasks.add(task_id)
            used_robots.add(robot_id)
            approved["assignments"].append(a)

    for m in plan.get("robot_messages") or []:
        if not isinstance(m, dict):
            rejected.append(f"message is not an object: {m!r}")
            continue
        robot_id, instruction, zone = m.get("robot_id"), m.get("instruction"), m.get("target_zone")
        if robot_id not in robots:
            rejected.append(f"message to unknown robot {robot_id!r}")
        elif instruction not in INSTRUCTIONS:
            rejected.append(f"{robot_id}: unknown instruction {instruction!r}")
        elif instruction == "go_to_zone" and zone not in zones:
            rejected.append(f"{robot_id}: go_to_zone with unknown zone {zone!r}")
        else:
            approved["robot_messages"].append(m)

    return approved, rejected
