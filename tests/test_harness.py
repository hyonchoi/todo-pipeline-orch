"""Unit tests for harness.py — fixture factory, preflight, convergence, monitor."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_pipeline.harness import (
    ConvergenceDetector,
    ConvergenceHaltError,
    HarnessMonitor,
    HarnessResult,
    _ConvergenceMonitor,
    _classify_error_class,
    _dispatch_phase,
    create_mock_project,
    filter_phases,
    isolate_config,
    preflight_check,
    run_harness,
)
from hermes_pipeline.phases import Phase


class TestCreateMockProject:
    """Test fixture factory creates valid mock projects."""

    def test_create_mock_project_happy_path(self, tmp_path: Path):
        result = create_mock_project(tmp_path, "happy-path")
        assert (tmp_path / ".git").exists()
        assert (tmp_path / "TODOS.md").exists()
        assert "TODO-" in (tmp_path / "TODOS.md").read_text()
        assert "project_slug" in result
        assert "todo_id" in result
        assert "branch" in result

    def test_create_mock_project_unknown_fixture_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Unknown fixture"):
            create_mock_project(tmp_path, "nonexistent-fixture")

    def test_create_mock_project_returns_metadata(self, tmp_path: Path):
        result = create_mock_project(tmp_path, "happy-path")
        assert result["project_slug"] == "mock-project"
        assert result["todo_id"] == 1
        assert result["fixture_name"] == "happy-path"


class TestPreflightCheck:
    def test_preflight_check_git_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PATH", "")
        with pytest.raises(RuntimeError, match="[Gg]it"):
            preflight_check()

    def test_preflight_check_hermes_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import shutil
        from hermes_pipeline.hermes_adapter import HermesDependencyError

        git_dir = Path(shutil.which("git")).parent
        monkeypatch.setenv("PATH", str(git_dir))
        with pytest.raises(HermesDependencyError, match="[Hh]ermes"):
            preflight_check()


class TestConvergenceDetector:
    def test_halts_after_threshold(self):
        d = ConvergenceDetector(threshold=3)
        d.record("p1", "hermes_error")
        d.record("p2", "hermes_error")
        assert d.should_halt() is False
        d.record("p3", "hermes_error")
        assert d.should_halt() is True

    def test_resets_on_success(self):
        d = ConvergenceDetector(threshold=3)
        d.record("p1", "hermes_error")
        d.record("p2", "hermes_error")
        d.record("p3", None)
        assert d.should_halt() is False
        d.record("p4", "hermes_error")
        assert d.should_halt() is False

    def test_different_error_class(self):
        d = ConvergenceDetector(threshold=3)
        d.record("p1", "hermes_error")
        d.record("p2", "hermes_error")
        d.record("p3", "timeout")
        assert d.should_halt() is False

    def test_custom_threshold(self):
        d = ConvergenceDetector(threshold=2)
        d.record("p1", "hermes_error")
        assert d.should_halt() is False
        d.record("p2", "hermes_error")
        assert d.should_halt() is True


class TestHarnessMonitor:
    def test_writes_jsonl_events(self, tmp_path: Path):
        log_path = tmp_path / "events.jsonl"
        monitor = HarnessMonitor(log_path)

        monitor("phase_started", {"phase_key": "phase_2_autoplan", "todo_id": "TODO-1"})
        monitor("phase_completed", {"phase_key": "phase_2_autoplan", "duration_ms": 5000})

        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 2

        event1 = json.loads(lines[0])
        assert event1["event_type"] == "phase_started"
        assert event1["phase_key"] == "phase_2_autoplan"
        assert "timestamp" in event1

        event2 = json.loads(lines[1])
        assert event2["event_type"] == "phase_completed"
        assert event2["duration_ms"] == 5000


class TestFilterPhases:
    def test_filters_to_single_phase(self):
        phases = [
            Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
            Phase(phase_key="phase_3", name="Phase 3", prompt="p3", tools="", turns=0),
            Phase(phase_key="phase_4", name="Phase 4", prompt="p4", tools="", turns=0),
        ]
        filtered = filter_phases(phases, "phase_3")
        assert len(filtered) == 1
        assert filtered[0].phase_key == "phase_3"

    def test_unknown_phase_raises(self):
        phases = [
            Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
        ]
        with pytest.raises(ValueError, match="phase_99"):
            filter_phases(phases, "phase_99")

    def test_with_real_phase_list(self):
        from hermes_pipeline.phases import load_phases
        all_phases = load_phases()
        gate_phases = [p for p in all_phases if p.gate]
        assert len(gate_phases) >= 1
        target = gate_phases[0].phase_key
        filtered = filter_phases(all_phases, target)
        assert len(filtered) == 1
        assert filtered[0].gate is True


class TestIsolateConfig:
    def test_sets_env_vars(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()

        with isolate_config(state_dir=state_dir, lock_dir=lock_dir):
            assert os.environ.get("PIPELINE_STATE_DIR") == str(state_dir)
            assert os.environ.get("PIPELINE_LOCK_DIR") == str(lock_dir)

        assert "PIPELINE_STATE_DIR" not in os.environ
        assert "PIPELINE_LOCK_DIR" not in os.environ

    def test_saves_and_restores(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PIPELINE_STATE_DIR", "/original/state")

        with isolate_config(state_dir=Path("/tmp"), lock_dir=Path("/tmp")):
            assert os.environ["PIPELINE_STATE_DIR"] == "/tmp"

        assert os.environ["PIPELINE_STATE_DIR"] == "/original/state"


class TestHarnessResult:
    def test_dataclass_fields(self):
        result = HarnessResult(exit_code=0, report_path=Path("/tmp/report.json"), temp_dir=None, summary="1/1 passed")
        assert result.exit_code == 0
        assert str(result.report_path) == "/tmp/report.json"
        assert result.temp_dir is None
        assert "passed" in result.summary


class TestDispatchPhase:
    """run_phase_fn must invoke the real pipeline entrypoint, not a stub."""

    def test_dispatches_to_phases_run(self, tmp_path: Path):
        phase = Phase(phase_key="phase_2_autoplan", name="Phase 2", prompt="p", tools="", turns=0)
        error_holder: dict = {}

        with patch("hermes_pipeline.phases.run") as mock_run:
            mock_run.return_value = {"status": "success"}
            rc = _dispatch_phase(
                phase,
                state_dir=tmp_path / ".hermes",
                todo_id=1,
                tick_id="TICK1",
                project_slug="mock-project",
                project_dir=tmp_path,
                error_holder=error_holder,
            )

        assert rc == 0
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["phase_key"] == "phase_2_autoplan"
        assert kwargs["todo_id"] == "TODO-1"
        assert kwargs["tick_id"] == "TICK1"
        assert kwargs["project_slug"] == "mock-project"

    def test_dispatch_failure_returns_1_and_classifies_error(self, tmp_path: Path):
        from hermes_pipeline.hermes_adapter import HermesCallError

        phase = Phase(phase_key="phase_2_autoplan", name="Phase 2", prompt="p", tools="", turns=0)
        error_holder: dict = {}

        with patch("hermes_pipeline.phases.run") as mock_run:
            mock_run.side_effect = HermesCallError("boom", returncode=1, stderr="boom")
            rc = _dispatch_phase(
                phase,
                state_dir=tmp_path / ".hermes",
                todo_id=1,
                tick_id="TICK1",
                project_slug="mock-project",
                project_dir=tmp_path,
                error_holder=error_holder,
            )

        assert rc == 1
        assert error_holder["error_class"] == "hermes_error"


class TestClassifyErrorClass:
    def test_dependency_errors(self):
        from hermes_pipeline.hermes_adapter import ClaudeDependencyError, HermesDependencyError

        assert _classify_error_class(HermesDependencyError("x")) == "dependency_error"
        assert _classify_error_class(ClaudeDependencyError("x")) == "dependency_error"

    def test_call_errors(self):
        from hermes_pipeline.hermes_adapter import ClaudeCallError, HermesCallError

        assert _classify_error_class(HermesCallError("x", 1, "")) == "hermes_error"
        assert _classify_error_class(ClaudeCallError("x", 1, "")) == "claude_error"

    def test_timeout(self):
        assert _classify_error_class(TimeoutError("x")) == "timeout"

    def test_unknown_falls_back_to_phase_failure(self):
        assert _classify_error_class(RuntimeError("x")) == "phase_failure"


class TestConvergenceMonitor:
    """The convergence detector must actually halt a harness run, not just track state."""

    def test_halts_after_threshold_consecutive_failures(self, tmp_path: Path):
        events = []
        inner = lambda et, data=None: events.append((et, data))
        detector = ConvergenceDetector(threshold=2)
        error_holder = {}
        monitor = _ConvergenceMonitor(inner, detector, error_holder)

        error_holder["error_class"] = "hermes_error"
        monitor("phase_started", {"phase_key": "p1"})
        monitor("phase_failed", {"phase_key": "p1"})

        error_holder["error_class"] = "hermes_error"
        monitor("phase_started", {"phase_key": "p2"})
        with pytest.raises(ConvergenceHaltError, match="hermes_error"):
            monitor("phase_failed", {"phase_key": "p2"})

        assert len(events) == 4

    def test_success_resets_the_detector(self):
        inner = lambda et, data=None: None
        detector = ConvergenceDetector(threshold=2)
        monitor = _ConvergenceMonitor(inner, detector, {})

        monitor("phase_completed", {"phase_key": "p1"})
        assert detector.should_halt() is False

    def test_tracks_current_phase_for_timeout_reporting(self):
        inner = lambda et, data=None: None
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(inner, detector, {})

        monitor("phase_started", {"phase_key": "phase_2_autoplan"})
        assert monitor.current_phase_key == "phase_2_autoplan"


class TestRunHarnessTimeout:
    """Overall --timeout must actually bound a hung phase, not just be accepted and ignored."""

    def test_hung_phase_times_out_and_reports_partial_progress(self, tmp_path, monkeypatch):
        import time as _time

        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        def _hang_forever(*args, **kwargs):
            _time.sleep(3)

        monkeypatch.setattr("hermes_pipeline.phases.run", _hang_forever)
        monkeypatch.setattr(
            "hermes_pipeline.harness.tempfile.mkdtemp",
            lambda prefix=None: str(tmp_path / "harness-run"),
        )

        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only="phase_2_autoplan",
            keep_dir=True,
            timeout=1,
            convergence_threshold=3,
            kanban_mode="null",
            config=None,
        )

        assert result.exit_code == 1
        assert "timeout" in result.summary.lower()
        report_data = json.loads(result.report_path.read_text())
        assert any(p["status"] == "timeout" for p in report_data["phases"])


class TestKanbanModeHermes:
    """Tests for --kanban hermes wiring in run_harness() using kanban-as-scheduler."""

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_registers_and_polls(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        """--kanban hermes uses register_todo_phases + polling, not PipelineRunner."""

        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "done"})
        mocker.patch("time.sleep")
        mock_observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        result = run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
            kanban_mode="hermes", config=None,
        )

        assert result.exit_code == 0
        mock_observe.assert_called_once()

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_preflight_failure_raises(self, mock_run, monkeypatch):
        from hermes_pipeline.harness import KanbanPreflightError

        preflight_fail = MagicMock(returncode=1, stdout="", stderr="not authenticated")
        mock_run.return_value = preflight_fail
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        with pytest.raises(KanbanPreflightError, match="hermes login"):
            run_harness(
                fixture_name="happy-path", loop=False, phase_only=None,
                keep_dir=False, timeout=60, convergence_threshold=3,
                kanban_mode="hermes", config=None,
            )

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_null_explicit_produces_no_kanban_calls(self, mock_run, monkeypatch, tmp_path):
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)
        with patch("hermes_pipeline.phases.run") as mock_phases_run:
            mock_phases_run.return_value = {"status": "success"}
            run_harness(
                fixture_name="happy-path", loop=False,
                phase_only="phase_2_autoplan", keep_dir=True,
                timeout=60, convergence_threshold=3,
                kanban_mode="null", config=None,
            )
        kanban_calls = [c for c in mock_run.call_args_list
                        if c[0][0][:2] == ["hermes", "kanban"]]
        assert kanban_calls == []

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_polling_emits_jsonl_events(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_2_autoplan": "running"},       # poll loop iteration 1
            {"phase_2_autoplan": "done"},          # poll loop iteration 2 (terminal)
            {"phase_2_autoplan": "done"},          # observe_outcomes call inside _poll_kanban_phases
            {"phase_2_autoplan": "done"},          # final status check at end of run_harness
        ])

        result = run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
            kanban_mode="hermes", config=None,
        )

        assert result.exit_code == 0
        report = json.loads(result.report_path.read_text())
        phases = report["phases"]
        assert len(phases) == 1
        assert phases[0]["phase_key"] == "phase_2_autoplan"
        assert phases[0]["status"] == "completed"

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_preflight_timeout_raises_actionable_error(self, mock_run, monkeypatch):
        from hermes_pipeline.harness import KanbanPreflightError
        import subprocess

        def _run_side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[:3] == ["hermes", "kanban", "list"]:
                raise subprocess.TimeoutExpired(cmd, 15)
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _run_side_effect
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        with pytest.raises(KanbanPreflightError, match="timed out.*15s"):
            run_harness(
                fixture_name="happy-path", loop=False, phase_only=None,
                keep_dir=False, timeout=60, convergence_threshold=3,
                kanban_mode="hermes", config=None,
            )

    def test_convergence_halt_stops_polling_hermes(self, monkeypatch, mocker):
        mock_sp = mocker.patch("hermes_pipeline.harness.subprocess.run")
        mock_sp.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2", "t3"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"p1": "running", "p2": "ready", "p3": "ready"}
            elif call_count[0] == 2:
                return {"p1": "failed", "p2": "running", "p3": "ready"}
            elif call_count[0] == 3:
                return {"p1": "failed", "p2": "failed", "p3": "running"}
            return {"p1": "failed", "p2": "failed", "p3": "failed"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = run_harness(
            fixture_name="happy-path", loop=False, phase_only=None,
            keep_dir=True, timeout=60, convergence_threshold=3,
            kanban_mode="hermes", config=None,
        )

        assert result.exit_code == 1

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_single_phase_registers_filtered(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        """--phase with --kanban hermes should pass phases_path to register_todo_phases."""

        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        mock_register = mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "done"})
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
            kanban_mode="hermes", config=None,
        )

        call_kwargs = mock_register.call_args
        assert call_kwargs.kwargs.get("phases_path") is not None


class TestAutoCompleteGateTasks:
    """Tests for _auto_complete_gate_tasks()."""

    def test_completes_blocked_gate_tasks(self, mocker):
        from hermes_pipeline.harness import _auto_complete_gate_tasks
        import json as _json

        header_gate = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_2b_plan_gate",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )
        header_dev = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_4_development",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )

        mock_data = [
            {"id": "t_gate", "status": "blocked", "body": header_gate + "\ngate"},
            {"id": "t_dev", "status": "ready", "body": header_dev + "\nphase"},
        ]

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(mock_data), stderr="")

        _auto_complete_gate_tasks("demo", "01TICK")

        complete_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][:3] == ["hermes", "kanban", "complete"]
        ]
        assert len(complete_calls) == 1
        assert complete_calls[0][0][0][3] == "t_gate"

    def test_skips_non_blocked_tasks(self, mocker):
        from hermes_pipeline.harness import _auto_complete_gate_tasks
        import json as _json

        header = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_2_autoplan",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )

        mock_data = [
            {"id": "t1", "status": "running", "body": header},
            {"id": "t2", "status": "done", "body": header.replace("phase_2_autoplan", "phase_3")},
        ]

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(mock_data), stderr="")

        _auto_complete_gate_tasks("demo", "01TICK")

        complete_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][:3] == ["hermes", "kanban", "complete"]
        ]
        assert len(complete_calls) == 0

    def test_is_best_effort_on_query_failure(self, mocker):
        """If get_todo_kanban_tasks raises, the function returns without error."""
        from hermes_pipeline.harness import _auto_complete_gate_tasks

        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
            side_effect=RuntimeError("query failed"),
        )

        _auto_complete_gate_tasks("demo", "01TICK")  # Should not raise


class TestPollKanbanPhases:
    """Tests for _poll_kanban_phases()."""

    def test_registers_phases_and_polls_to_completion(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
        )
        import json as _json

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"phase_2_autoplan": "running", "phase_4_development": "ready"}
            return {"phase_2_autoplan": "done", "phase_4_development": "done"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is True
        assert call_count[0] >= 2

        lines = events_log.read_text().strip().splitlines()
        events = [_json.loads(l) for l in lines if l.strip()]
        event_types = [e["event_type"] for e in events]
        assert "phase_started" in event_types
        assert "phase_completed" in event_types

    def test_emits_phase_failed_event_on_kanban_failure(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
        )
        import json as _json

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_2_autoplan": "running"},
            {"phase_2_autoplan": "failed"},
        ])

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is False
        lines = events_log.read_text().strip().splitlines()
        events = [_json.loads(l) for l in lines if l.strip()]
        failed = [e for e in events if e["event_type"] == "phase_failed"]
        assert len(failed) == 1
        assert failed[0]["phase_key"] == "phase_2_autoplan"

    def test_convergence_halt_stops_polling(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
        )
        import json as _json

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2", "t3"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mock_observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"p1": "running", "p2": "ready", "p3": "blocked"}
            elif call_count[0] == 2:
                return {"p1": "failed", "p2": "running", "p3": "blocked"}
            elif call_count[0] == 3:
                return {"p1": "failed", "p2": "failed", "p3": "blocked"}
            return {"p1": "failed", "p2": "failed", "p3": "failed"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is False
        mock_observe.assert_called_once()

    def test_auto_completes_blocked_gates(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2"])
        mock_auto = mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "done", "phase_2b_plan_gate": "done"})
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        mock_auto.assert_called_once_with("demo", "01TICK")

