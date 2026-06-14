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

    with patch.object(phases_mod, "_invoke_claude", _fake_invoke):
        phases_mod.run(
            state_dir=state_dir,
            todo_id="TODO-7",
            tick_id="01JT",
            phase_key="autoplan",
        )
    assert seen == [True], "marker must exist when claude invocation begins"

def test_marker_deleted_on_success(state_dir):
    with patch.object(phases_mod, "_invoke_claude", lambda *a, **kw: {"status": "success"}):
        phases_mod.run(state_dir=state_dir, todo_id="TODO-7", tick_id="01JT", phase_key="autoplan")
    assert not _phase_marker(state_dir, "TODO-7").exists()

def test_marker_deleted_on_failure(state_dir):
    def _boom(*a, **kw):
        raise RuntimeError("phase blew up")

    with patch.object(phases_mod, "_invoke_claude", _boom):
        with pytest.raises(RuntimeError):
            phases_mod.run(state_dir=state_dir, todo_id="TODO-7", tick_id="01JT", phase_key="autoplan")
    assert not _phase_marker(state_dir, "TODO-7").exists()

def test_marker_contains_tick_id_and_started_at(state_dir):
    captured = {}

    def _fake_invoke(*a, **kw):
        marker = _phase_marker(state_dir, "TODO-7")
        captured.update(json.loads(marker.read_text()))
        return {"status": "success"}

    with patch.object(phases_mod, "_invoke_claude", _fake_invoke):
        phases_mod.run(state_dir=state_dir, todo_id="TODO-7", tick_id="01JT", phase_key="autoplan")
    assert captured["tick_id"] == "01JT"
    assert "started_at" in captured
    assert captured["phase_key"] == "autoplan"
