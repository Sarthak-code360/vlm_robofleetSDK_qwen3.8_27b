import json
import unittest
from pathlib import Path

from robofleet import parse_json_reply, rule_based_plan, validate_plan

EXAMPLES = Path(__file__).parent.parent / "examples/warehouse"
SITE = json.loads((EXAMPLES / "site.json").read_text())
FLEET = json.loads((EXAMPLES / "fleet_state.json").read_text())


def plan(assignments=(), messages=()):
    return {"assignments": list(assignments), "robot_messages": list(messages), "summary": ""}


class ValidatorTest(unittest.TestCase):
    def test_rejects_assignment_that_drains_battery(self):
        # RBT-004 has 45%, T-901 costs 30 -> 15%, below the 20% reserve.
        approved, rejected = validate_plan(
            plan([{"task_id": "T-901", "robot_id": "RBT-004", "reason": ""}]), SITE, FLEET)
        self.assertEqual(approved["assignments"], [])
        self.assertIn("battery would drop to 15%", rejected[0])

    def test_rejects_made_up_ids_and_duplicates(self):
        approved, rejected = validate_plan(plan([
            {"task_id": "T-902", "robot_id": "RBT-999", "reason": ""},
            {"task_id": "T-777", "robot_id": "RBT-001", "reason": ""},
            {"task_id": "T-902", "robot_id": "RBT-001", "reason": ""},
            {"task_id": "T-902", "robot_id": "RBT-003", "reason": ""},
        ]), SITE, FLEET)
        self.assertEqual([a["robot_id"] for a in approved["assignments"]], ["RBT-001"])
        self.assertEqual(len(rejected), 3)

    def test_rejects_unknown_zone(self):
        approved, rejected = validate_plan(plan(messages=[
            {"robot_id": "RBT-002", "instruction": "go_to_zone", "target_zone": "Mars", "reason": ""},
        ]), SITE, FLEET)
        self.assertEqual(approved["robot_messages"], [])
        self.assertEqual(len(rejected), 1)

    def test_rule_plan_always_passes_checker(self):
        _, rejected = validate_plan(rule_based_plan(SITE, FLEET), SITE, FLEET)
        self.assertEqual(rejected, [])

    def test_parse_json_reply_strips_think_and_fences(self):
        text = '<think>hmm</think>\n```json\n{"assignments": [], "robot_messages": [], "summary": "ok"}\n```'
        self.assertEqual(parse_json_reply(text)["summary"], "ok")


if __name__ == "__main__":
    unittest.main()
