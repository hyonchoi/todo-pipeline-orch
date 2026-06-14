"""The phases._invoke_claude body must produce a ready_for_review record
identical to what watcher.run_phase produced before extraction."""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from hermes_pipeline import phases as phases_mod
from hermes_pipeline.config import Config

@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".hermes"
    (d / "ready_for_review").mkdir(parents=True)
    return d

def test_invoke_writes_ready_for_review_on_success(state_dir, monkeypatch):
    monkeypatch.setattr(
        phases_mod, "_run_claude_subprocess",
        lambda **kw: {"returncode": 0, "stdout": "phase ok", "branch": "todo-7-autoplan"},
    )
    out = phases_mod._invoke_claude(
        todo_id="TODO-7",
        phase_key="phase9_merge_ready",
        tick_id="01JT",
        state_dir=state_dir,
        project_slug="demo",
    )
    assert out["status"] == "success"
    rfr = json.loads((state_dir / "ready_for_review" / "7.json").read_text())
    assert rfr["todo_id"] == 7
    assert rfr["merge_status"] == "pending"
    assert rfr["tick_id"] == "01JT"

def test_invoke_propagates_subprocess_failure(state_dir, monkeypatch):
    monkeypatch.setattr(
        phases_mod, "_run_claude_subprocess",
        lambda **kw: {"returncode": 2, "stdout": "boom"},
    )
    with pytest.raises(RuntimeError, match="phase failed"):
        phases_mod._invoke_claude(
            todo_id="TODO-7", phase_key="autoplan",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
        )
