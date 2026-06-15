"""The phases._invoke_hermes body must produce a ready_for_review record
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

def _fake_phase(*, phase_key: str, terminal: bool, prompt: str = "do thing") -> phases_mod.Phase:
    return phases_mod.Phase(
        phase_key=phase_key, name=phase_key, prompt=prompt,
        tools="Read", turns=1, timeout=10, terminal=terminal,
    )

def test_invoke_writes_ready_for_review_on_terminal_phase(state_dir, monkeypatch):
    monkeypatch.setattr(phases_mod, "load_phases", lambda: [
        _fake_phase(phase_key="phase_8_finish_branch", terminal=True),
    ])
    monkeypatch.setattr(
        phases_mod, "_run_hermes_subprocess",
        lambda **kw: {"returncode": 0, "stdout": "phase ok"},
    )
    out = phases_mod._invoke_hermes(
        todo_id="TODO-7",
        phase_key="phase_8_finish_branch",
        tick_id="01JT",
        state_dir=state_dir,
        project_slug="demo",
    )
    assert out["status"] == "success"
    # State.write_ready_for_review uses `todo-<n>.json` — the same path
    # State.read_ready_for_review (and merge.run_phase9) reads from.
    rfr = json.loads((state_dir / "ready_for_review" / "todo-7.json").read_text())
    assert rfr["todo_id"] == 7
    assert rfr["merge_status"] == "pending"
    assert rfr["tick_id"] == "01JT"

def test_invoke_does_not_write_rfr_for_non_terminal_phase(state_dir, monkeypatch):
    monkeypatch.setattr(phases_mod, "load_phases", lambda: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])
    monkeypatch.setattr(
        phases_mod, "_run_hermes_subprocess",
        lambda **kw: {"returncode": 0, "stdout": "phase ok"},
    )
    phases_mod._invoke_hermes(
        todo_id="TODO-7", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
    )
    assert not (state_dir / "ready_for_review" / "todo-7.json").exists()

def test_invoke_passes_todo_context_into_prompt(state_dir, monkeypatch):
    """A picked TODO must be visible to Claude. The static phase prompt alone
    leaves Claude with no idea which TODO this run is for."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
    ])
    seen = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": ""}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)
    phases_mod._invoke_hermes(
        todo_id="TODO-7", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
    )
    assert "TODO-7" in seen["prompt"]
    assert "01JT" in seen["prompt"]
    assert "demo" in seen["prompt"]
    assert "do thing" in seen["prompt"]

def test_invoke_raises_on_unknown_phase_key(state_dir, monkeypatch):
    monkeypatch.setattr(phases_mod, "load_phases", lambda: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])
    with pytest.raises(phases_mod.UnknownPhaseError):
        phases_mod._invoke_hermes(
            todo_id="TODO-7", phase_key="bogus",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
        )

def test_invoke_propagates_subprocess_failure(state_dir, monkeypatch):
    monkeypatch.setattr(phases_mod, "load_phases", lambda: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])
    monkeypatch.setattr(
        phases_mod, "_run_hermes_subprocess",
        lambda **kw: {"returncode": 2, "stdout": "boom"},
    )
    with pytest.raises(RuntimeError, match="phase failed"):
        phases_mod._invoke_hermes(
            todo_id="TODO-7", phase_key="phase_2_autoplan",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
        )
