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

def test_test_subcommand_timeout_default_is_86400():
    """--timeout must default large enough that it stops being the de-facto
    kill switch for healthy long test runs (raised from 3600s / 1h)."""
    from hermes_pipeline.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path"])
    assert args.timeout == 86400


