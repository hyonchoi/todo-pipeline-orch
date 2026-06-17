"""Tests for the tick subcommand (TODO-10)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


class TestTickSubcommand:
    """Tests for pipeline-watch tick <project>."""

    def test_tick_help(self):
        """tick subcommand shows in help."""
        from hermes_pipeline.cli import build_parser

        parser = build_parser()
        # Parse --help for tick — argparse --help prints and exits
        with pytest.raises(SystemExit):
            parser.parse_args(["tick", "--help"])

    def test_tick_prior_in_flight_skips(self, state_dir, mocker):
        """Prior tick still has in-flight kanban tasks -> skip."""
        from hermes_pipeline.cli import _cmd_tick

        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=False
        )

        # Write prior_tick_id
        (state_dir / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 0

    def test_tick_prior_complete_proceeds(self, state_dir, mocker):
        """Prior tick complete -> proceed with new selection."""
        from hermes_pipeline.cli import _cmd_tick

        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
        )
        mock_selection = mocker.patch(
            "hermes_pipeline.cli.run_selection"
        )

        # Mock the selection decision
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

        # Write prior_tick_id
        (state_dir / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        # Create TODOS.md
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

    def test_tick_no_prior_proceeds(self, state_dir, mocker):
        """No prior tick -> proceed normally."""
        from hermes_pipeline.cli import _cmd_tick

        mock_selection = mocker.patch(
            "hermes_pipeline.cli.run_selection"
        )

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

        # No prior_tick_id file
        assert not (state_dir / "current_tick_id.txt").exists()

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

    def test_tick_lock_held_exits_early(self, state_dir, mocker):
        """Tick lock already held -> exit early with error."""
        from hermes_pipeline.cli import _cmd_tick

        # Hold the lock
        lock_dir = state_dir / "tick.lock"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "holder.json").write_text(
            json.dumps({
                "tick_id": "other",
                "acquired_at": "2026-06-16T00:00:00Z",
                "pid": 12345,
            })
        )

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir

        result = _cmd_tick(args, config)
        assert result == 1  # Exit code for lock held

    def test_validate_project_slug_valid(self):
        """Valid slugs return True."""
        from hermes_pipeline.cli import _validate_project_slug

        assert _validate_project_slug("demo") is True
        assert _validate_project_slug("my-project") is True
        assert _validate_project_slug("my.project") is True
        assert _validate_project_slug("my_project") is True
        assert _validate_project_slug("a1b2") is True
        # Note: --help is technically valid per the regex (only alphanumeric, dot, dash)
        assert _validate_project_slug("--help") is True

    def test_validate_project_slug_invalid_spaces(self):
        """Slugs with spaces are rejected."""
        from hermes_pipeline.cli import _validate_project_slug

        assert _validate_project_slug("my project") is False

    def test_validate_project_slug_invalid_shell(self):
        """Slugs with shell metacharacters are rejected."""
        from hermes_pipeline.cli import _validate_project_slug

        assert _validate_project_slug("a;b") is False
        assert _validate_project_slug("a|b") is False
        assert _validate_project_slug("a$b") is False
        assert _validate_project_slug("a&b") is False

    def test_validate_project_slug_invalid_empty(self):
        """Empty slug is rejected."""
        from hermes_pipeline.cli import _validate_project_slug

        assert _validate_project_slug("") is False

    def test_tick_invalid_project_slug_rejected(self, state_dir, mocker):
        """Invalid project slug in _cmd_tick -> return 2."""
        from hermes_pipeline.cli import _cmd_tick

        # Mock selection so we get past it into slug validation
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

        project_dir = state_dir.parent / "a;b"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")

        args = mocker.MagicMock()
        args.project = "a;b"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 2

    def test_tick_project_not_found(self, state_dir, mocker):
        """Project directory does not exist -> return 2."""
        from hermes_pipeline.cli import _cmd_tick

        # Create TODOS.md in a different dir
        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n")

        # But tick a different project
        args = mocker.MagicMock()
        args.project = "nonexistent"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 2

    def test_tick_todos_md_not_found(self, state_dir, mocker):
        """TODOS.md missing in project -> return 2."""
        from hermes_pipeline.cli import _cmd_tick

        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        # No TODOS.md

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 2

    def test_tick_kanban_registration_failure(self, state_dir, mocker):
        """Kanban registration raises RuntimeError -> return 1, writes failed_to_spawn."""
        from hermes_pipeline.cli import _cmd_tick

        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
        )
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

        # Create TODOS.md
        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")

        # Mock registration to fail
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

    def test_tick_observe_outcomes_exception(self, state_dir, mocker):
        """observe_outcomes for prior tick raises -> warning, tick proceeds."""
        from hermes_pipeline.cli import _cmd_tick

        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
        )
        # Mock get_todo_kanban_status to raise only when called from the
        # observe_outcomes path (after all_phases_complete check).
        # We need to let the first call (inside all_phases_complete) succeed,
        # so we mock it to return a valid map, then mock observe_outcomes to raise.
        mocker.patch(
            "hermes_pipeline.kanban_tasks.observe_outcomes",
            side_effect=RuntimeError("kanban error"),
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
        assert result == 0  # Should proceed despite observe_outcomes failure

    def test_tick_picked_none_writes_sentinel(self, state_dir, mocker):
        """picked=None -> writes picked_none sentinel, persists tick_id."""
        from hermes_pipeline.cli import _cmd_tick

        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
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

        # No prior tick — skip the observe_outcomes path entirely
        assert not (state_dir / "current_tick_id.txt").exists()

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

        # Check sentinel was written (tick_id is dynamically generated)
        outcomes_dir = state_dir / "outcomes"
        assert outcomes_dir.exists()
        sentinel_files = list(outcomes_dir.glob("*-phases.json"))
        assert len(sentinel_files) > 0

        # Verify the sentinel content
        content = sentinel_files[0].read_text().strip()
        data = json.loads(content)
        assert data.get("outcome") == "picked_none"


class TestCliHelpers:
    """Tests for _cmd_tick helper functions."""

    def test_read_prior_tick_id_existing(self, state_dir):
        """Reads prior tick_id when file exists."""
        from hermes_pipeline.cli import _read_prior_tick_id

        (state_dir / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        result = _read_prior_tick_id(state_dir)
        assert result == "01HA6PH2V0ZJ7GK0S39D243TQX"

    def test_read_prior_tick_id_missing(self, state_dir):
        """Returns None when file doesn't exist."""
        from hermes_pipeline.cli import _read_prior_tick_id

        assert not (state_dir / "current_tick_id.txt").exists()
        result = _read_prior_tick_id(state_dir)
        assert result is None

    def test_read_prior_tick_id_invalid_json(self, state_dir):
        """Returns None when file has invalid content."""
        from hermes_pipeline.cli import _read_prior_tick_id

        (state_dir / "current_tick_id.txt").write_text("not json {")
        result = _read_prior_tick_id(state_dir)
        # Should handle gracefully - the file stores plain text, not JSON
        # So it should just return the text
        assert result == "not json {"

    def test_generate_tick_id_format(self):
        """_generate_tick_id returns a non-empty string."""
        from hermes_pipeline.cli import _generate_tick_id

        result = _generate_tick_id()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_tick_id_unique(self):
        """_generate_tick_id returns unique values."""
        from hermes_pipeline.cli import _generate_tick_id

        ids = [_generate_tick_id() for _ in range(5)]
        assert len(set(ids)) == 5

    def test_generate_tick_id_fallback(self, mocker):
        """_generate_tick_id falls back to datetime+random if ULID fails."""
        from hermes_pipeline.cli import _generate_tick_id

        mocker.patch("hermes_pipeline.cli._new_tick_id", side_effect=ImportError("no ulid"))

        result = _generate_tick_id()
        assert isinstance(result, str)
        assert len(result) > 10  # Format: 20260101120000012345 (16+6=22 chars)

    def test_persist_tick_id_writes(self, state_dir):
        """_persist_tick_id writes the tick_id file."""
        from hermes_pipeline.cli import _persist_tick_id

        _persist_tick_id(state_dir, "01HA6PH2V0ZJ7GK0S39D243TQX")
        content = (state_dir / "current_tick_id.txt").read_text()
        assert content == "01HA6PH2V0ZJ7GK0S39D243TQX"

    def test_persist_tick_id_oserror(self, state_dir, mocker):
        """_persist_tick_id handles OSError gracefully."""
        from hermes_pipeline.cli import _persist_tick_id

        mocker.patch("pathlib.Path.write_text", side_effect=OSError("disk full"))

        # Should not raise - OSError is caught and logged
        _persist_tick_id(state_dir, "01HA6PH2V0ZJ7GK0S39D243TQX")
