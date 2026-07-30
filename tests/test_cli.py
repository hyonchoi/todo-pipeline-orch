"""Tests for cli.py — test subcommand."""


import pytest

from hermes_pipeline.cli import build_parser, main


class TestBuildParser:
    """Test argument parser construction."""

    def test_build_parser_help(self):
        """Parser shows help for main command."""
        parser = build_parser()
        # Parser should have subcommands
        assert parser.prog == "tpo"


def test_main_no_command(tmp_path):
    """main() with no command shows help."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    # Set config file for projects_dir.
    import os
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"projects_dir: {projects_dir!s}\n")
    os.environ["TPO_CONFIG_FILE"] = str(config_file)

    try:
        import io
        import sys
        old_stdout = sys.stdout
        old_argv = sys.argv
        sys.stdout = io.StringIO()
        sys.argv = ['tpo']
        try:
            result = main(None)
        finally:
            sys.stdout = old_stdout
            sys.argv = old_argv
        # Should return 0 (help is not an error)
        assert result == 0
    finally:
        for key in ["TPO_CONFIG_FILE"]:
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


def test_test_subcommand_loop_help_describes_workspace_snapshot(capsys):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["test", "--help"])

    help_output = " ".join(capsys.readouterr().out.split())
    assert exc_info.value.code == 0
    assert "numbered report snapshot in the current workspace" in help_output
    assert "previous run" not in help_output


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
