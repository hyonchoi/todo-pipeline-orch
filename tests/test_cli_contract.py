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


class TestInitAssignee:
    def test_init_assignee_parser(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo", "--assignee", "pipeline"])
        assert args.assignee == "pipeline"

    def test_init_assignee_defaults_to_none(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo"])
        assert args.assignee is None

    def test_init_writes_assignee_flag_value(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee="pipeline"), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert 'assignee = "pipeline"' in contract.read_text()

    def test_init_without_assignee_uses_default(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee=None), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert 'assignee = "default"' in contract.read_text()


from hermes_pipeline.cli import _cmd_doctor
from hermes_pipeline.phases import Phase


class TestInstallProfileParser:
    def test_install_profile_parses_force(self):
        parser = build_parser()
        args = parser.parse_args(["install-profile", "--force"])
        assert args.command == "install-profile"
        assert args.force is True

    def test_install_profile_force_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["install-profile"])
        assert args.force is False


class TestBuildParserDoctor:
    def test_doctor_help(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["doctor", "--help"])

    def test_doctor_parses_project(self):
        parser = build_parser()
        args = parser.parse_args(["doctor", "demo"])
        assert args.command == "doctor"
        assert args.project == "demo"


class TestCmdDoctor:
    def test_doctor_unknown_project_returns_2(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = Config(projects_dir=projects_dir)
        result = _cmd_doctor(FakeArgs(project="nope"), config)
        assert result == 2

    def test_doctor_missing_contract_returns_2(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "pipeline-watch init" in capsys.readouterr().out

    def test_doctor_invalid_contract_returns_2(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text("schema_version = 99\n")
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "INVALID" in capsys.readouterr().out

    def test_doctor_clean_returns_0(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\ncapabilities = ["Read", "Write"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write")],
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        assert "OK" in capsys.readouterr().out

    def test_doctor_drift_returns_1(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\ncapabilities = ["Read"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 1
        out = capsys.readouterr().out
        assert "DRIFT" in out
        assert "Write" in out and "Bash" in out
