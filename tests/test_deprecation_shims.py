"""Tests for the pipeline-watch/hermes-pipeline deprecation shims (TODO-33)."""
from __future__ import annotations

import sys

from hermes_pipeline.deprecated_entry import (
    hermes_pipeline_deprecated,
    pipeline_watch_deprecated,
)


class TestDeprecationShims:
    def test_pipeline_watch_prints_warning_and_dispatches(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pipeline-watch", "--version"])
        result = pipeline_watch_deprecated()
        captured = capsys.readouterr()
        assert "pipeline-watch" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "tpo" in captured.err
        assert result == 0

    def test_hermes_pipeline_prints_warning_and_dispatches(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["hermes-pipeline", "--version"])
        result = hermes_pipeline_deprecated()
        captured = capsys.readouterr()
        assert "hermes-pipeline" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "tpo" in captured.err
        assert result == 0

    def test_pipeline_watch_forwards_args_unchanged(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pipeline-watch", "recover-counter", "myproject"])
        # No config env set up -> _resolve_project_dir logs "project not found" and
        # returns exit code 2, proving the real subcommand ran (not a no-op).
        result = pipeline_watch_deprecated()
        assert result == 2
