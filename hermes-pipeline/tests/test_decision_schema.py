from __future__ import annotations
import json
from hermes_pipeline.decision import HermesSelectionDecision, SelectionContext

def test_decision_roundtrips_json():
    d = HermesSelectionDecision(
        tick_id="01JABCDXYZ",
        timestamp="2026-06-13T12:00:00Z",
        model="claude-opus-4-7",
        prompt_sha="deadbeef",
        candidates_considered=["TODO-1", "TODO-2"],
        picked="TODO-2",
        rationale="TODO-2 unblocks the merge gate work.",
        blocked_reasons={"TODO-3": "depends on TODO-2"},
        in_flight=[],
    )
    parsed = HermesSelectionDecision.from_json(d.to_json())
    assert parsed == d

def test_decision_picked_none_is_valid():
    d = HermesSelectionDecision(
        tick_id="t",
        timestamp="2026-06-13T12:00:00Z",
        model="claude-opus-4-7",
        prompt_sha="x",
        candidates_considered=[],
        picked=None,
        rationale="no eligible TODOs",
        blocked_reasons={},
        in_flight=[],
    )
    assert json.loads(d.to_json())["picked"] is None

def test_selection_context_construct():
    ctx = SelectionContext(
        todos_md="- TODO-1: do thing",
        in_flight=[],
        recent_decisions=[],
        kanban_snapshot={"columns": []},
        project_slug="demo",
    )
    assert ctx.project_slug == "demo"
