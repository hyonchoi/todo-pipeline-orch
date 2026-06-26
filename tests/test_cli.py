"""Tests for cli.py — merge, status, kill subcommands."""

from pathlib import Path
from datetime import datetime, timezone
import pytest

from hermes_pipeline.cli import build_parser, _cmd_status, main
from hermes_pipeline.config import Config
from hermes_pipeline.state import ReadyForReview


class TestBuildParser:
    """Test argument parser construction."""

    def test_build_parser_help(self):
        """Parser shows help for main command."""
        parser = build_parser()
        # Parser should have subcommands
        assert parser.prog == "pipeline-watch"

    def test_build_parser_merge(self):
        """Parser has 'merge' subcommand with project and todo_id."""
        parser = build_parser()
        args = parser.parse_args(["merge", "test_proj", "42"])
        assert args.command == "merge"
        assert args.project == "test_proj"
        assert args.todo_id == 42

    def test_build_parser_merge_with_abandon(self):
        """Parser 'merge' accepts --abandon flag."""
        parser = build_parser()
        args = parser.parse_args(["merge", "test_proj", "42", "--abandon"])
        assert args.command == "merge"
        assert args.abandon is True

    def test_build_parser_status(self):
        """Parser has 'status' subcommand."""
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"
        assert hasattr(args, "func")


class TestCmdStatus:
    """Test 'status' subcommand."""

    def test_cmd_status_no_records(self, tmp_path):
        """'status' shows "No pending records" when none exist."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()

        config = Config(
            projects_dir=projects_dir,
            lock_dir=lock_dir,
        )

        class Args:
            pass

        args = Args()

        # Capture output
        import io
        import sys
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result = _cmd_status(args, config)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        assert result == 0
        assert "No pending records" in output

    def test_cmd_status_with_records(self, tmp_path):
        """'status' displays pending records as table."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        lock_dir = state_dir / "pipeline_locks"
        lock_dir.mkdir(parents=True)

        # Create a project
        proj = projects_dir / "test_proj"
        proj.mkdir()
        (proj / "TODOS.md").write_text("# TODOs\n")

        # Create a ready-for-review record
        ready_dir = state_dir / "ready_for_review"
        ready_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc).isoformat()
        rec = ReadyForReview(
            project="test_proj",
            todo_id=1,
            branch="feat/test",
            pr_url="https://github.com/test/pull/1",
            phase_summaries={},
            kanban_task_id=None,
            merge_status="pending",
            created_at=now,
        )
        (ready_dir / "test_proj_1.json").write_text(rec.to_json())

        config = Config(
            projects_dir=projects_dir,
            lock_dir=lock_dir,
            state_dir=state_dir,
        )

        class Args:
            pass

        args = Args()

        # Capture output
        import io
        import sys
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result = _cmd_status(args, config)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout

        assert result == 0
        assert "test_proj" in output
        assert "pending" in output


class TestMain:
    """Test main entry point."""

    def test_main_status(self, tmp_path):
        """main() dispatches to status subcommand."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Set env vars for Config
        import os
        os.environ["PIPELINE_PROJECTS_DIR"] = str(projects_dir)
        os.environ["PIPELINE_LOCK_DIR"] = str(lock_dir)
        os.environ["PIPELINE_STATE_DIR"] = str(state_dir)

        try:
            import io
            import sys
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                result = main(["status"])
            finally:
                sys.stdout = old_stdout
            assert result == 0
        finally:
            for key in ["PIPELINE_PROJECTS_DIR", "PIPELINE_LOCK_DIR", "PIPELINE_STATE_DIR"]:
                os.environ.pop(key, None)

    def test_main_no_command(self, tmp_path):
        """main() with no command shows help."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Set env vars for Config
        import os
        os.environ["PIPELINE_PROJECTS_DIR"] = str(projects_dir)
        os.environ["PIPELINE_LOCK_DIR"] = str(lock_dir)
        os.environ["PIPELINE_STATE_DIR"] = str(state_dir)

        try:
            import io
            import sys
            old_stdout = sys.stdout
            old_argv = sys.argv
            sys.stdout = io.StringIO()
            sys.argv = ['pipeline-watch']
            try:
                result = main(None)
            finally:
                sys.stdout = old_stdout
                sys.argv = old_argv
            # Should return 0 (help is not an error)
            assert result == 0
        finally:
            for key in ["PIPELINE_PROJECTS_DIR", "PIPELINE_LOCK_DIR", "PIPELINE_STATE_DIR"]:
                os.environ.pop(key, None)
