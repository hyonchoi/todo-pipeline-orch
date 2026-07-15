"""Unit tests for harness.py — fixture factory, preflight, convergence, monitor."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_pipeline.harness import (
    ConvergenceDetector,
    HarnessMonitor,
    HarnessResult,
    create_mock_project,
    filter_phases,
    isolate_config,
    preflight_check,
)
from hermes_pipeline.phases import Phase


class TestCreateMockProject:
    """Test fixture factory creates valid mock projects."""

    def test_create_mock_project_happy_path(self, tmp_path: Path):
        result = create_mock_project(tmp_path, "happy-path")
        assert (tmp_path / ".git").exists()
        assert (tmp_path / "TODOS.md").exists()
        assert "TODO-" in (tmp_path / "TODOS.md").read_text()
        config_toml = tmp_path / ".hermes" / "config.toml"
        assert config_toml.exists()
        assert "claude-haiku-4-5" in config_toml.read_text()
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
        git_dir = Path(shutil.which("git")).parent
        monkeypatch.setenv("PATH", str(git_dir))
        with pytest.raises(RuntimeError, match="[Hh]ermes"):
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
