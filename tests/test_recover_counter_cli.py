"""Tests for the recover-counter CLI subcommand and --verbose/--debug flags."""
from __future__ import annotations

import os
import pytest

from hermes_pipeline.cli import build_parser, main
from hermes_pipeline.counter import COUNTER_FILE


class TestRecoverCounterSubcommand:
    """Tests for pipeline-watch recover-counter <project>."""

    def test_subcommand_parses(self):
        """recover-counter subcommand registers and parses project arg."""
        parser = build_parser()
        args = parser.parse_args(["recover-counter", "myproject"])
        assert args.command == "recover-counter"
        assert args.project == "myproject"
        assert hasattr(args, "func")

    def test_recover_counter_success(self, tmp_path):
        """Valid recover-counter call returns 0 and sets counter."""
        projects_dir = tmp_path / "projects"
        (projects_dir / "myproject").mkdir(parents=True)
        (projects_dir / "myproject" / "TODOS.md").write_text(
            "# TODOS\n- TODO-3: test\n"
        )
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        os.environ["PIPELINE_PROJECTS_DIR"] = str(projects_dir)
        os.environ["PIPELINE_STATE_DIR"] = str(state_dir)
        try:
            result = main(["recover-counter", "myproject"])
            assert result == 0
            counter_file = projects_dir / "myproject" / COUNTER_FILE
            assert counter_file.read_text() == "3"
        finally:
            for k in ("PIPELINE_PROJECTS_DIR", "PIPELINE_STATE_DIR"):
                os.environ.pop(k, None)

    def test_recover_counter_invalid_slug(self, tmp_path):
        """Invalid project slug (leading dash) returns 2."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        os.environ["PIPELINE_PROJECTS_DIR"] = str(projects_dir)
        os.environ["PIPELINE_STATE_DIR"] = str(state_dir)
        try:
            # Use -- to separate flags from positional arg (slug starting with dash)
            result = main(["recover-counter", "--", "-invalid"])
            assert result == 2
        finally:
            for k in ("PIPELINE_PROJECTS_DIR", "PIPELINE_STATE_DIR"):
                os.environ.pop(k, None)

    def test_recover_counter_missing_project(self, tmp_path):
        """Nonexistent project returns 2."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        os.environ["PIPELINE_PROJECTS_DIR"] = str(projects_dir)
        os.environ["PIPELINE_STATE_DIR"] = str(state_dir)
        try:
            result = main(["recover-counter", "nonexistent"])
            assert result == 2
        finally:
            for k in ("PIPELINE_PROJECTS_DIR", "PIPELINE_STATE_DIR"):
                os.environ.pop(k, None)

    def test_recover_counter_no_todos_md(self, tmp_path):
        """Project without TODOS.md returns 2 (FileNotFoundError)."""
        projects_dir = tmp_path / "projects"
        (projects_dir / "myproject").mkdir(parents=True)
        # No TODOS.md
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        os.environ["PIPELINE_PROJECTS_DIR"] = str(projects_dir)
        os.environ["PIPELINE_STATE_DIR"] = str(state_dir)
        try:
            result = main(["recover-counter", "myproject"])
            assert result == 2
        finally:
            for k in ("PIPELINE_PROJECTS_DIR", "PIPELINE_STATE_DIR"):
                os.environ.pop(k, None)

    def test_recover_counter_oserror(self, tmp_path):
        """recover_counter raises OSError (e.g., disk full) -> returns 2."""
        from unittest.mock import patch
        projects_dir = tmp_path / "projects"
        (projects_dir / "myproject").mkdir(parents=True)
        (projects_dir / "myproject" / "TODOS.md").write_text(
            "# TODOS\n- TODO-3: test\n"
        )
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        os.environ["PIPELINE_PROJECTS_DIR"] = str(projects_dir)
        os.environ["PIPELINE_STATE_DIR"] = str(state_dir)
        try:
            with patch("hermes_pipeline.counter.recover_counter", side_effect=OSError("disk full")):
                result = main(["recover-counter", "myproject"])
                assert result == 2
        finally:
            for k in ("PIPELINE_PROJECTS_DIR", "PIPELINE_STATE_DIR"):
                os.environ.pop(k, None)


class TestVerboseDebugFlags:
    """Tests for --verbose and --debug CLI flags via _strip_global_flags."""

    def test_strip_verbose_flag(self):
        """_strip_global_flags extracts --verbose from anywhere in arg list."""
        from hermes_pipeline.cli import _strip_global_flags
        verbose, debug, remaining = _strip_global_flags(["--verbose", "status"])
        assert verbose is True
        assert debug is False
        assert remaining == ["status"]

    def test_strip_debug_flag(self):
        """_strip_global_flags extracts --debug from anywhere in arg list."""
        from hermes_pipeline.cli import _strip_global_flags
        verbose, debug, remaining = _strip_global_flags(["--debug", "status"])
        assert verbose is False
        assert debug is True
        assert remaining == ["status"]

    def test_strip_both_flags(self):
        """_strip_global_flags extracts both --verbose and --debug."""
        from hermes_pipeline.cli import _strip_global_flags
        verbose, debug, remaining = _strip_global_flags(["--verbose", "--debug", "status"])
        assert verbose is True
        assert debug is True
        assert remaining == ["status"]

    def test_strip_flags_after_subcommand(self):
        """_strip_global_flags extracts --verbose even after subcommand."""
        from hermes_pipeline.cli import _strip_global_flags
        verbose, debug, remaining = _strip_global_flags(["tick", "myproject", "--verbose"])
        assert verbose is True
        assert debug is False
        assert remaining == ["tick", "myproject"]

    def test_neither_flag(self):
        """No flags: remaining args unchanged."""
        from hermes_pipeline.cli import _strip_global_flags
        verbose, debug, remaining = _strip_global_flags(["status"])
        assert verbose is False
        assert debug is False
        assert remaining == ["status"]

    def test_main_verbose_before_subcommand(self, tmp_path, monkeypatch):
        """main() configures logging at INFO when --verbose is before subcommand."""
        import logging
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Create a test project directory for recover-counter
        proj = projects_dir / "test-proj"
        proj.mkdir()
        (proj / "TODOS.md").write_text("# TODOs\n")
        monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setenv("PIPELINE_STATE_DIR", str(state_dir))
        # Use recover-counter instead of deleted 'status' subcommand
        result = main(["--verbose", "recover-counter", "test-proj"])
        assert result == 0
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_main_debug_after_subcommand(self, tmp_path, monkeypatch):
        """main() configures logging at DEBUG when --debug is after subcommand."""
        import logging
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Create a test project directory for recover-counter
        proj = projects_dir / "test-proj"
        proj.mkdir()
        (proj / "TODOS.md").write_text("# TODOs\n")
        monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setenv("PIPELINE_STATE_DIR", str(state_dir))
        # Use recover-counter instead of deleted 'status' subcommand
        result = main(["recover-counter", "test-proj", "--debug"])
        assert result == 0
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_main_verbose_after_subcommand(self, tmp_path, monkeypatch):
        """main() configures logging at INFO when --verbose is after subcommand."""
        import logging
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Create a test project directory for recover-counter
        proj = projects_dir / "test-proj"
        proj.mkdir()
        (proj / "TODOS.md").write_text("# TODOs\n")
        monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setenv("PIPELINE_STATE_DIR", str(state_dir))
        # Use recover-counter instead of deleted 'status' subcommand
        result = main(["recover-counter", "test-proj", "--verbose"])
        assert result == 0
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_main_debug_before_subcommand(self, tmp_path, monkeypatch):
        """main() configures logging at DEBUG when --debug is before subcommand."""
        import logging
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Create a test project directory for recover-counter
        proj = projects_dir / "test-proj"
        proj.mkdir()
        (proj / "TODOS.md").write_text("# TODOs\n")
        monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setenv("PIPELINE_STATE_DIR", str(state_dir))
        # Use recover-counter instead of deleted 'status' subcommand
        result = main(["--debug", "recover-counter", "test-proj"])
        assert result == 0
        root = logging.getLogger()
        assert root.level == logging.DEBUG
