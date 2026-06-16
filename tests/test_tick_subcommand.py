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
