"""Tests for the pipeline execution contract wired into the tick flow."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from hermes_pipeline.cli import _cmd_tick
from hermes_pipeline.config import Config


def _make_decision(picked):
    decision = MagicMock()
    decision.picked = picked
    decision.rationale = "test"
    decision.candidates_considered = []
    return decision


def _create_project(projects_dir, name):
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")
    return project_dir


class FakeArgs:
    def __init__(self, **kwargs):
        kwargs.setdefault("project", None)
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestTickContractAssignee:
    def test_tick_uses_contract_assignee(self, tmp_path, mocker):
        """register_todo_phases is called with the contract's assignee."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch("hermes_pipeline.cli.register_todo_phases", return_value=["t_1"])

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\nassignee = "reviewer-bot"\ncapabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["assignee"] == "reviewer-bot"

    def test_tick_no_contract_falls_back_to_default_assignee(self, tmp_path, mocker):
        """No pipeline.toml -> falls back to assignee='default', doesn't block."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch("hermes_pipeline.cli.register_todo_phases", return_value=["t_1"])

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["assignee"] == "default"

    def test_tick_capability_mismatch_skips_project_not_whole_scan(self, tmp_path, mocker):
        """A project with a capability-deficient contract is skipped, scan continues."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch("hermes_pipeline.cli.register_todo_phases")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\ncapabilities = ["Read"]\n'
        )

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0  # scan-level result: per-project errors don't abort the scan
        mock_register.assert_not_called()

    def test_tick_stale_contract_version_skips_project(self, tmp_path, mocker):
        """A contract with a stale schema_version fails closed for that project."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch("hermes_pipeline.cli.register_todo_phases")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text("schema_version = 99\n")

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        mock_register.assert_not_called()
