"""Tests for the init/doctor subcommands (pipeline execution contract)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import subprocess as _test_sp

from hermes_pipeline.cli import build_parser, _cmd_init, _cmd_doctor, _cmd_install_profile
from hermes_pipeline.config import Config
from hermes_pipeline.contract import (
    PipelineContract,
    _render_contract_toml,
    bundled_profile_dir,
)
from hermes_pipeline.phases import Phase


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
        contract.write_text('schema_version = 2\nassignee = "custom"\n')

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
        contract.write_text('schema_version = 2\nassignee = "custom"\n')

        result = _cmd_init(FakeArgs(project="demo", force=True), config)

        assert result == 0
        assert 'assignee = "custom"' not in contract.read_text()
        assert "schema_version = 2" in contract.read_text()


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
            'schema_version = 2\ncapabilities = ["Read", "Write"]\n'
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
            'schema_version = 2\ncapabilities = ["Read"]\n'
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


class TestDoctorMissingProfile:
    def test_doctor_checks_profile_for_non_default_assignee(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            return_value=MagicMock(returncode=1, stderr="profile not found", stdout=""),
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        out = capsys.readouterr().out
        assert "pipeline" in out.lower() or "profile" in out.lower()

    def test_doctor_skips_profile_check_for_default_assignee(self, tmp_path, mocker, capsys):
        """When assignee is 'default', doctor should NOT check Hermes profile."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        call_count = {"n": 0}
        original_run = _test_sp.run
        def tracking_run(*a, **kw):
            call_count["n"] += 1
            cmd = a[0] if a else kw.get("args", [])
            if "profile" in cmd:
                return MagicMock(returncode=1, stderr="profile not found", stdout="")
            return original_run(*a, **kw)
        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=tracking_run)
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        assert call_count["n"] == 0

    def test_doctor_profile_check_success_returns_0(self, tmp_path, mocker, capsys):
        """Non-default assignee whose profile IS installed should pass clean."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            return_value=MagicMock(returncode=0, stderr="", stdout=""),
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        assert "OK" in capsys.readouterr().out

    def test_doctor_hermes_not_on_path_returns_2(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=FileNotFoundError("hermes"),
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "not on PATH" in capsys.readouterr().out


class TestCmdInitPatchErrors:
    def test_init_malformed_existing_toml_returns_1(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text("not valid = toml =")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee="pipeline"), config)

        assert result == 1

    def test_init_missing_schema_version_returns_1(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text('assignee = "default"\n')
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee="pipeline"), config)

        assert result == 1


class TestRenderContractToml:
    def test_render_contract_toml_roundtrips(self):
        import tomllib

        contract = PipelineContract(
            schema_version=1, assignee="pipeline", capabilities=("Read", "Write")
        )
        rendered = _render_contract_toml(contract)
        parsed = tomllib.loads(rendered)

        assert parsed["schema_version"] == 1
        assert parsed["assignee"] == "pipeline"
        assert parsed["capabilities"] == ["Read", "Write"]


class TestBundledProfileDir:
    def test_bundled_profile_dir_resolves_soul_md(self):
        profile_dir = bundled_profile_dir()
        assert (profile_dir / "SOUL.md").is_file()


class TestCmdInstallProfile:
    def test_install_profile_happy_path_returns_0(self, mocker, tmp_path, capsys):
        show_out = f"Profile: pipeline\nPath:    {tmp_path}\n"
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=[
                MagicMock(returncode=0, stderr="", stdout=""),  # create
                MagicMock(returncode=0, stderr="", stdout=show_out),  # show
            ],
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        out = capsys.readouterr().out
        assert result == 0
        assert "installed successfully" in out
        assert (tmp_path / "SOUL.md").is_file()

    def test_install_profile_force_deletes_existing_first(self, mocker, tmp_path):
        show_out = f"Profile: pipeline\nPath:    {tmp_path}\n"
        run_mock = mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=[
                MagicMock(returncode=0, stderr="", stdout=""),  # delete
                MagicMock(returncode=0, stderr="", stdout=""),  # create
                MagicMock(returncode=0, stderr="", stdout=show_out),  # show
            ],
        )

        _cmd_install_profile(FakeArgs(force=True), config=None)

        delete_call = run_mock.call_args_list[0]
        assert delete_call.args[0][:3] == ["hermes", "profile", "delete"]
        create_call = run_mock.call_args_list[1]
        assert create_call.args[0][:3] == ["hermes", "profile", "create"]

    def test_install_profile_force_delete_fails_returns_2(self, mocker, capsys):
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=[
                MagicMock(returncode=1, stderr="", stdout=""),  # delete fails, no stderr
            ],
        )

        result = _cmd_install_profile(FakeArgs(force=True), config=None)

        assert result == 2
        out = capsys.readouterr().out
        assert "Problem: `hermes profile delete` failed" in out

    def test_install_profile_soul_missing_returns_1(self, mocker, tmp_path, caplog):
        mocker.patch(
            "hermes_pipeline.contract.bundled_profile_dir", return_value=tmp_path / "nonexistent"
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 1

    def test_install_profile_hermes_not_on_path_returns_2(self, mocker, capsys):
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=FileNotFoundError("hermes"),
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 2
        assert "not found" in capsys.readouterr().out

    def test_install_profile_create_command_fails_returns_2(self, mocker, capsys):
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            return_value=MagicMock(returncode=1, stderr="boom", stdout=""),
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 2
        assert "failed" in capsys.readouterr().out

    def test_install_profile_verify_fails_returns_1(self, mocker, capsys):
        create_ok = MagicMock(returncode=0, stderr="", stdout="")
        show_fail = MagicMock(returncode=1, stderr="", stdout="")
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run", side_effect=[create_ok, show_fail]
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 1
        assert "created but" in capsys.readouterr().out

    def test_install_profile_path_missing_from_show_output_returns_1(self, mocker, capsys):
        create_ok = MagicMock(returncode=0, stderr="", stdout="")
        show_no_path = MagicMock(returncode=0, stderr="", stdout="Profile: pipeline\n")
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run", side_effect=[create_ok, show_no_path]
        )

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 1
        assert "Could not determine the profile path" in capsys.readouterr().out

    def test_install_profile_soul_copy_failure_returns_1(self, mocker, tmp_path, capsys):
        show_out = f"Profile: pipeline\nPath:    {tmp_path}\n"
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=[
                MagicMock(returncode=0, stderr="", stdout=""),  # create
                MagicMock(returncode=0, stderr="", stdout=show_out),  # show
            ],
        )
        mocker.patch("hermes_pipeline.cli.shutil.copyfile", side_effect=OSError("disk full"))

        result = _cmd_install_profile(FakeArgs(force=False), config=None)

        assert result == 1
        assert "Failed to copy pipeline SOUL.md" in capsys.readouterr().out

    def test_install_profile_force_delete_hermes_not_found_returns_2(self, mocker, capsys):
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            side_effect=FileNotFoundError("hermes"),
        )

        result = _cmd_install_profile(FakeArgs(force=True), config=None)

        assert result == 2
        assert "not found" in capsys.readouterr().out
