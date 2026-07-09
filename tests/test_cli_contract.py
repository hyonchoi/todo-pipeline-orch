"""Tests for the init/doctor subcommands (pipeline execution contract)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pipeline.cli import build_parser, _cmd_init
from hermes_pipeline.config import Config


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _create_project(projects_dir, name):
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "TODOS.md").write_text("# TODOS\n")
    return project_dir


class TestBuildParserInit:
    def test_init_help(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["init", "--help"])

    def test_init_parses_project_and_force(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo", "--force"])
        assert args.command == "init"
        assert args.project == "demo"
        assert args.force is True

    def test_init_force_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo"])
        assert args.force is False


class TestCmdInit:
    def test_init_unknown_project_returns_2(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = Config(projects_dir=projects_dir)
        result = _cmd_init(FakeArgs(project="nope", force=False), config)
        assert result == 2

    def test_init_writes_default_contract(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert contract.is_file()
        assert "Wrote pipeline execution contract" in capsys.readouterr().out

    def test_init_idempotent_without_force(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        _cmd_init(FakeArgs(project="demo", force=False), config)
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        contract.write_text('schema_version = 1\nassignee = "custom"\n')

        result = _cmd_init(FakeArgs(project="demo", force=False), config)

        assert result == 0
        assert "already exists" in capsys.readouterr().out
        assert 'assignee = "custom"' in contract.read_text()

    def test_init_force_overwrites(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        _cmd_init(FakeArgs(project="demo", force=False), config)
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        contract.write_text('schema_version = 1\nassignee = "custom"\n')

        result = _cmd_init(FakeArgs(project="demo", force=True), config)

        assert result == 0
        assert 'assignee = "custom"' not in contract.read_text()
        assert "schema_version = 1" in contract.read_text()
