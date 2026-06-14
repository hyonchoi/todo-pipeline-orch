from __future__ import annotations
import json
from pathlib import Path
import pytest
from hermes_pipeline.decision import HermesSelectionDecision
from hermes_pipeline.decision.store import (
    persist, append_outcome, load_recent, rotate_if_needed,
)

def _mk(tid: str, picked: str | None = "TODO-1") -> HermesSelectionDecision:
    return HermesSelectionDecision(
        tick_id=tid,
        timestamp="2026-06-13T12:00:00Z",
        model="claude-opus-4-7",
        prompt_sha="x",
        candidates_considered=["TODO-1"],
        picked=picked,
        rationale="r",
        blocked_reasons={},
        in_flight=[],
    )

def test_persist_writes_decision_json(tmp_path):
    persist(tmp_path, _mk("01JA"))
    body = (tmp_path / "decisions" / "01JA.json").read_text()
    assert json.loads(body)["tick_id"] == "01JA"

def test_persist_is_write_once(tmp_path):
    persist(tmp_path, _mk("01JA"))
    with pytest.raises(FileExistsError):
        persist(tmp_path, _mk("01JA", picked="TODO-2"))

def test_append_outcome_does_not_touch_decision(tmp_path):
    persist(tmp_path, _mk("01JA"))
    before = (tmp_path / "decisions" / "01JA.json").read_text()
    append_outcome(tmp_path, "01JA", outcome="merged", detail={})
    after = (tmp_path / "decisions" / "01JA.json").read_text()
    assert before == after
    sidecar = json.loads((tmp_path / "outcomes" / "01JA.json").read_text())
    assert sidecar["outcome"] == "merged"

def test_load_recent_joins_decisions_and_outcomes(tmp_path):
    persist(tmp_path, _mk("01JA", picked="TODO-1"))
    persist(tmp_path, _mk("01JB", picked="TODO-2"))
    append_outcome(tmp_path, "01JA", outcome="merged", detail={})
    rs = load_recent(tmp_path, n=5)
    assert len(rs) == 2
    by_tick = {r["tick_id"]: r for r in rs}
    assert by_tick["01JA"]["outcome"] == "merged"
    assert by_tick["01JB"]["outcome"] == "in_flight"

def test_rotate_moves_pairs_in_lockstep(tmp_path):
    for i in range(55):
        tid = f"tick{i:03d}"
        persist(tmp_path, _mk(tid))
        if i % 2 == 0:
            append_outcome(tmp_path, tid, outcome="merged", detail={})
    rotate_if_needed(tmp_path, hot_cap=50)
    hot = list((tmp_path / "decisions").glob("*.json"))
    archived = list((tmp_path / "decisions" / "archive").glob("*.json"))
    assert len(hot) == 50
    assert len(archived) == 5
    for ar in archived:
        if ar.stem in {f"tick{i:03d}" for i in range(0, 55, 2)}:
            assert (tmp_path / "outcomes" / "archive" / f"{ar.stem}.json").exists()
