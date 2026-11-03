"""One coordination cycle: briefing -> model -> checker -> plan, with the rules as fallback."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .briefing import build_messages
from .model import ModelClient, ModelError
from .rules import rule_based_plan
from .schema import PLAN_SCHEMA
from .validator import validate_plan


@dataclass
class Decision:
    """Result of one coordination cycle.

    ``plan`` is what should be sent to the robots: the checked model plan, or the
    rule-based plan when there is no model or the model failed.
    """

    plan: dict
    source: str  # "model" or "rules"
    baseline: dict
    rejected: list[str] = field(default_factory=list)
    model_raw: dict | None = None
    stats: dict | None = None
    error: str | None = None

    def to_record(self) -> dict:
        return {"ts": time.time(), **asdict(self)}


class Coordinator:
    """Plans which robot does which task at one site (factory, warehouse, hotel, ...).

    Args:
        site: Fixed site knowledge: ``zones``, ``chargers``, ``rules`` (see examples/).
        model: A :class:`ModelClient`. ``None`` means rules only (offline).
        log_path: If set, every decision is appended to this JSONL file.
    """

    def __init__(self, site: dict, model: ModelClient | None = None, log_path: str | Path | None = None):
        self.site = site
        self.model = model
        self.log_path = Path(log_path) if log_path else None

    def plan(self, fleet: dict, images=()) -> Decision:
        baseline, _ = validate_plan(rule_based_plan(self.site, fleet), self.site, fleet)
        if self.model is None:
            decision = Decision(plan=baseline, source="rules", baseline=baseline)
        else:
            try:
                raw, stats = self.model.chat_json(build_messages(self.site, fleet, images), PLAN_SCHEMA, "fleet_plan")
                approved, rejected = validate_plan(raw, self.site, fleet)
                decision = Decision(plan=approved, source="model", baseline=baseline,
                                    rejected=rejected, model_raw=raw, stats=stats)
            except ModelError as e:
                decision = Decision(plan=baseline, source="rules", baseline=baseline, error=str(e))
        self._log(decision)
        return decision

    def _log(self, decision: Decision) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(decision.to_record(), default=str) + "\n")


def load_json(path: str | Path) -> dict:
    """Reads a site, fleet or printer config file."""
    return json.loads(Path(path).read_text())
