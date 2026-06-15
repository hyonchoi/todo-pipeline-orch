"""Cross-repo contract: schemas consumed by the Hermes config repo.

These dataclasses are the source of truth. The Hermes `pipeline-tick` and
`pipeline-phase` command definitions import them directly. Do NOT add a
separate markdown contract — keep the docstrings authoritative.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from typing import Literal

Outcome = Literal[
    "in_flight",
    "merged",
    "failed_at_phase_N",
    "discarded",
    "killed_by_operator",
    "failed_to_spawn",
]

@dataclass(frozen=True)
class HermesSelectionDecision:
    """One agent pick per tick. Immutable once written.

    Persisted at `.hermes/decisions/<tick_id>.json`. Joined at read time
    with `.hermes/outcomes/<tick_id>.json` (written later by state.py).
    """
    tick_id: str
    timestamp: str
    model: str
    prompt_sha: str
    candidates_considered: list[str]
    picked: str | None
    rationale: str
    blocked_reasons: dict[str, str]
    in_flight: list[str]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, data: str) -> "HermesSelectionDecision":
        return cls(**json.loads(data))

@dataclass(frozen=True)
class SelectionContext:
    """Input to `run_selection`. Built per-tick by `decision/context.py`."""
    todos_md: str
    in_flight: list[str]
    recent_decisions: list[dict]
    kanban_snapshot: dict
    project_slug: str
