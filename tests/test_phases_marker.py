from __future__ import annotations
import json
from pathlib import Path
import pytest
from unittest.mock import patch
from hermes_pipeline import phases as phases_mod

@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".hermes"
    d.mkdir()
    (d / "ready_for_review").mkdir()
    return d

def _phase_marker(state_dir: Path, todo_id: str) -> Path:
    return state_dir / "phase_started" / f"{todo_id}.json"

def test_marker_written_before_invocation(state_dir):
    seen = []

    def _fake_invoke(*a, **kw):
        seen.append(_phase_marker(state_dir, "TODO-7").exists())
        return {"status": "success"}

    with patch.object(phases_mod, "_invoke_hermes", _fake_invoke):
        phases_mod.run(
            state_dir=state_dir,
            todo_id="TODO-7",
            tick_id="01JT",
            phase_key="autoplan",
        )
    assert seen == [True], "marker must exist when claude invocation begins"

def test_marker_deleted_on_success(state_dir):
    with patch.object(phases_mod, "_invoke_hermes", lambda *a, **kw: {"status": "success"}):
        phases_mod.run(state_dir=state_dir, todo_id="TODO-7", tick_id="01JT", phase_key="autoplan")
    assert not _phase_marker(state_dir, "TODO-7").exists()

def test_marker_deleted_on_failure(state_dir):
    def _boom(*a, **kw):
        raise RuntimeError("phase blew up")

    with patch.object(phases_mod, "_invoke_hermes", _boom):
        with pytest.raises(RuntimeError):
            phases_mod.run(state_dir=state_dir, todo_id="TODO-7", tick_id="01JT", phase_key="autoplan")
    assert not _phase_marker(state_dir, "TODO-7").exists()

def test_concurrent_write_marker_refuses_to_overwrite(state_dir):
    """Two phases for the same TODO must not both claim the marker."""
    from hermes_pipeline.phases import _write_marker, MarkerHeld
    _write_marker(state_dir, todo_id="TODO-7", tick_id="01JA", phase_key="autoplan")
    with pytest.raises(MarkerHeld):
        _write_marker(state_dir, todo_id="TODO-7", tick_id="01JB", phase_key="autoplan")

def test_delete_marker_only_unlinks_when_tick_id_matches(state_dir):
    """A second tick must not be able to delete the first tick's live marker."""
    from hermes_pipeline.phases import _write_marker, _delete_marker, _marker_path
    _write_marker(state_dir, todo_id="TODO-7", tick_id="01JA", phase_key="autoplan")
    _delete_marker(state_dir, "TODO-7", tick_id="01JOTHER")
    assert _marker_path(state_dir, "TODO-7").exists(), "non-owner must not unlink marker"
    _delete_marker(state_dir, "TODO-7", tick_id="01JA")
    assert not _marker_path(state_dir, "TODO-7").exists()

def test_run_writes_failed_outcome_on_phase_failure(state_dir, tmp_path):
    """A failed phase must leave a failed_at_phase_<key> outcome sidecar."""
    from unittest.mock import patch as _patch
    from hermes_pipeline import phases as phases_mod
    def _boom(*a, **kw):
        raise RuntimeError("phase blew up")
    with _patch.object(phases_mod, "_invoke_hermes", _boom):
        with pytest.raises(RuntimeError):
            phases_mod.run(state_dir=state_dir, todo_id="TODO-7", tick_id="01JFAIL", phase_key="autoplan")
    out = state_dir / "outcomes" / "01JFAIL.json"
    assert out.exists(), "failed phase must write outcome sidecar"
    data = json.loads(out.read_text())
    assert data["outcome"] == "failed_at_phase_autoplan"
    assert data["detail"]["todo_id"] == "TODO-7"

def test_marker_contains_tick_id_and_started_at(state_dir):
    captured = {}

    def _fake_invoke(*a, **kw):
        marker = _phase_marker(state_dir, "TODO-7")
        captured.update(json.loads(marker.read_text()))
        return {"status": "success"}

    with patch.object(phases_mod, "_invoke_hermes", _fake_invoke):
        phases_mod.run(state_dir=state_dir, todo_id="TODO-7", tick_id="01JT", phase_key="autoplan")
    assert captured["tick_id"] == "01JT"
    assert "started_at" in captured
    assert captured["phase_key"] == "autoplan"
