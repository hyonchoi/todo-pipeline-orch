"""Cross-repo contract: schemas consumed by the Hermes config repo.

These dataclasses are the source of truth. The Hermes `pipeline-tick` and
`pipeline-phase` command definitions import them directly. Do NOT add a
separate markdown contract — keep the docstrings authoritative.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Literal

# ---------------------------------------------------------------------------
# Existing: Hermes selection schemas
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Plan-gate: DecisionSheet schemas
# ---------------------------------------------------------------------------

QuestionClassification = Literal["taste", "premise", "user-challenge", "mechanical"]


class PlanGateError(Exception):
    """Validation or operational error in the plan gate."""


@dataclass(frozen=True)
class _Option:
    label: str
    description: str


@dataclass(frozen=True)
class DecisionQuestion:
    """One decision point in a plan. Immutable once validated."""
    question_id: str
    classification: QuestionClassification
    prompt: str
    options: list[_Option]
    recommendation: str
    rationale: str
    answer: str | None = None

    def __post_init__(self):
        # Validate classification
        valid_classifications = ("taste", "premise", "user-challenge", "mechanical")
        if self.classification not in valid_classifications:
            object.__setattr__(
                self,
                "classification",
                f"invalid classification {self.classification!r}; must be one of {valid_classifications}",
            )
            raise PlanGateError(
                f"question {self.question_id!r}: invalid classification {self.classification!r}"
            )
        # Validate options >= 2
        if len(self.options) < 2:
            raise PlanGateError(
                f"question {self.question_id!r}: must have at least 2 options, got {len(self.options)}"
            )
        # Validate recommendation matches an option label
        labels = {opt.label for opt in self.options}
        if self.recommendation not in labels:
            raise PlanGateError(
                f"question {self.question_id!r}: recommendation {self.recommendation!r} "
                f"not in option labels {sorted(labels)}"
            )
        # Validate answer is None or matches an option label
        if self.answer is not None and self.answer not in labels:
            raise PlanGateError(
                f"question {self.question_id!r}: answer {self.answer!r} "
                f"not in option labels {sorted(labels)}"
            )


@dataclass(frozen=True)
class DecisionSheet:
    """The plan-gate artifact. Immutable once validated.

    Persisted at `.hermes/decisions/<tick_id>-plan.json`.
    """
    schema_version: str
    todo_id: int
    tick_id: str
    questions: list[DecisionQuestion]

    def __post_init__(self):
        if self.schema_version != "1.0":
            raise PlanGateError(f"unsupported schema_version {self.schema_version!r}")
        if self.todo_id <= 0:
            raise PlanGateError(f"todo_id must be positive, got {self.todo_id}")
        if len(self.questions) < 1:
            raise PlanGateError("must have at least 1 question (questions cannot be empty)")
        ids = [q.question_id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise PlanGateError(f"duplicate question_id in sheet: {ids}")

    def to_json(self) -> str:
        return json.dumps(_serialize_sheet(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, data: str) -> "DecisionSheet":
        return validate_decision_sheet(json.loads(data))


def _serialize_sheet(sheet: DecisionSheet) -> dict:
    return {
        "schema_version": sheet.schema_version,
        "todo_id": sheet.todo_id,
        "tick_id": sheet.tick_id,
        "questions": [
            {
                "question_id": q.question_id,
                "classification": q.classification,
                "prompt": q.prompt,
                "options": [
                    {"label": o.label, "description": o.description} for o in q.options
                ],
                "recommendation": q.recommendation,
                "rationale": q.rationale,
                "answer": q.answer,
            }
            for q in sheet.questions
        ],
    }


def validate_decision_sheet(data: dict) -> DecisionSheet:
    """Validate and construct a DecisionSheet from a raw dict.

    Raises PlanGateError on any validation failure.
    """
    questions: list[DecisionQuestion] = []
    for qd in data.get("questions", []):
        opts = [
            _Option(label=o["label"], description=o["description"])
            for o in qd["options"]
        ]
        questions.append(
            DecisionQuestion(
                question_id=qd["question_id"],
                classification=qd["classification"],
                prompt=qd["prompt"],
                options=opts,
                recommendation=qd["recommendation"],
                rationale=qd["rationale"],
                answer=qd.get("answer"),
            )
        )
    return DecisionSheet(
        schema_version=data["schema_version"],
        todo_id=data["todo_id"],
        tick_id=data["tick_id"],
        questions=questions,
    )
