"""Tests for cli.py — test subcommand."""

import pytest

from hermes_pipeline.cli import build_parser, main


class TestBuildParser:
    """Test argument parser construction."""

    def test_build_parser_help(self):
        """Parser shows help for main command."""
        parser = build_parser()
        # Parser should have subcommands
        assert parser.prog == "pipeline-watch"


def test_main_no_command(tmp_path):
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


# --- Test subcommand parsing (Task 2) ---

def test_test_subcommand_parsing():
    """Verify 'test' subcommand parses --fixture flag."""
    from hermes_pipeline.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path"])
    assert args.command == "test"
    assert args.fixture == "happy-path"

def test_test_subcommand_loop_flag():
    """Verify --loop flag is parsed."""
    from hermes_pipeline.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path", "--loop"])
    assert args.loop is True

def test_test_subcommand_phase_flag():
    """Verify --phase flag is parsed."""
    from hermes_pipeline.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path", "--phase", "phase_2_autoplan"])
    assert args.phase == "phase_2_autoplan"


def test_test_subcommand_kanban_rejects_invalid_choice(capsys):
    """Verify --kanban flag rejects invalid choice."""
    from hermes_pipeline.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["test", "--fixture", "happy-path", "--kanban", "not-a-real-choice"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err


def test_test_subcommand_kanban_defaults_to_null():
    """Verify --kanban flag defaults to null."""
    from hermes_pipeline.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path"])
    assert args.kanban == "null"


def test_test_subcommand_kanban_accepts_hermes():
    """Verify --kanban flag accepts hermes choice."""
    from hermes_pipeline.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path", "--kanban", "hermes"])
    assert args.kanban == "hermes"
