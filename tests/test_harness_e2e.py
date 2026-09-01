"""Integration test — happy-path fixture driven end-to-end through run_harness.

Exercises the full harness orchestration (fixture bootstrap, PipelineRunner,
monitor, report generation) with `phases.run` mocked to avoid real Hermes /
Claude Code subprocess calls. Verifies structural properties (phase ordering,
report contents) per the design doc's "assertion granularity" decision.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from importlib.resources import files
from unittest.mock import patch

import pytest
import yaml

from hermes_pipeline.github_issues import repository_identity
from hermes_pipeline.harness import _offline_terminal_phase_key, run_harness
from hermes_pipeline.phases import (
    load_phases,
    load_profile_prerequisites,
    resolve_profile_phases_path,
)


def _supported_bundled_profiles() -> list[str]:
    root = files("hermes_pipeline").joinpath("data", "phase-profiles")
    profiles = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or not candidate.joinpath("phases.yaml").is_file():
            continue
        prerequisites = load_profile_prerequisites(candidate.name)
        if all(item.support != "Unverified" for item in prerequisites.skills):
            profiles.append(candidate.name)
    return sorted(profiles)


@pytest.fixture(autouse=True)
def _skip_preflight(monkeypatch):
    monkeypatch.setattr(
        "hermes_pipeline.harness.preflight_check",
        lambda **_kwargs: None,
    )


@pytest.mark.parametrize("profile_name", _supported_bundled_profiles())
def test_happy_path_e2e_runs_offline_full_profile_and_generates_report(
    tmp_path, monkeypatch, mocker, capsys, profile_name
):
    """The no-remote fixture converges on exactly the cards registration created."""
    from hermes_pipeline.harness import _OFFLINE_TERMINAL_PROMPT
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo

    workspace = tmp_path / "harness-run"
    monkeypatch.setattr(
        "hermes_pipeline.harness.tempfile.mkdtemp",
        lambda prefix=None, dir=None: str(workspace),
    )
    mocker.patch("hermes_pipeline.harness._kanban_preflight")
    mocker.patch("hermes_pipeline.harness.time.sleep")
    mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
    monkeypatch.delenv("TPO_GH_BIN", raising=False)
    monkeypatch.delenv("TPO_FAKE_GH_STATE", raising=False)
    gh_env_during_run: dict[str, str | None] = {}

    # A tiny kanban: registration creates the cards, workers advance one step per
    # poll, gates stay blocked until the harness auto-completes them.
    board: dict[str, str] = {}
    gates: set[str] = set()
    registered: dict[str, object] = {}
    polls = {"n": 0}
    completed_gates: list[str] = []

    def create(*, prepared, **_kwargs):
        registered["prepared"] = list(prepared)
        for task in prepared:
            board[task.phase_key] = "blocked" if task.gate else "ready"
            if task.gate:
                gates.add(task.phase_key)
        return [f"t_{task.phase_key}" for task in prepared]

    def status(*_args, **_kwargs):
        gh_env_during_run["TPO_GH_BIN"] = os.environ.get("TPO_GH_BIN")
        gh_env_during_run["TPO_FAKE_GH_STATE"] = os.environ.get("TPO_FAKE_GH_STATE")
        polls["n"] += 1
        if polls["n"] == 2:
            board.update({key: "running" for key in board if key not in gates})
        elif polls["n"] == 3:
            board.update({key: "done" for key in board if key not in gates})
        return dict(board)

    def tasks(_tenant, tick_id):
        return {
            key: KanbanTaskInfo(task_id=f"t_{key}", phase_key=key, status=value, todo_id="TODO-1")
            for key, value in board.items()
        }

    def complete(_tenant, task_id):
        key = task_id.removeprefix("t_")
        completed_gates.append(key)
        board[key] = "done"
        return True

    mocker.patch("hermes_pipeline.kanban_tasks.create_prepared_todo_phases", side_effect=create)
    mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=status)
    mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_tasks", side_effect=tasks)
    mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task", side_effect=complete)

    selected_phases = load_phases(resolve_profile_phases_path(profile_name))
    expected_phase_keys = [phase.phase_key for phase in selected_phases]
    offline_terminal_key = _offline_terminal_phase_key(selected_phases, profile_name)

    result = run_harness(
        fixture_name="happy-path",
        loop=False,
        phase_only=None,
        keep_dir=True,
        timeout=60,
        convergence_threshold=3,
        config=None,
        profile_name=profile_name,
    )

    prepared = registered["prepared"]  # what real registration rendered and created
    registered_keys = [task.phase_key for task in prepared]
    profile = yaml.safe_load((workspace / "artifacts" / "harness-phases.yaml").read_text())
    assert [phase["phase_key"] for phase in profile["phases"]] == expected_phase_keys
    terminal = next(p for p in profile["phases"] if p["phase_key"] == offline_terminal_key)
    assert "local terminal workflow" in terminal["prompt"].lower()
    assert "do not push or open a pull request" in terminal["prompt"].lower()

    if profile_name == "native-sdd":
        # The manifest fans the development phase out and skips the profile's
        # terminal phases, so the offline workflow lands on the last worker card.
        assert registered_keys == ["plan:task-1", "validate:task-1"]
        assert _OFFLINE_TERMINAL_PROMPT.splitlines()[0] in prepared[0].body
        assert "END EXTERNAL AGENT PROMPT" in prepared[0].body.split(_OFFLINE_TERMINAL_PROMPT.splitlines()[0])[1]
        assert completed_gates == ["validate:task-1"]
    else:
        assert registered_keys == expected_phase_keys
        assert not any(key.startswith(("plan:", "validate:")) for key in registered_keys)
    assert all(value == "done" for value in board.values())

    # The placeholder origin is never contacted; it only gives the tick a
    # GitHub repository identity to resolve through the bundled fake gh.
    assert repository_identity(workspace / "project") == "tpo-harness/mock-project"
    fake_gh = workspace / "project" / "bin" / "gh"
    assert gh_env_during_run == {
        "TPO_GH_BIN": str(fake_gh),
        "TPO_FAKE_GH_STATE": str(workspace / "project" / ".hermes" / "fake-gh-state.json"),
    }
    assert "TPO_GH_BIN" not in os.environ
    assert "TPO_FAKE_GH_STATE" not in os.environ
    listed = subprocess.run(
        [str(fake_gh), "api", "-H", "Accept: application/vnd.github+json", "--paginate", "--slurp",
         "repos/tpo-harness/mock-project/issues?state=open&labels=tpo%3Atodo&per_page=100"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "TPO_FAKE_GH_STATE": gh_env_during_run["TPO_FAKE_GH_STATE"]},
    )
    assert [issue["number"] for page in json.loads(listed.stdout) for issue in page] == [1]
    contract = tomllib.loads(
        (workspace / "project" / ".hermes" / "pipeline.toml").read_text()
    )
    assert contract["profile"] == profile_name
    assert result.exit_code == 0
    assert result.report_path is not None
    report = json.loads(result.report_path.read_text())
    assert report["profile"] == profile_name
    assert report["fixture_name"] == "happy-path"
    assert report["prompt_client"] == "claude"
    # The report lists exactly the registered cards.
    assert {phase["phase_key"] for phase in report["phases"]} == set(registered_keys)
    assert all(phase["status"] == "completed" for phase in report["phases"])
    assert "passed" in result.summary
    assert f"profile={profile_name}" in result.summary
    assert f"profile={profile_name}" in capsys.readouterr().out


@pytest.mark.skip(reason="phases.run deleted in Task 4; restored when Task 5 rewrites harness dispatch")
def test_happy_path_e2e_single_phase_execution(tmp_path):
    from hermes_pipeline.phases import load_phases

    single_phase_key = load_phases()[0].phase_key

    with patch("hermes_pipeline.phases.run") as mock_run:
        mock_run.return_value = {"status": "success"}

        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=single_phase_key,
            keep_dir=True,
            timeout=60,
            convergence_threshold=3,
            config=None,
        )

    assert mock_run.call_count == 1
    assert mock_run.call_args.kwargs["phase_key"] == single_phase_key

    report = json.loads(result.report_path.read_text())
    assert len(report["phases"]) == 1
    assert report["phases"][0]["phase_key"] == single_phase_key


@pytest.mark.skip(reason="phases.run deleted in Task 4; restored when Task 5 rewrites harness dispatch")
def test_happy_path_e2e_phase_failure_recorded_and_run_continues(tmp_path):
    from hermes_pipeline.hermes_adapter import HermesCallError
    from hermes_pipeline.phases import load_phases

    all_phases = load_phases()
    dispatched_phase_keys = [p.phase_key for p in all_phases if not p.gate]
    failing_phase = dispatched_phase_keys[0]

    def _side_effect(*, phase_key, **kwargs):
        if phase_key == failing_phase:
            raise HermesCallError(returncode=1)
        return {"status": "success"}

    with patch("hermes_pipeline.phases.run", side_effect=_side_effect) as mock_run:
        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=None,
            keep_dir=True,
            timeout=60,
            convergence_threshold=3,
            config=None,
        )

    assert result.exit_code == 1
    assert mock_run.call_count == len(dispatched_phase_keys)

    report = json.loads(result.report_path.read_text())
    failed = [p for p in report["phases"] if p["phase_key"] == failing_phase]
    assert failed and failed[0]["status"] == "failed"
    assert failed[0]["error_message"] == "hermes_error"
