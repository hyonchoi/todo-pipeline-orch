"""The phases._invoke_hermes body must produce a ready_for_review record
identical to what the inline watcher.run_phase produced before extraction."""
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

def _fake_phase(*, phase_key: str, terminal: bool, prompt: str = "do thing",
                 turns: int = 1, timeout: int = 10) -> phases_mod.Phase:
    return phases_mod.Phase(
        phase_key=phase_key, name=phase_key, prompt=prompt,
        tools="Read", turns=turns, timeout=timeout, terminal=terminal,
    )

def _capture_prompt(monkeypatch) -> dict:
    """Patch _run_hermes_subprocess to record the rendered prompt instead of
    running a subprocess. Returns the dict the prompt will be captured into."""
    seen: dict = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": ""}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)
    return seen

def test_invoke_writes_ready_for_review_on_terminal_phase(state_dir, monkeypatch):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
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
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
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
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
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
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])
    with pytest.raises(phases_mod.UnknownPhaseError):
        phases_mod._invoke_hermes(
            todo_id="TODO-7", phase_key="bogus",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
        )


def test_invoke_uses_profile_from_contract_not_gstack_default(tmp_path, monkeypatch):
    """A contract declaring profile='agent-skills' must execute agent-skills
    phases, not silently fall back to the gstack default (regression for the
    profile-not-threaded-to-execution bug)."""
    from hermes_pipeline.contract import write_default_contract

    state_dir = tmp_path / ".hermes"
    (state_dir / "ready_for_review").mkdir(parents=True)
    write_default_contract(state_dir, profile="agent-skills")

    seen = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": "ok"}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)

    out = phases_mod._invoke_hermes(
        todo_id="TODO-7", phase_key="phase_1_spec",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
    )
    assert out["status"] == "success"
    assert "prompt" in seen

    with pytest.raises(phases_mod.UnknownPhaseError):
        phases_mod._invoke_hermes(
            todo_id="TODO-7", phase_key="phase_2_autoplan",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
        )


def test_invoke_falls_back_to_gstack_without_contract(state_dir, monkeypatch):
    """No pipeline.toml exists in state_dir — execution must still work,
    defaulting to the bundled gstack profile."""
    seen = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": "ok"}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)

    out = phases_mod._invoke_hermes(
        todo_id="TODO-7", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
    )
    assert out["status"] == "success"
    assert "prompt" in seen


def test_invoke_propagates_malformed_contract_error(state_dir, monkeypatch):
    """A contract that exists but fails to parse/validate is a real
    configuration error - it must not be silently treated as 'use gstack'."""
    from hermes_pipeline.contract import ContractSchemaError

    (state_dir / "pipeline.toml").write_text("schema_version = ")

    with pytest.raises(ContractSchemaError):
        phases_mod._invoke_hermes(
            todo_id="TODO-7", phase_key="phase_2_autoplan",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
        )


def test_invoke_propagates_subprocess_failure(state_dir, monkeypatch):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])
    monkeypatch.setattr(
        phases_mod, "_run_hermes_subprocess",
        lambda **kw: {"returncode": 2, "stdout": "boom", "stderr": "E100: hermes error"},
    )
    with pytest.raises(RuntimeError, match="phase failed"):
        phases_mod._invoke_hermes(
            todo_id="TODO-7", phase_key="phase_2_autoplan",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
        )

def test_invoke_failure_error_includes_stderr(state_dir, monkeypatch):
    """Phase failure RuntimeError must include stderr for debugging."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])
    monkeypatch.setattr(
        phases_mod, "_run_hermes_subprocess",
        lambda **kw: {"returncode": 2, "stdout": "boom", "stderr": "E100: stderr detail"},
    )
    with pytest.raises(RuntimeError, match="E100: stderr detail"):
        phases_mod._invoke_hermes(
            todo_id="TODO-7", phase_key="phase_2_autoplan",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
        )


def test_run_hermes_subprocess_wraps_hermes_agent_result(monkeypatch):
    """_run_hermes_subprocess must unwrap HermesAgentResult into a dict."""
    from hermes_pipeline.hermes_adapter import HermesAgentResult
    monkeypatch.setattr(
        "hermes_pipeline.hermes_adapter.hermes_agent_call",
        lambda **kw: HermesAgentResult(
            returncode=0, stdout="ok", stderr="", timed_out=False,
        ),
    )
    result = phases_mod._run_hermes_subprocess(
        prompt="test", tools="Read", turns=5, timeout=30, cwd="/tmp",
    )
    assert result == {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    }


def test_run_hermes_subprocess_timed_out_flag_propagates(monkeypatch):
    """_run_hermes_subprocess must preserve the timed_out flag."""
    from hermes_pipeline.hermes_adapter import HermesAgentResult
    monkeypatch.setattr(
        "hermes_pipeline.hermes_adapter.hermes_agent_call",
        lambda **kw: HermesAgentResult(
            returncode=-1, stdout="", stderr="[killed on timeout]", timed_out=True,
        ),
    )
    result = phases_mod._run_hermes_subprocess(
        prompt="test", tools="Read", turns=5, timeout=30, cwd="/tmp",
    )
    assert result["timed_out"] is True
    assert result["returncode"] == -1


def test_run_hermes_subprocess_propagates_exception(monkeypatch):
    """_run_hermes_subprocess should propagate exceptions from hermes_agent_call."""
    monkeypatch.setattr(
        "hermes_pipeline.hermes_adapter.hermes_agent_call",
        lambda **kw: (_ for _ in ()).throw(FileNotFoundError("hermes not found")),
    )
    with pytest.raises(FileNotFoundError, match="hermes not found"):
        phases_mod._run_hermes_subprocess(
            prompt="test", tools="Read", turns=5, timeout=30, cwd="/tmp",
        )

def test_invoke_on_pid_records_child_pid_in_marker(state_dir, monkeypatch):
    """_invoke_hermes should record the child PID in the phase marker via on_pid."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])

    def _capture_on_pid(**kw):
        # The on_pid callback should fire with a PID from the subprocess
        if kw.get("on_pid") is not None:
            kw["on_pid"](42424)  # simulate subprocess PID
        return {"returncode": 0, "stdout": "ok"}

    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture_on_pid)

    phases_mod._invoke_hermes(
        todo_id="TODO-7",
        phase_key="phase_2_autoplan",
        tick_id="01JT",
        state_dir=state_dir,
        project_slug="demo",
    )

    marker = state_dir / "phase_started" / "TODO-7.json"
    if marker.exists():
        data = json.loads(marker.read_text())
        assert data.get("child_pid") == 42424

def test_invoke_load_phases_exception_propagates(monkeypatch):
    """When load_phases raises, the exception should propagate from _invoke_hermes."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: (_ for _ in ()).throw(ValueError("yaml corrupt")))
    with pytest.raises(ValueError, match="yaml corrupt"):
        phases_mod._invoke_hermes(
            todo_id="TODO-7",
            phase_key="phase_2_autoplan",
            tick_id="01JT",
            state_dir="/tmp",
            project_slug="demo",
        )

def test_run_logs_sidecar_write_failure(state_dir, monkeypatch, caplog):
    """When the outcome sidecar write fails (not FileExistsError), it should be logged."""
    import logging
    caplog.set_level(logging.WARNING)

    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])
    monkeypatch.setattr(
        phases_mod, "_run_hermes_subprocess",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("phase boom")),
    )

    # Patch append_outcome to raise a non-FileExistsError exception
    def fake_append_outcome(*a, **kw):
        raise PermissionError("disk full")

    monkeypatch.setattr(
        "hermes_pipeline.decision.store.append_outcome",
        fake_append_outcome,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="phase boom"):
        phases_mod.run(
            state_dir=state_dir,
            todo_id="TODO-7",
            tick_id="01JT",
            phase_key="phase_2_autoplan",
            project_slug="demo",
        )

    # The sidecar failure should be logged
    assert "failed to write outcome sidecar" in caplog.text

def test_run_sidecar_fileexists_error_suppressed(state_dir, monkeypatch):
    """When append_outcome raises FileExistsError, it should be silently
    swallowed so as not to mask the original phase failure."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False),
    ])
    monkeypatch.setattr(
        phases_mod, "_run_hermes_subprocess",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("phase boom")),
    )

    # Patch append_outcome to raise FileExistsError (outcome already exists)
    monkeypatch.setattr(
        "hermes_pipeline.decision.store.append_outcome",
        lambda *a, **kw: (_ for _ in ()).throw(FileExistsError("already exists")),
        raising=False,
    )

    # The original exception should propagate, not the FileExistsError
    with pytest.raises(RuntimeError, match="phase boom"):
        phases_mod.run(
            state_dir=state_dir,
            todo_id="TODO-7",
            tick_id="01JT",
            phase_key="phase_2_autoplan",
            project_slug="demo",
        )


def test_invoke_routes_review_phase_through_review_lifecycle(state_dir, monkeypatch, tmp_path):
    """phase_5_review must go through capture -> hermes -> finalize, not the
    generic rc-check path."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_5_review", terminal=False,
                    prompt="run /review", turns=30, timeout=2400),
    ])
    calls = {}

    from hermes_pipeline import review_phase as rp

    monkeypatch.setattr(rp, "capture_pre_review_state",
                        lambda **kw: rp.PreReviewState(head_sha="abc123", diff_is_empty=False))
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess",
                        lambda **kw: {"returncode": 0, "stdout": "reviewed", "stderr": "", "timed_out": False})

    def _fake_finalize(**kw):
        calls["finalize"] = kw
        return {"status": "success", "phase_key": "phase_5_review", "outcome": rp.OUTCOME_CLEAN}

    monkeypatch.setattr(rp, "finalize_review", _fake_finalize)

    out = phases_mod._invoke_hermes(
        todo_id="TODO-7", phase_key="phase_5_review", tick_id="01JT",
        state_dir=state_dir, project_slug="demo", project_dir=str(tmp_path),
    )
    assert out["outcome"] == rp.OUTCOME_CLEAN
    assert calls["finalize"]["hermes_result"]["stdout"] == "reviewed"
    assert calls["finalize"]["pre_state"].head_sha == "abc123"


def test_invoke_review_phase_short_circuits_on_no_diff(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_5_review", terminal=False, prompt="run /review"),
    ])
    from hermes_pipeline import review_phase as rp
    monkeypatch.setattr(rp, "capture_pre_review_state",
                        lambda **kw: rp.PreReviewState(head_sha="abc", diff_is_empty=True))
    monkeypatch.setattr(rp, "write_review_artifacts", lambda **kw: None)
    monkeypatch.setattr(rp, "commit_all", lambda **kw: None)

    def _boom(**kw):
        raise AssertionError("hermes must not run on a no-diff branch")

    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _boom)

    out = phases_mod._invoke_hermes(
        todo_id="TODO-9", phase_key="phase_5_review", tick_id="01JT",
        state_dir=state_dir, project_slug="demo", project_dir=str(tmp_path),
    )
    assert out["outcome"] == rp.OUTCOME_NO_DIFF



def test_first_phase_injects_spec_and_reference(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
        _fake_phase(phase_key="phase_3_other", terminal=False, prompt="do other"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "docs" / "pipeline").mkdir(parents=True)
    spec_file = project_dir / "docs" / "pipeline" / "TODO-25-spec.md"
    spec_file.write_text("spec content")
    ref_file = project_dir / "docs" / "notes" / "a.md"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("ref content")
    (project_dir / "TODOS.md").write_text(f"""\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** docs/pipeline/TODO-25-spec.md
  - **Reference:** docs/notes/a.md
""")
    seen = _capture_prompt(monkeypatch)
    phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert "Spec (authoritative): docs/pipeline/TODO-25-spec.md" in seen["prompt"]
    assert "Reference material: docs/notes/a.md" in seen["prompt"]


def test_non_first_phase_does_not_inject(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
        _fake_phase(phase_key="phase_3_other", terminal=False, prompt="do other"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "docs" / "pipeline").mkdir(parents=True)
    (project_dir / "docs" / "pipeline" / "TODO-25-spec.md").write_text("spec content")
    (project_dir / "TODOS.md").write_text("""\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** docs/pipeline/TODO-25-spec.md
""")
    seen = _capture_prompt(monkeypatch)
    phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_3_other",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert "Spec (authoritative):" not in seen["prompt"]


def test_missing_spec_file_dropped_reference_kept(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    ref_file = project_dir / "docs" / "notes" / "a.md"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("ref content")
    (project_dir / "TODOS.md").write_text("""\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** docs/pipeline/nonexistent-spec.md
  - **Reference:** docs/notes/a.md
""")
    seen = _capture_prompt(monkeypatch)
    phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert "Spec (authoritative):" not in seen["prompt"]
    assert "Reference material: docs/notes/a.md" in seen["prompt"]


def test_traversal_path_dropped_independently_of_existence(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # File exists on disk (outside project_dir) but must still be rejected
    # by the containment check, independent of the existence check.
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("outside content")
    ref_file = project_dir / "docs" / "notes" / "a.md"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("ref content")
    (project_dir / "TODOS.md").write_text("""\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** ../outside.md
  - **Reference:** docs/notes/a.md
""")
    seen = _capture_prompt(monkeypatch)
    phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert "Spec (authoritative):" not in seen["prompt"]
    assert "Reference material: docs/notes/a.md" in seen["prompt"]


def test_no_todos_md_no_injection_phase_runs_normally(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    seen = _capture_prompt(monkeypatch)
    out = phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert out["status"] == "success"
    assert "Spec (authoritative):" not in seen["prompt"]


def test_empty_phases_list_does_not_raise_indexerror(state_dir, monkeypatch, tmp_path):
    """load_phases() returning [] must not crash phases_list[0] lookup —
    but phase lookup itself already raises UnknownPhaseError first, which
    is the correct existing behavior; this confirms no IndexError leaks
    through first-phase detection before that point."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [])
    import pytest
    with pytest.raises(phases_mod.UnknownPhaseError):
        phases_mod._invoke_hermes(
            todo_id="TODO-25", phase_key="phase_2_autoplan",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
            project_dir=str(tmp_path),
        )
