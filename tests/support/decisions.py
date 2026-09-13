"""Shared decision and context helpers for test fixtures."""
from unittest.mock import MagicMock

from hermes_pipeline.decision import SelectionContext


def make_decision(
    picked=None, *, rationale: str = "test", candidates_considered: list | None = None
):
    """Create a mock HermesSelectionDecision with the right shape.

    Args:
        picked: The picked issue (if any)
        rationale: The decision rationale
        candidates_considered: List of candidates considered

    Returns:
        MagicMock with decision shape
    """
    decision = MagicMock()
    decision.picked = picked
    decision.rationale = rationale
    decision.candidates_considered = candidates_considered or []
    return decision


def selection_context(
    *,
    candidate_ids: tuple[str, ...] = ("TODO-1", "TODO-2"),
    selection_markdown: str | None = None,
    in_flight: list[str] | None = None,
    kanban_snapshot: dict | None = None,
) -> SelectionContext:
    """Create a SelectionContext for testing.

    Args:
        candidate_ids: Tuple of candidate TODO IDs
        selection_markdown: Optional markdown override; if None, generated from candidate_ids
        in_flight: Optional list of in-flight TODO IDs
        kanban_snapshot: Optional kanban snapshot dict (default: {})

    Returns:
        SelectionContext configured for testing
    """
    if selection_markdown is None:
        selection_markdown = "\n".join(f"- [ ] **{todo_id}: Title**" for todo_id in candidate_ids)

    if kanban_snapshot is None:
        kanban_snapshot = {}

    return SelectionContext(
        selection_markdown=selection_markdown,
        candidate_ids=candidate_ids,
        in_flight=in_flight or [],
        recent_decisions=[],
        kanban_snapshot=kanban_snapshot,
        project_slug="demo",
    )
