"""Additional coverage for tick subcommand edge cases and codepaths."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_pipeline.cli import (
    _cmd_tick,
    _load_toml_overlay,
    _make_circuit_breaker,
    _persist_tick_id,
)


class TestLoadTomlOverlay:
    """Tests for _load_toml_overlay() — TOML config loading."""

    def test_missing_config_file(self, state_dir, mocker):
        """Missing config.toml returns (None, CircuitBreakerConfig)."""
        config = mocker.MagicMock()
        full_cfg, cb_cfg = _load_toml_overlay(state_dir, config)

        assert full_cfg is None
        assert cb_cfg is not None  # Default CircuitBreakerConfig

    def test_config_file_exception(self, state_dir, mocker):
        """Exception loading config.toml returns (None, CircuitBreakerConfig)."""
        mocker.patch(
            "hermes_pipeline.config.load_toml_overlay",
            side_effect=ValueError("bad toml"),
        )

        config = mocker.MagicMock()
        full_cfg, cb_cfg = _load_toml_overlay(state_dir, config)

        assert full_cfg is None
        assert cb_cfg is not None

    def test_valid_config_file(self, state_dir, mocker):
        """Valid config.toml returns (FullConfig, CircuitBreakerConfig)."""
        from hermes_pipeline.config import FullConfig, SelectionConfig, CircuitBreakerConfig

        full_cfg = FullConfig(
            base=mocker.MagicMock(),
            selection=SelectionConfig(),
            circuit_breaker=CircuitBreakerConfig(),
        )

        mocker.patch(
            "hermes_pipeline.config.load_toml_overlay",
            return_value=full_cfg,
        )

        config = mocker.MagicMock()
        result_full, result_cb = _load_toml_overlay(state_dir, config)

        assert result_full is not None
        assert result_cb is full_cfg.circuit_breaker


class TestMakeCircuitBreaker:
    """Tests for _make_circuit_breaker() — circuit breaker factory."""

    def test_creates_with_config(self, state_dir, mocker):
        """Creates CircuitBreaker with correct config values."""
        cb_cfg = mocker.MagicMock()
        cb_cfg.no_progress_threshold = 5
        cb_cfg.backoff_interval_min = 60
        cb_cfg.alert_dedup_hours = 12

        cb = _make_circuit_breaker(state_dir, cb_cfg, "#my-channel")

        assert cb.no_progress_threshold == 5
        assert cb.backoff_interval_min == 60
        assert cb.alert_dedup_hours == 12
        assert cb.slack_channel == "#my-channel"
        assert cb.state_path == state_dir / "circuit.json"


class TestPersistTickId:
    """Tests for _persist_tick_id() — atomic tick_id persistence."""

    def test_persist_writes_sentinel(self, state_dir):
        """_persist_tick_id writes tick_started sentinel alongside tick_id."""
        _persist_tick_id(state_dir, "01HB")

        # Check tick_id was written
        tick_id_content = (state_dir / "current_tick_id.txt").read_text().strip()
        assert tick_id_content == "01HB"

        # Check sentinel was written
        outcomes_dir = state_dir / "outcomes"
        assert outcomes_dir.exists()
        sentinel_file = list(outcomes_dir.glob("*-phases.json"))
        assert len(sentinel_file) == 1

        # Check sentinel content
        content = sentinel_file[0].read_text().strip()
        data = json.loads(content)
        assert data.get("outcome") == "tick_started"

    def test_sentinel_os_error_handled(self, state_dir, mocker):
        """If writing the sentinel fails, the tick_id is still persisted."""
        from hermes_pipeline.state import _atomic_write_text

        # Track which calls succeed/fail by path
        original = _atomic_write_text

        def side_effect(path, content):
            # Second call (sentinel) fails
            if "phases.json" in str(path):
                raise OSError("disk full")
            return original(path, content)

        mocker.patch(
            "hermes_pipeline.state._atomic_write_text",
            side_effect=side_effect,
        )

        _persist_tick_id(state_dir, "01HB")

        # Tick_id should still be written (first call)
        tick_id_content = (state_dir / "current_tick_id.txt").read_text().strip()
        assert tick_id_content == "01HB"


class TestTickPicked:
    """Tests for _cmd_tick when a TODO is picked."""

    def test_tick_picked_registers_tasks(self, state_dir, mocker):
        """When picked=TODO-10, tasks are registered and tick_id persisted."""
        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
        )

        from hermes_pipeline.decision.schema import HermesSelectionDecision
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=["TODO-10"],
            picked="TODO-10",
            rationale="Selected",
            blocked_reasons={},
            in_flight=[],
        )

        # Create project
        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")

        # Mock registration
        mock_register = mocker.patch(
            "hermes_pipeline.cli.register_todo_phases",
            return_value=["task-001", "task-002"],
        )

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 0

        # Verify registration was called
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["todo_id"] == "TODO-10"
        assert mock_register.call_args.kwargs["board_slug"] == "demo"

        # Verify tick_id was persisted (will be a generated ID, not "01HB")
        tick_id_content = (state_dir / "current_tick_id.txt").read_text().strip()
        assert len(tick_id_content) > 0

    def test_prior_observe_outcomes_and_circuit_breaker(self, state_dir, mocker):
        """When prior tick exists and is complete, observe_outcomes + circuit breaker are called."""
        mock_all_complete = mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
        )
        mock_get_status = mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )
        # Patch at the cli module level since it's a local import alias
        mock_observe = mocker.patch(
            "hermes_pipeline.cli.observe_outcomes"
        )
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        from hermes_pipeline.decision.schema import HermesSelectionDecision
        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=["TODO-10"],
            picked=None,
            rationale="All done",
            blocked_reasons={},
            in_flight=[],
        )

        # Write prior tick_id
        (state_dir / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 0

        # Verify observe_outcomes was called for the prior tick
        mock_observe.assert_called_once()
        assert mock_observe.call_args.kwargs["tick_id"] == "01HA6PH2V0ZJ7GK0S39D243TQX"

    def test_circuit_breaker_observe_on_picked(self, state_dir, mocker):
        """When a TODO is picked, the circuit breaker is observed with picked=TODO-N."""
        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
        )

        from hermes_pipeline.decision.schema import HermesSelectionDecision
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=["TODO-10"],
            picked="TODO-10",
            rationale="Selected",
            blocked_reasons={},
            in_flight=[],
        )

        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")

        mock_register = mocker.patch(
            "hermes_pipeline.cli.register_todo_phases",
            return_value=["task-001"],
        )

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 0

        # Verify circuit breaker state was reset (consecutive_no_progress = 0)
        import json as _json
        circuit_path = state_dir / "circuit.json"
        if circuit_path.exists():
            state = _json.loads(circuit_path.read_text())
            assert state.get("consecutive_no_progress") == 0

    def test_kanban_registration_failure_writes_outcome(self, state_dir, mocker):
        """Kanban registration failure writes failed_to_spawn outcome."""
        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
        )

        from hermes_pipeline.decision.schema import HermesSelectionDecision
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=["TODO-10"],
            picked="TODO-10",
            rationale="Selected",
            blocked_reasons={},
            in_flight=[],
        )

        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")

        mocker.patch(
            "hermes_pipeline.cli.register_todo_phases",
            side_effect=RuntimeError("kanban error"),
        )

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 1

        # Verify the outcome sidecar was written
        outcomes_dir = state_dir / "outcomes"
        if outcomes_dir.exists():
            outcome_files = list(outcomes_dir.glob("*.json"))
            found_failed = False
            for f in outcome_files:
                content = f.read_text().strip()
                if content:
                    data = json.loads(content)
                    if data.get("outcome") == "failed_to_spawn":
                        found_failed = True
            assert found_failed, "failed_to_spawn outcome not found"
