"""robofleet: coordinate a robot fleet (factory, warehouse, hotel) with a vision-language model.

    from robofleet import Coordinator, ModelClient, load_json

    site = load_json("site.json")          # zones, chargers, rules
    fleet = load_json("fleet_state.json")  # robots, pending tasks, alerts
    coordinator = Coordinator(site, ModelClient.from_env())
    decision = coordinator.plan(fleet)
    print(decision.source, decision.plan)

The model proposes, the checker verifies, and the rule-based planner is the fallback.
"""

from .briefing import build_briefing, build_messages, system_prompt
from .coordinator import Coordinator, Decision, load_json
from .model import DEFAULT_BASE_URL, DEFAULT_MODEL, ModelClient, ModelError, parse_json_reply
from .rules import rule_based_plan
from .schema import INSTRUCTIONS, PLAN_SCHEMA
from .validator import validate_plan

__version__ = "0.1.0"

__all__ = [
    "Coordinator", "Decision", "load_json",
    "ModelClient", "ModelError", "DEFAULT_BASE_URL", "DEFAULT_MODEL", "parse_json_reply",
    "validate_plan", "rule_based_plan",
    "build_briefing", "build_messages", "system_prompt",
    "PLAN_SCHEMA", "INSTRUCTIONS",
    "__version__",
]
