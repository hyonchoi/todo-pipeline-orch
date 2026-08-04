"""Integration test — happy-path fixture driven end-to-end through run_harness.

Exercises the full harness orchestration (fixture bootstrap, PipelineRunner,
monitor, report generation) with `phases.run` mocked to avoid real Hermes /
Claude Code subprocess calls. Verifies structural properties (phase ordering,
report contents) per the design doc's "assertion granularity" decision.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from hermes_pipeline.harness import run_harness


@pytest.fixture(autouse=True)
def _skip_preflight(monkeypatch):
    monkeypatch.setattr(
        "hermes_pipeline.harness.preflight_check",
        lambda **_kwargs: None,
    )


def test_happy_path_e2e_runs_offline_full_profile_and_generates_report(
    tmp_path, monkeypatch, mocker
):
    """The no-remote fixture has a successful local terminal phase contract."""
    from hermes_pipeline.phases import load_phases

    workspace = tmp_path / "harness-run"
    monkeypatch.setattr(
        "hermes_pipeline.harness.tempfile.mkdtemp",
        lambda prefix=None, dir=None: str(workspace),
    )
    mocker.patch("hermes_pipeline.harness._kanban_preflight")
    register = mocker.patch(
        "hermes_pipeline.kanban_tasks.register_todo_phases",
        return_value=["t_00000001"],
    )
    mocker.patch("hermes_pipeline.harness.time.sleep")
    mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
    expected_phase_keys = [phase.phase_key for phase in load_phases()]
    completed = {phase_key: "done" for phase_key in expected_phase_keys}
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
        return_value=completed,
    )

    result = run_harness(
        fixture_name="happy-path",
        loop=False,
        phase_only=None,
        keep_dir=True,
        timeout=60,
        convergence_threshold=3,
        config=None,
    )

    profile_path = Path(register.call_args.kwargs["phases_path"])
    profile = yaml.safe_load(profile_path.read_text())
    registered_phase_keys = [phase["phase_key"] for phase in profile["phases"]]
    terminal = next(
        phase
        for phase in profile["phases"]
        if phase["phase_key"] == "phase_8_finish_branch"
    )

    assert registered_phase_keys == expected_phase_keys
    assert "local terminal workflow" in terminal["prompt"].lower()
    assert "do not push or open a pull request" in terminal["prompt"].lower()
    assert subprocess.run(
        ["git", "remote"],
        cwd=workspace / "project",
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    assert result.exit_code == 0
    assert result.report_path is not None
    report = json.loads(result.report_path.read_text())
    assert [phase["phase_key"] for phase in report["phases"]] == expected_phase_keys
    assert all(phase["status"] == "completed" for phase in report["phases"])
    assert "passed" in result.summary


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
            raise HermesCallError("boom", returncode=1, stderr="boom")
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
