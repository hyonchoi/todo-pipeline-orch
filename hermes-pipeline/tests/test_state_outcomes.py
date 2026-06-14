"""Outcome sidecar writes on terminal merge_status transitions."""

from __future__ import annotations
import json
from pathlib import Path
import pytest
from hermes_pipeline.state import State, ReadyForReview


@pytest.fixture
def state(tmp_path: Path) -> State:
    """State with .hermes state root under tmp_path."""
    sd = tmp_path / ".hermes"
    sd.mkdir()
    (sd / "ready_for_review").mkdir()
    return State(
        project="demo",
        lock_dir=tmp_path / "locks",
        checkpoint_dir=sd / "pipeline_checkpoints",
        ready_dir=sd / "ready_for_review",
    )


def _rfr(tick_id: str = "01JT") -> ReadyForReview:
    return ReadyForReview(
        project="demo",
        todo_id=7,
        branch="todo-7",
        pr_url="",
        phase_summaries={},
        kanban_task_id=None,
        merge_status="pending",
        created_at="2026-06-13T00:00:00Z",
        tick_id=tick_id,
    )


def test_set_merge_status_merged_writes_outcome_sidecar(state, tmp_path: Path):
    """Transition to merged writes an outcome sidecar with outcome=merged."""
    state.write_ready_for_review(_rfr())
    state.set_merge_status(todo_id=7, status="merged")
    side = json.loads((tmp_path / ".hermes" / "outcomes" / "01JT.json").read_text())
    assert side["outcome"] == "merged"


def test_set_merge_status_failed_writes_failed_at_phase(state, tmp_path: Path):
    """Transition to failed writes outcome=failed_at_phase_<last_key>."""
    rec_with_phase = ReadyForReview(
        **{
            **_rfr().__dict__,
            "phase_summaries": {"phase3": "boom"},
        }
    )
    state.write_ready_for_review(rec_with_phase)
    state.set_merge_status(todo_id=7, status="failed", error="phase3 crashed")
    side = json.loads((tmp_path / ".hermes" / "outcomes" / "01JT.json").read_text())
    assert side["outcome"].startswith("failed_at_phase")


def test_discard_writes_discarded_outcome(state, tmp_path: Path):
    """Transition to rejected writes outcome=discarded."""
    state.write_ready_for_review(_rfr())
    state.set_merge_status(todo_id=7, status="rejected")
    side = json.loads((tmp_path / ".hermes" / "outcomes" / "01JT.json").read_text())
    assert side["outcome"] == "discarded"


def test_outcome_sidecar_is_write_once_best_effort(state, tmp_path):
    """A second terminal transition for the same tick_id must NOT crash the
    caller — `set_merge_status` has already persisted the new RFR status to
    disk by the time it tries to append the sidecar. Crashing here would
    leave the RFR and outcome divergent (RFR says merged, outcome still
    in_flight). The original sidecar wins; the second call is a no-op."""
    state.write_ready_for_review(_rfr())
    state.set_merge_status(todo_id=7, status="merged")
    # Manually mutate the RFR back to pending so set_merge_status will try
    # to write a different outcome — original sidecar must survive.
    rec = state.read_ready_for_review(7)
    rec.merge_status = "pending"
    state.write_ready_for_review(rec)
    # No raise — best-effort sidecar.
    state.set_merge_status(todo_id=7, status="rejected")
    side = json.loads((tmp_path / ".hermes" / "outcomes" / "01JT.json").read_text())
    assert side["outcome"] == "merged"
    # RFR write-once does NOT apply — merge_status is the operator's view of
    # truth and must reflect the latest transition.
    assert state.read_ready_for_review(7).merge_status == "rejected"
