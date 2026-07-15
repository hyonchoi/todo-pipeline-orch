"""Integration test — happy-path fixture driven end-to-end through run_harness.

Exercises the full harness orchestration (fixture bootstrap, PipelineRunner,
monitor, report generation) with `phases.run` mocked to avoid real Hermes /
Claude Code subprocess calls. Verifies structural properties (phase ordering,
report contents) per the design doc's "assertion granularity" decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_pipeline.harness import run_harness, preflight_check


@pytest.fixture(autouse=True)
def _skip_preflight(monkeypatch):
    monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)


def test_happy_path_e2e_runs_all_phases_and_generates_report(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    with patch("hermes_pipeline.phases.run") as mock_run:
        mock_run.return_value = {"status": "success"}

        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=None,
            keep_dir=True,
            timeout=60,
            convergence_threshold=3,
            config=None,
        )

    assert result.exit_code == 0
    assert mock_run.call_count > 0

    called_phase_keys = [kwargs["phase_key"] for _, kwargs in mock_run.call_args_list]
    assert called_phase_keys == sorted(set(called_phase_keys), key=called_phase_keys.index)
    assert len(called_phase_keys) == len(set(called_phase_keys))

    assert result.report_path is not None
    report = json.loads(result.report_path.read_text())
    dispatched_report_phases = [p for p in report["phases"] if p["phase_key"] in called_phase_keys]
    assert len(dispatched_report_phases) == len(called_phase_keys)
    assert all(p["status"] == "completed" for p in dispatched_report_phases)

    assert "passed" in result.summary


def test_happy_path_e2e_single_phase_execution(tmp_path, monkeypatch):
    from hermes_pipeline.phases import load_phases

    monkeypatch.setenv("HOME", str(tmp_path))
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


def test_happy_path_e2e_phase_failure_recorded_and_run_continues(tmp_path, monkeypatch):
    from hermes_pipeline.hermes_adapter import HermesCallError
    from hermes_pipeline.phases import load_phases

    monkeypatch.setenv("HOME", str(tmp_path))
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
