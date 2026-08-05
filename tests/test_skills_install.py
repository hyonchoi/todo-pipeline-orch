"""Tests for the tpo skills install subcommand and bundled skill data."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from hermes_pipeline.cli import (
    _cmd_skills_install,
    _cmd_skills_uninstall,
    _preflight_skill_replacement,
    build_parser,
)
from hermes_pipeline.config import Config
from hermes_pipeline.phases import load_profile_prerequisites

TODOS_MANAGER_DATA = files("hermes_pipeline.data").joinpath(
    "skills", "todos-manager"
)


def test_todos_manager_skill_is_packaged_data():
    """SKILL.md and sections/ are importable via importlib.resources."""
    skill_md = TODOS_MANAGER_DATA.joinpath("SKILL.md")
    assert skill_md.is_file()
    sections_dir = TODOS_MANAGER_DATA.joinpath("sections")
    section_names = {p.name for p in sections_dir.iterdir()}
    assert "schema.md" in section_names
    assert "id-assignment.md" in section_names
    assert "document-attachments.md" in section_names


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


def test_uninstall_parser_accepts_scope_target_and_yes():
    parser = build_parser()
    args = parser.parse_args(["skills", "uninstall", "--target", "all", "--scope", "project", "--yes"])
    assert args.skills_command == "uninstall"
    assert args.target == "all"
    assert args.scope == "project"
    assert args.yes is True


def test_uninstall_parser_accepts_short_yes_alias():
    args = build_parser().parse_args(["skills", "uninstall", "-y"])
    assert args.yes is True


def test_uninstall_refuses_without_yes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = tmp_path / ".claude" / "skills" / "todos-manager"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("installed", encoding="utf-8")

    result = _cmd_skills_uninstall(FakeArgs(target="claude", scope="user", yes=False), None)

    out = capsys.readouterr().out
    assert result == 1
    assert dest.exists()
    assert "Problem (claude): uninstall requires confirmation." in out


def test_uninstall_yes_removes_existing_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = tmp_path / ".claude" / "skills" / "todos-manager"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("installed", encoding="utf-8")

    result = _cmd_skills_uninstall(FakeArgs(target="claude", scope="user", yes=True), None)

    assert result == 0
    assert not dest.exists()


def test_uninstall_yes_is_a_noop_for_absent_destination(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = _cmd_skills_uninstall(FakeArgs(target="claude", scope="user", yes=True), None)

    assert result == 0
    assert "todos-manager is not installed" in capsys.readouterr().out


@pytest.mark.parametrize("dangling", [False, True], ids=["live", "dangling"])
def test_uninstall_removes_symlink_without_following_target(
    tmp_path, monkeypatch, dangling
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "target"
    if not dangling:
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
    dest = tmp_path / ".claude" / "skills" / "todos-manager"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(target, target_is_directory=True)

    result = _cmd_skills_uninstall(FakeArgs(target="claude", scope="user", yes=True), None)

    assert result == 0
    assert not dest.is_symlink()
    if not dangling:
        assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_preflight_install_probe_does_not_follow_predictable_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    install_dir = tmp_path / ".agents" / "skills"
    install_dir.mkdir(parents=True)
    (install_dir / ".tpo-install-probe-codex").symlink_to(victim)

    reason = _preflight_skill_replacement("codex", install_dir / "todos-manager")

    assert reason is None
    assert victim.read_text(encoding="utf-8") == "keep"


def test_preflight_delete_probe_does_not_follow_predictable_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    dest = tmp_path / ".claude" / "skills" / "todos-manager"
    dest.mkdir(parents=True)
    (dest / ".tpo-delete-probe-claude").symlink_to(victim)

    reason = _preflight_skill_replacement("claude", dest)

    assert reason is None
    assert victim.read_text(encoding="utf-8") == "keep"


def test_uninstall_all_preflights_before_removing_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dest = tmp_path / ".claude" / "skills" / "todos-manager"
    codex_dest = tmp_path / ".agents" / "skills" / "todos-manager"
    claude_dest.mkdir(parents=True)
    codex_dest.parent.mkdir(parents=True)
    codex_dest.write_text("not a directory", encoding="utf-8")
    (claude_dest / "SKILL.md").write_text("keep me", encoding="utf-8")

    result = _cmd_skills_uninstall(FakeArgs(target="all", scope="user", yes=True), None)

    out = capsys.readouterr().out
    assert result == 1
    assert claude_dest.exists()
    assert "Problem (codex): cannot replace todos-manager" in out


def test_uninstall_rolls_back_all_staged_destinations_when_later_rename_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dest = tmp_path / ".claude" / "skills" / "todos-manager"
    codex_dest = tmp_path / ".agents" / "skills" / "todos-manager"
    for dest, content in ((claude_dest, "claude"), (codex_dest, "codex")):
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(content, encoding="utf-8")

    real_rename = Path.rename

    def fail_codex_stage(self, destination):
        if self == codex_dest:
            raise OSError("second stage failed")
        return real_rename(self, destination)

    monkeypatch.setattr(Path, "rename", fail_codex_stage)

    result = _cmd_skills_uninstall(FakeArgs(target="all", scope="user", yes=True), None)

    assert result == 1
    assert (claude_dest / "SKILL.md").read_text(encoding="utf-8") == "claude"
    assert (codex_dest / "SKILL.md").read_text(encoding="utf-8") == "codex"
    assert not list(tmp_path.glob("**/.tpo-skill-backup-*"))


def test_uninstall_preserves_staged_backup_when_nested_cleanup_fails(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = tmp_path / ".claude" / "skills" / "todos-manager"
    nested = dest / "nested"
    nested.mkdir(parents=True)
    (nested / "locked.txt").write_text("keep", encoding="utf-8")

    def fail_nested_cleanup(path, *args, **kwargs):
        assert (Path(path) / "nested" / "locked.txt").read_text(encoding="utf-8") == "keep"
        raise PermissionError("nested directory is not writable")

    monkeypatch.setattr("shutil.rmtree", fail_nested_cleanup)

    result = _cmd_skills_uninstall(FakeArgs(target="claude", scope="user", yes=True), None)

    backups = list(dest.parent.glob(".tpo-skill-backup-*"))
    out = capsys.readouterr().out
    assert result == 1
    assert not dest.exists()
    assert len(backups) == 1
    assert (backups[0] / "nested" / "locked.txt").read_text(encoding="utf-8") == "keep"
    assert "Warning (claude): removal could not clean the staged backup" in out


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

    def test_install_target_all_without_reinstall_preflights_before_copying_first(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        codex_dest = tmp_path / ".agents" / "skills" / "todos-manager"
        codex_dest.mkdir(parents=True)
        (codex_dest / "SKILL.md").write_text("local edit", encoding="utf-8")

        result = _cmd_skills_install(
            FakeArgs(target="all", scope="user", reinstall=False), config
        )

        out = capsys.readouterr().out
        claude_dest = tmp_path / ".claude" / "skills" / "todos-manager"
        assert result == 1
        assert not claude_dest.exists()
        assert (codex_dest / "SKILL.md").read_text(encoding="utf-8") == "local edit"
        assert "Problem (codex): todos-manager is already installed" in out

    def test_install_preserves_destination_that_appears_after_preflight(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        install_dir = tmp_path / ".claude" / "skills"
        dest = install_dir / "todos-manager"
        real_mkdir = Path.mkdir

        def create_destination_at_copy_boundary(self, *args, **kwargs):
            result = real_mkdir(self, *args, **kwargs)
            if self == install_dir and not dest.exists():
                dest.mkdir()
                (dest / "SKILL.md").write_text("appeared locally", encoding="utf-8")
            return result

        monkeypatch.setattr(Path, "mkdir", create_destination_at_copy_boundary)

        result = _cmd_skills_install(
            FakeArgs(target="claude", scope="user", reinstall=False), config
        )

        assert result == 1
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == "appeared locally"

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

    def test_reinstall_target_all_restores_every_original_when_second_swap_fails(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        claude_dest = tmp_path / ".claude" / "skills" / "todos-manager"
        codex_dest = tmp_path / ".agents" / "skills" / "todos-manager"
        for dest, content in ((claude_dest, "claude local"), (codex_dest, "codex local")):
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text(content, encoding="utf-8")

        real_rename = Path.rename

        def fail_second_replacement(self, destination):
            if (
                self.name.startswith(".tpo-skill-stage-")
                and Path(destination) == codex_dest
            ):
                raise OSError("second replacement failed")
            return real_rename(self, destination)

        monkeypatch.setattr(Path, "rename", fail_second_replacement)

        result = _cmd_skills_install(
            FakeArgs(target="all", scope="user", reinstall=True), config
        )

        assert result == 1
        assert (claude_dest / "SKILL.md").read_text(encoding="utf-8") == "claude local"
        assert (codex_dest / "SKILL.md").read_text(encoding="utf-8") == "codex local"
        assert not list(tmp_path.glob("**/.tpo-skill-stage-*"))
        assert not list(tmp_path.glob("**/.tpo-skill-backup-*"))

    def test_reinstall_preserves_destination_that_appears_after_preflight(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        dest = tmp_path / ".claude" / "skills" / "todos-manager"
        real_copytree = __import__("shutil").copytree

        def create_local_destination_after_staging(*args, **kwargs):
            result = real_copytree(*args, **kwargs)
            staged = Path(args[1])
            if staged.parent == dest.parent and not dest.exists():
                dest.mkdir()
                (dest / "SKILL.md").write_text("appeared locally", encoding="utf-8")
            return result

        monkeypatch.setattr("shutil.copytree", create_local_destination_after_staging)

        result = _cmd_skills_install(
            FakeArgs(target="claude", scope="user", reinstall=True), config
        )

        assert result == 1
        assert (dest / "SKILL.md").read_text(encoding="utf-8") == "appeared locally"
        assert not list(tmp_path.glob("**/.tpo-skill-stage-*"))
        assert not list(tmp_path.glob("**/.tpo-skill-backup-*"))

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

    def test_profile_prerequisites_do_not_expand_package_installer_scope(
        self, tmp_path, monkeypatch
    ):
        metadata = load_profile_prerequisites("gstack")
        assert {"autoplan", "review", "ship"} <= {
            item.skill_id for item in metadata.skills
        }
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(
            FakeArgs(target="all", scope="user"), config
        )

        assert result == 0
        for root in (
            tmp_path / ".claude" / "skills",
            tmp_path / ".agents" / "skills",
        ):
            assert {path.name for path in root.iterdir()} == {"todos-manager"}
            for external_skill in ("autoplan", "review", "ship"):
                assert not (root / external_skill).exists()

    def test_scope_project_uses_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(FakeArgs(target="claude", scope="project"), config)

        assert result == 0
        assert (tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_project_install_matches_packaged_skill_byte_for_byte(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(
            FakeArgs(target="codex", scope="project"), config
        )

        assert result == 0
        installed = tmp_path / ".agents" / "skills" / "todos-manager"
        assert (installed / "SKILL.md").read_bytes() == (
            TODOS_MANAGER_DATA.joinpath("SKILL.md").read_bytes()
        )
        packaged_sections = TODOS_MANAGER_DATA.joinpath("sections")
        packaged_names = {
            path.name for path in packaged_sections.iterdir() if path.name.endswith(".md")
        }
        installed_names = {
            path.name for path in (installed / "sections").iterdir() if path.suffix == ".md"
        }
        assert installed_names == packaged_names
        for name in sorted(packaged_names):
            assert (installed / "sections" / name).read_bytes() == (
                packaged_sections.joinpath(name).read_bytes()
            )

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
