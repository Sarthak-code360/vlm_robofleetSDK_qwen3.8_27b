"""The shape of a plan. The model is forced to answer in exactly this JSON shape."""

INSTRUCTIONS = ["go_to_charger", "pause", "resume", "go_to_zone"]

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assignments", "robot_messages", "summary"],
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "robot_id", "reason"],
                "properties": {
                    "task_id": {"type": "string"},
                    "robot_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "robot_messages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["robot_id", "instruction", "target_zone", "reason"],
                "properties": {
                    "robot_id": {"type": "string"},
                    "instruction": {"type": "string", "enum": INSTRUCTIONS},
                    "target_zone": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}
