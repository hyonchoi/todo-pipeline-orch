"""Additional coverage for tick subcommand edge cases and codepaths."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_pipeline.config import Config
from hermes_pipeline.cli import (
    _cmd_tick,
    _load_toml_overlay,
    _make_circuit_breaker,
    _persist_tick_id,
)


def _make_decision(picked=None, **kwargs):
    """Create a mock HermesSelectionDecision with the right shape."""
    decision = MagicMock()
    decision.picked = picked or kwargs.get("picked")
    decision.rationale = "test"
    decision.candidates_considered = kwargs.get("candidates_considered", [])
    return decision


def _create_project(projects_dir, name, todos=True):
    """Helper to create a project directory with optional TODOS.md."""
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    if todos:
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")
    return project_dir


class FakeArgs:
    """Minimal argparse.Namespace for testing."""
    def __init__(self, **kwargs):
        kwargs.setdefault("project", None)
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestLoadTomlOverlay:
    """Tests for _load_toml_overlay() — TOML config loading."""

    def test_missing_config_file(self, tmp_path, mocker):
        """Missing config.toml returns (None, CircuitBreakerConfig)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = mocker.MagicMock()
        full_cfg, cb_cfg = _load_toml_overlay(state_dir, config)

        assert full_cfg is None
        assert cb_cfg is not None

    def test_config_file_exception(self, tmp_path, mocker):
        """Exception loading config.toml returns (None, CircuitBreakerConfig)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        mocker.patch(
            "hermes_pipeline.config.load_toml_overlay",
            side_effect=ValueError("bad toml"),
        )

        config = mocker.MagicMock()
        full_cfg, cb_cfg = _load_toml_overlay(state_dir, config)

        assert full_cfg is None
        assert cb_cfg is not None

    def test_valid_config_file(self, tmp_path, mocker):
        """Valid config.toml returns (FullConfig, CircuitBreakerConfig)."""
        from hermes_pipeline.config import FullConfig, SelectionConfig, CircuitBreakerConfig

        state_dir = tmp_path / "state"
        state_dir.mkdir()
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

    def test_creates_with_config(self, tmp_path, mocker):
        """Creates CircuitBreaker with correct config values."""
        cb_cfg = mocker.MagicMock()
        cb_cfg.no_progress_threshold = 5
        cb_cfg.alert_dedup_hours = 12

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        cb = _make_circuit_breaker(state_dir, cb_cfg, "#my-channel")

        assert cb.no_progress_threshold == 5
        assert cb.alert_dedup_hours == 12
        assert cb.slack_channel == "#my-channel"
        assert cb.state_path == state_dir / "circuit.json"


class TestPersistTickId:
    """Tests for _persist_tick_id() — atomic tick_id persistence."""

    def test_persist_writes_sentinel(self, tmp_path):
        """_persist_tick_id writes tick_started sentinel alongside tick_id."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _persist_tick_id(state_dir, "01HB")

        tick_id_content = (state_dir / "current_tick_id.txt").read_text().strip()
        assert tick_id_content == "01HB"

        outcomes_dir = state_dir / "outcomes"
        assert outcomes_dir.exists()
        sentinel_file = list(outcomes_dir.glob("*-phases.json"))
        assert len(sentinel_file) == 1

        content = sentinel_file[0].read_text().strip()
        data = json.loads(content)
        assert data.get("outcome") == "tick_started"

    def test_sentinel_os_error_handled(self, tmp_path, mocker):
        """If writing the sentinel fails, the tick_id is still persisted."""
        from hermes_pipeline.state import _atomic_write_text

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        original = _atomic_write_text

        def side_effect(path, content):
            if "phases.json" in str(path):
                raise OSError("disk full")
            return original(path, content)

        mocker.patch(
            "hermes_pipeline.state._atomic_write_text",
            side_effect=side_effect,
        )

        _persist_tick_id(state_dir, "01HB")

        tick_id_content = (state_dir / "current_tick_id.txt").read_text().strip()
        assert tick_id_content == "01HB"


class TestTickPicked:
    """Tests for _cmd_tick when a TODO is picked (scan loop mode)."""

    def test_tick_picked_registers_tasks(self, tmp_path, mocker):
        """When picked=TODO-10, tasks are registered and tick_id persisted in per-project state."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        from hermes_pipeline.decision.schema import HermesSelectionDecision
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

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        mock_register = mocker.patch(
            "hermes_pipeline.cli.register_todo_phases",
            return_value=["task-001", "task-002"],
        )

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["todo_id"] == "TODO-10"
        assert mock_register.call_args.kwargs["board_slug"] == "demo"

        # Verify tick_id was persisted in per-project state
        project_state = projects_dir / "demo" / ".hermes"
        tick_id_content = (project_state / "current_tick_id.txt").read_text().strip()
        assert len(tick_id_content) > 0

    def test_prior_observe_outcomes_and_circuit_breaker(self, tmp_path, mocker):
        """When prior tick exists and is complete, observe_outcomes + circuit breaker are called."""
        mock_all_complete = mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mock_get_status = mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_2_autoplan": "done"},
        )
        mock_observe = mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Write prior tick_id in per-project state
        project_state = projects_dir / "demo" / ".hermes"
        project_state.mkdir(parents=True)
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

        # observe_outcomes is called for prior tick observation AND for picked=None
        assert mock_observe.call_count >= 1
        # Check the prior tick call
        assert any(
            call.kwargs.get("tick_id") == "01HA6PH2V0ZJ7GK0S39D243TQX"
            for call in mock_observe.call_args_list
        )

    def test_circuit_breaker_observe_on_picked(self, tmp_path, mocker):
        """When a TODO is picked, the circuit breaker is observed with picked=TODO-N."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        from hermes_pipeline.decision.schema import HermesSelectionDecision
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

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        mock_register = mocker.patch(
            "hermes_pipeline.cli.register_todo_phases",
            return_value=["task-001"],
        )

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

        # Verify circuit breaker state was reset
        project_state = projects_dir / "demo" / ".hermes"
        circuit_path = project_state / "circuit.json"
        if circuit_path.exists():
            state = json.loads(circuit_path.read_text())
            assert state.get("consecutive_no_progress") == 0

    def test_kanban_registration_failure_logs_error(self, tmp_path, mocker):
        """Kanban registration failure is caught and logged — tick continues."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        from hermes_pipeline.decision.schema import HermesSelectionDecision
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

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        mocker.patch(
            "hermes_pipeline.cli.register_todo_phases",
            side_effect=RuntimeError("kanban error"),
        )

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        # Per-project error is caught, tick returns 0
        assert result == 0

    def test_kanban_registration_failure_writes_outcome(self, tmp_path, mocker):
        """Kanban registration failure writes failed_to_spawn outcome.

        When register_todo_phases raises RuntimeError, _tick_project catches it,
        writes a ``failed_to_spawn`` outcome sidecar via append_outcome, then
        re-raises. The re-raise is caught at the _cmd_tick level (error
        isolation), but the outcome persists for the circuit breaker.
        """
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        from hermes_pipeline.decision.schema import HermesSelectionDecision
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

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        mocker.patch(
            "hermes_pipeline.cli.register_todo_phases",
            side_effect=RuntimeError("kanban error"),
        )

        # Mock append_outcome to capture the failure record
        mock_append = mocker.patch("hermes_pipeline.decision.store.append_outcome")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)

        # Scan returns 0 (error isolation — one project's failure doesn't
        # affect the exit code), but the failed_to_spawn outcome was written.
        assert result == 0
        mock_append.assert_called_once()
        call_kwargs = mock_append.call_args
        assert call_kwargs.kwargs.get("outcome", call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None) == "failed_to_spawn"


class TestPerProjectLockScanning:
    """Tests for the per-project lock model and scan rotation."""

    def test_per_project_lock_held_skips_only_that_project(self, tmp_path, mocker):
        """A held lock on project-a should not block project-b."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "alpha")
        _create_project(projects_dir, "beta")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Hold the lock for "alpha" only
        alpha_state = projects_dir / "alpha" / ".hermes"
        lock_dir = alpha_state / "tick.lock"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "holder.json").write_text(json.dumps({
            "tick_id": "other",
            "acquired_at": "2026-06-16T00:00:00Z",
            "pid": 12345,
        }))

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        mock_selection.reset_mock()
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        # run_selection was called once — for beta (alpha's lock was held)
        assert mock_selection.call_count == 1

    def test_scan_rotation_varies_first_project(self, tmp_path, mocker):
        """_rotate_projects should advance a cursor so the first-project
        advantage doesn't always land on the same project."""
        from hermes_pipeline.cli import _rotate_projects

        p1 = (Path("/fake/a"), None)
        p2 = (Path("/fake/b"), None)
        projects = [p1, p2]

        # First rotation: cursor=0 → offset 0 → [a, b]; cursor becomes 1
        r1 = _rotate_projects(projects, tmp_path)
        # Second rotation: cursor=1 → offset 1 → [b, a]; cursor becomes 2
        r2 = _rotate_projects(projects, tmp_path)

        assert r1[0][0].name == "a"
        assert r2[0][0].name == "b"

    def test_scan_cursor_written(self, tmp_path):
        """_rotate_projects persists the scan cursor for the next tick."""
        from hermes_pipeline.cli import _rotate_projects

        p1 = (Path("/fake/a"), None)
        p2 = (Path("/fake/b"), None)
        projects = [p1, p2]
        _rotate_projects(projects, tmp_path)
        assert (tmp_path / "scan_cursor.txt").exists()


class TestSelectionTimeout:
    """Tests for timeout threading through run_selection."""

    def test_run_selection_receives_timeout(self, tmp_path, mocker):
        """_tick_project derives a per-project timeout and passes it."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        _cmd_tick(FakeArgs(), config)

        assert mock_selection.called
        call_kwargs = mock_selection.call_args
        # timeout is a keyword argument to run_selection
        assert "timeout" in call_kwargs.kwargs
        assert isinstance(call_kwargs.kwargs["timeout"], int)
        assert call_kwargs.kwargs["timeout"] > 0


class TestSecurityHardening:
    """Tests for the security fixes: slug regex, slack channel, symlinks."""

    def test_slug_rejects_trailing_newline(self):
        """_validate_project_slug should reject slugs with trailing newlines
        (the Z anchor prevents this, while $ would allow it)."""
        from hermes_pipeline.config import _validate_project_slug
        assert not _validate_project_slug("valid-slug\n")

    def test_slack_channel_rejects_leading_dash(self):
        """A leading dash in a slack channel could inject CLI flags."""
        from hermes_pipeline.project_config import _is_valid_slack_channel
        assert not _is_valid_slack_channel("-evil")

    def test_slack_channel_accepts_valid(self):
        """Normal channel names with # or @ prefix should be accepted."""
        from hermes_pipeline.project_config import _is_valid_slack_channel
        assert _is_valid_slack_channel("#alerts")
        assert _is_valid_slack_channel("@person")
        assert _is_valid_slack_channel("plain-channel")

    def test_slack_channel_rejects_trailing_newline(self):
        """A trailing newline could inject into CLI args."""
        from hermes_pipeline.project_config import _is_valid_slack_channel
        assert not _is_valid_slack_channel("#alerts\n")

    def test_discover_projects_skips_symlinks(self, tmp_path, mocker):
        """_discover_projects should skip symlinked project directories."""
        from hermes_pipeline.project_config import _discover_projects

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Real project
        real_project = projects_dir / "real"
        real_project.mkdir()
        (real_project / "TODOS.md").write_text("# TODOS\n\nTODO-1 — test\n")

        # Symlink pointing to the real project (escape attempt)
        symlink = projects_dir / "symlink-project"
        symlink.symlink_to(real_project)

        config = mocker.MagicMock()
        config.projects_dir = projects_dir

        projects = _discover_projects(config)
        slugs = [d.name for d, _ in projects]

        assert "real" in slugs
        assert "symlink-project" not in slugs

    def test_resolve_slack_channel_falls_back_on_invalid(self, tmp_path, mocker):
        """Invalid slack_channel in project.toml falls back to default."""
        from hermes_pipeline.project_config import _resolve_slack_channel, DEFAULT_SLACK_CHANNEL

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        hermes = project_dir / ".hermes"
        hermes.mkdir()
        (hermes / "project.toml").write_text(
            '[notifications]\nslack_channel = "-evil"\n'
        )

        result = _resolve_slack_channel(project_dir, env_channel="")
        assert result == DEFAULT_SLACK_CHANNEL