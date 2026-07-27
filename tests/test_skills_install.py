"""Tests for the tpo skills install subcommand and bundled skill data."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from hermes_pipeline.cli import _cmd_skills_install, build_parser
from hermes_pipeline.config import Config


def test_todos_manager_skill_is_packaged_data():
    """SKILL.md and sections/ are importable via importlib.resources."""
    data_root = files("hermes_pipeline.data")
    skill_md = data_root.joinpath("skills", "todos-manager", "SKILL.md")
    assert skill_md.is_file()
    sections_dir = data_root.joinpath("skills", "todos-manager", "sections")
    section_names = {p.name for p in sections_dir.iterdir()}
    assert "schema.md" in section_names
    assert "id-assignment.md" in section_names


class FakeArgs:
    def __init__(self, **kwargs):
        kwargs.setdefault("target", "claude")
        kwargs.setdefault("scope", "user")
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestSkillsInstallParsing:
    def test_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["skills", "install"])
        assert args.target == "claude"
        assert args.scope == "user"
        assert hasattr(args, "func")

    def test_target_all_scope_project(self):
        parser = build_parser()
        args = parser.parse_args(["skills", "install", "--target", "all", "--scope", "project"])
        assert args.target == "all"
        assert args.scope == "project"

    def test_install_accepts_reinstall_flag(self):
        parser = build_parser()
        args = parser.parse_args(["skills", "install", "--reinstall"])
        assert args.reinstall is True

    def test_install_does_not_accept_force_alias(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["skills", "install", "--force"])

    def test_invalid_target_rejected(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["skills", "install", "--target", "bogus"])


class TestCmdSkillsInstall:
    def test_installs_to_claude_user_target(self, tmp_path, mocker, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        result = _cmd_skills_install(FakeArgs(target="claude", scope="user"), config)
        assert result == 0
        installed = tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md"
        assert installed.is_file()

    def test_install_existing_destination_fails_without_reinstall(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        target_dir = tmp_path / ".claude" / "skills" / "todos-manager"
        target_dir.mkdir(parents=True)
        (target_dir / "SKILL.md").write_text("local edit", encoding="utf-8")

        result = _cmd_skills_install(
            FakeArgs(target="claude", scope="user", reinstall=False), config
        )

        out = capsys.readouterr().out
        assert result == 1
        assert (target_dir / "SKILL.md").read_text(encoding="utf-8") == "local edit"
        assert "Problem (claude): todos-manager is already installed" in out
        assert "Cause: reinstalling without --reinstall would overwrite local changes." in out
        assert "Fix: rerun with `tpo skills install --target claude --scope user --reinstall`" in out

    def test_install_reinstall_replaces_existing_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        target_dir = tmp_path / ".claude" / "skills" / "todos-manager"
        target_dir.mkdir(parents=True)
        (target_dir / "SKILL.md").write_text("stale content", encoding="utf-8")

        result = _cmd_skills_install(
            FakeArgs(target="claude", scope="user", reinstall=True), config
        )

        assert result == 0
        content = (target_dir / "SKILL.md").read_text(encoding="utf-8")
        assert content != "stale content"

    def test_install_reinstall_target_all_preflights_before_removing_first(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        claude_dest = tmp_path / ".claude" / "skills" / "todos-manager"
        codex_dest = tmp_path / ".agents" / "skills" / "todos-manager"
        claude_dest.mkdir(parents=True)
        codex_dest.parent.mkdir(parents=True)
        codex_dest.write_text("not a directory", encoding="utf-8")
        (claude_dest / "SKILL.md").write_text("keep me", encoding="utf-8")

        result = _cmd_skills_install(
            FakeArgs(target="all", scope="user", reinstall=True), config
        )

        out = capsys.readouterr().out
        assert result == 1
        assert (claude_dest / "SKILL.md").read_text(encoding="utf-8") == "keep me"
        assert "Problem (codex): cannot replace todos-manager" in out

    def test_install_reinstall_target_all_preflights_missing_later_parent(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        claude_dest = tmp_path / ".claude" / "skills" / "todos-manager"
        codex_parent = tmp_path / ".agents" / "skills"
        claude_dest.mkdir(parents=True)
        (claude_dest / "SKILL.md").write_text("keep me", encoding="utf-8")

        real_mkdir = Path.mkdir

        def _reject_codex_parent(self, *args, **kwargs):
            if self == codex_parent:
                raise PermissionError("codex parent is not writable")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _reject_codex_parent)

        result = _cmd_skills_install(
            FakeArgs(target="all", scope="user", reinstall=True), config
        )

        out = capsys.readouterr().out
        assert result == 1
        assert (claude_dest / "SKILL.md").read_text(encoding="utf-8") == "keep me"
        assert "Problem (codex): cannot replace todos-manager" in out

    def test_reinstall_stages_before_replacing_existing_destination(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        target_dir = tmp_path / ".claude" / "skills" / "todos-manager"
        target_dir.mkdir(parents=True)
        skill_md = target_dir / "SKILL.md"
        skill_md.write_text("local edit", encoding="utf-8")

        def _fail_after_partial_stage(source, destination, **kwargs):
            Path(destination, "partial-file").write_text("partial", encoding="utf-8")
            raise PermissionError("nested destination failure")

        monkeypatch.setattr("shutil.copytree", _fail_after_partial_stage)

        result = _cmd_skills_install(
            FakeArgs(target="claude", scope="user", reinstall=True), config
        )

        assert result == 1
        assert skill_md.read_text(encoding="utf-8") == "local edit"
        assert not (target_dir / "partial-file").exists()

    def test_reinstall_preserves_backup_when_rollback_fails(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        target_dir = tmp_path / ".claude" / "skills" / "todos-manager"
        target_dir.mkdir(parents=True)
        (target_dir / "SKILL.md").write_text("local edit", encoding="utf-8")

        real_rename = Path.rename
        rename_calls = {"count": 0}

        def _fail_replacement_and_rollback(self, destination):
            rename_calls["count"] += 1
            if rename_calls["count"] >= 2:
                raise OSError("rename failed")
            return real_rename(self, destination)

        monkeypatch.setattr(Path, "rename", _fail_replacement_and_rollback)

        result = _cmd_skills_install(
            FakeArgs(target="claude", scope="user", reinstall=True), config
        )

        backups = list(target_dir.parent.glob(".tpo-skill-backup-*"))
        assert result == 1
        assert not target_dir.exists()
        assert len(backups) == 1
        assert (backups[0] / "SKILL.md").read_text(encoding="utf-8") == "local edit"

    def test_creates_target_directory_if_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        assert not (tmp_path / ".claude").exists()

        result = _cmd_skills_install(FakeArgs(target="claude", scope="user"), config)

        assert result == 0
        assert (tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_target_codex_installs_to_agents_skills(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(FakeArgs(target="codex", scope="user"), config)

        assert result == 0
        assert (tmp_path / ".agents" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_target_all_installs_to_both(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(FakeArgs(target="all", scope="user"), config)

        assert result == 0
        assert (tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md").is_file()
        assert (tmp_path / ".agents" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_scope_project_uses_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(FakeArgs(target="claude", scope="project"), config)

        assert result == 0
        assert (tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_permission_denied_produces_structured_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        def _raise_permission_error(*a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr("shutil.copytree", _raise_permission_error)

        result = _cmd_skills_install(FakeArgs(target="claude", scope="user"), config)

        out = capsys.readouterr().out
        assert result == 1
        assert "Problem (" in out
        assert "Cause:" in out
        assert "Fix:" in out

    def test_target_all_partial_failure_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        real_copytree = __import__("shutil").copytree
        call_count = {"n": 0}

        def _flaky_copytree(*args, **kw):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise PermissionError("denied on second target")
            return real_copytree(*args, **kw)

        monkeypatch.setattr("shutil.copytree", _flaky_copytree)

        result = _cmd_skills_install(FakeArgs(target="all", scope="user"), config)

        assert result == 1
        out = capsys.readouterr().out
        assert "claude" in out.lower()
        assert "codex" in out.lower() or "agents" in out.lower()
