from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from hermes_pipeline.cli import build_parser


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "bundle" / "todo-manager"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# bundled\n", encoding="utf-8")
    return source


def _paths(home: Path, target: str = "codex") -> tuple[Path, Path, Path, Path]:
    parent = home / (".agents/skills" if target == "codex" else ".claude/skills")
    return (
        parent / "todo-manager",
        parent / ".todo-manager.tpo-lock",
        parent / ".todo-manager.tpo-journal.json",
        parent / ".todo-manager.tpo-receipt.json",
    )


def test_parser_exposes_single_target_transactional_commands():
    parser = build_parser()
    install = parser.parse_args(
        ["skills", "install", "todo-manager", "--target", "codex", "--scope", "project"]
    )
    assert (install.skill, install.target, install.scope) == (
        "todo-manager",
        "codex",
        "project",
    )
    recover = parser.parse_args(
        ["skills", "recover", "todo-manager", "--target", "claude", "--finish"]
    )
    assert recover.finish is True
    with pytest.raises(SystemExit):
        parser.parse_args(["skills", "install", "todo-manager", "--target", "all"])


def test_install_and_uninstall_write_exact_destination_and_receipt(
    tmp_path, monkeypatch
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    assert skill_installer.install("todo-manager", target="codex", scope="user") == 0
    dest, _lock, journal, receipt = _paths(home)
    assert (dest / "SKILL.md").read_text() == "# bundled\n"
    assert json.loads(receipt.read_text())["destination_digest"]
    assert not journal.exists()

    assert skill_installer.uninstall(
        "todo-manager", target="codex", scope="user", yes=True
    ) == 0
    assert not dest.exists()
    assert not receipt.exists()
    assert not journal.exists()


def test_project_scope_uses_git_toplevel(tmp_path, monkeypatch):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    root = tmp_path / "repository"
    (root / "nested").mkdir(parents=True)
    monkeypatch.chdir(root / "nested")
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    monkeypatch.setattr(skill_installer, "_git_toplevel", lambda: root)
    assert skill_installer.install(
        "todo-manager", target="claude", scope="project"
    ) == 0
    assert (root / ".claude/skills/todo-manager/SKILL.md").is_file()


@pytest.mark.parametrize(
    "kill_at",
    [
        "backup:pending",
        "backup:renamed",
        "backup:done",
        "activate:pending",
        "activate:renamed",
        "activate:done",
        "receipt:written",
        "receipt:done",
        "commit",
        "cleanup:pending",
        "cleanup:removed",
        "stage:pending",
        "stage:copied",
        "stage:done",
    ],
)
def test_reinstall_recovers_from_each_durable_kill_point(
    tmp_path, monkeypatch, kill_at
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, receipt = _paths(home)
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("old\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)

    def kill(point: str) -> None:
        if point == kill_at:
            raise skill_installer.InjectedCrash(point)

    monkeypatch.setattr(skill_installer, "_checkpoint", kill)
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install(
            "todo-manager", target="codex", scope="user", reinstall=True
        )
    assert journal.exists()
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", finish=True
    ) == 0
    assert (dest / "SKILL.md").read_text() == "# bundled\n"
    assert receipt.exists()
    assert not journal.exists()


def test_precommit_recovery_can_rollback_but_postcommit_cannot(tmp_path, monkeypatch):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, _receipt = _paths(home)
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("old\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)

    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "activate:done"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user", reinstall=True)
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 0
    assert (dest / "SKILL.md").read_text() == "old\n"
    assert not journal.exists()

    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "commit"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user", reinstall=True)
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 1
    assert journal.exists()


def test_new_mutation_refuses_existing_journal_and_lock_contention(tmp_path, monkeypatch):
    import fcntl

    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, lock, journal, _receipt = _paths(home)
    journal.parent.mkdir(parents=True)
    journal.write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    assert skill_installer.install("todo-manager", target="codex", scope="user") == 1
    journal.unlink()
    lock.touch()
    with lock.open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert skill_installer.install("todo-manager", target="codex", scope="user") == 1
    assert not dest.exists()


def test_symlink_identity_drift_missing_metadata_and_cross_device_fail_closed(
    tmp_path, monkeypatch
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, _receipt = _paths(home)
    victim = tmp_path / "victim"
    victim.mkdir()
    dest.parent.mkdir(parents=True)
    dest.symlink_to(victim, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    assert skill_installer.install(
        "todo-manager", target="codex", scope="user", reinstall=True
    ) == 1
    assert dest.is_symlink()
    dest.unlink()

    journal.write_text(json.dumps({"schema_version": 1, "operation": "install"}))
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", finish=True
    ) == 1
    journal.unlink()

    real_replace = os.replace
    monkeypatch.setattr(
        skill_installer.os,
        "replace",
        lambda src, dst: (_ for _ in ()).throw(OSError(18, "cross-device"))
        if Path(src).name.startswith(".todo-manager.stage-")
        else real_replace(src, dst),
    )
    assert skill_installer.install("todo-manager", target="codex", scope="user") == 1
    assert not dest.exists()


def test_recovery_refuses_destination_digest_drift(tmp_path, monkeypatch):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, _receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "activate:done"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user")
    (dest / "SKILL.md").write_text("tampered\n")
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", finish=True
    ) == 1
    assert journal.exists()


def test_install_refuses_symlinked_install_parent(tmp_path, monkeypatch):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    victim = tmp_path / "victim"
    victim.mkdir()
    home.mkdir()
    (home / ".agents").symlink_to(victim, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    assert skill_installer.install("todo-manager", target="codex", scope="user") == 1
    assert not (victim / "skills").exists()


@pytest.mark.parametrize(
    "kill_at",
    [
        "backup:pending",
        "backup:renamed",
        "backup:done",
        "commit",
        "receipt-remove:pending",
        "receipt-remove:removed",
        "receipt-remove:done",
        "cleanup:removed",
    ],
)
def test_uninstall_finish_recovers_each_destructive_kill_window(
    tmp_path, monkeypatch, kill_at
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    assert skill_installer.install("todo-manager", target="codex", scope="user") == 0

    def kill(point: str) -> None:
        if point == kill_at:
            raise skill_installer.InjectedCrash(point)

    monkeypatch.setattr(skill_installer, "_checkpoint", kill)
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.uninstall(
            "todo-manager", target="codex", scope="user", yes=True
        )
    assert journal.exists()
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", finish=True
    ) == 0
    assert not dest.exists()
    assert not receipt.exists()
    assert not journal.exists()


def test_uninstall_requires_receipt_unless_force(tmp_path, monkeypatch):
    from hermes_pipeline import skill_installer

    home = tmp_path / "home"
    dest, _lock, _journal, _receipt = _paths(home)
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("local\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    assert (
        skill_installer.uninstall(
            "todo-manager", target="codex", scope="user", yes=True
        )
        == 1
    )
    assert dest.exists()
    assert (
        skill_installer.uninstall(
            "todo-manager", target="codex", scope="user", yes=True, force=True
        )
        == 0
    )


@pytest.mark.parametrize(
    "kill_at",
    [
        "stage:pending",
        "stage:copied",
        "stage:done",
        "backup:pending",
        "backup:renamed",
        "backup:done",
        "activate:pending",
        "activate:renamed",
        "activate:done",
        "receipt:pending",
        "receipt:written",
        "receipt:done",
    ],
)
def test_reinstall_rollback_recovers_every_precommit_kill_window(
    tmp_path, monkeypatch, kill_at
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, receipt = _paths(home)
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("old\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)

    def kill(point: str) -> None:
        if point == kill_at:
            raise skill_installer.InjectedCrash(point)

    monkeypatch.setattr(skill_installer, "_checkpoint", kill)
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user", reinstall=True)
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 0
    assert (dest / "SKILL.md").read_text() == "old\n"
    assert not receipt.exists()
    assert not journal.exists()


def test_rollback_refuses_destination_and_receipt_drift_and_retains_journal(
    tmp_path, monkeypatch
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "receipt:done"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user")
    receipt.write_text("{}\n")
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 1
    assert journal.exists()

    receipt.write_text(json.loads(journal.read_text())["receipt_written_text"])
    (dest / "SKILL.md").write_text("drift\n")
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 1
    assert journal.exists()


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_existing_non_directory_is_rejected_before_journal(
    tmp_path, monkeypatch, operation
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, _receipt = _paths(home)
    dest.parent.mkdir(parents=True)
    dest.write_text("not a directory\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    if operation == "install":
        result = skill_installer.install(
            "todo-manager", target="codex", scope="user", reinstall=True
        )
    else:
        result = skill_installer.uninstall(
            "todo-manager", target="codex", scope="user", yes=True, force=True
        )
    assert result == 1
    assert dest.read_text() == "not a directory\n"
    assert not journal.exists()


def test_staging_fsyncs_nested_directories_bottom_up(tmp_path, monkeypatch):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    (source / "one" / "two").mkdir(parents=True)
    (source / "one" / "two" / "data.txt").write_text("data\n")
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    seen: list[Path] = []
    real_fsync_dir = skill_installer._fsync_dir

    def record(path: Path) -> None:
        if ".stage-" in str(path):
            seen.append(path)
        real_fsync_dir(path)

    monkeypatch.setattr(skill_installer, "_fsync_dir", record)
    assert skill_installer.install("todo-manager", target="codex", scope="user") == 0
    staged = [path for path in seen if path.name in {"one", "two"} or ".stage-" in path.name]
    names = [path.name for path in staged[:3]]
    assert names == ["two", "one", staged[2].name]


@pytest.mark.parametrize(
    "rollback_kill",
    [
        "rollback-receipt:pending",
        "rollback-receipt:replaced",
        "rollback-receipt:done",
    ],
)
def test_rollback_receipt_replacement_is_crash_recoverable(
    tmp_path, monkeypatch, rollback_kill
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    assert skill_installer.install("todo-manager", target="codex", scope="user") == 0
    old_receipt = receipt.read_text()
    (source / "SKILL.md").write_text("# replacement\n")

    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "receipt:done"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user", reinstall=True)

    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == rollback_kill
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.recover(
            "todo-manager", target="codex", scope="user", rollback=True
        )
    assert journal.exists()
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 0
    assert receipt.read_text() == old_receipt
    assert (dest / "SKILL.md").read_text() == "# bundled\n"
    assert not journal.exists()


def test_rollback_refuses_drifted_receipt_replacement(tmp_path, monkeypatch):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    _dest, _lock, journal, _receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    assert skill_installer.install("todo-manager", target="codex", scope="user") == 0
    (source / "SKILL.md").write_text("# replacement\n")
    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "receipt:done"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user", reinstall=True)
    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "rollback-receipt:pending"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.recover(
            "todo-manager", target="codex", scope="user", rollback=True
        )
    replacement = Path(json.loads(journal.read_text())["receipt_rollback_path"])
    replacement.write_text("attacker replacement\n")
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 1
    assert journal.exists()
    assert replacement.exists()


@pytest.mark.parametrize("drift", ["content", "mode"])
def test_null_identity_stage_cleanup_requires_source_equivalence(
    tmp_path, monkeypatch, drift
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    _dest, _lock, journal, _receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "stage:copied"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user")
    stage = Path(json.loads(journal.read_text())["stage"])
    if drift == "content":
        (stage / "SKILL.md").write_text("drift\n")
    else:
        stage.chmod(0o700 if (stage.stat().st_mode & 0o777) != 0o700 else 0o755)
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 1
    assert journal.exists()
    assert stage.exists()


@pytest.mark.parametrize(
    "cleanup_kill",
    [
        "rollback-stage-cleanup:pending",
        "rollback-stage-cleanup:partial",
        "rollback-stage-cleanup:removed",
        "rollback-stage-cleanup:done",
    ],
)
def test_rollback_stage_cleanup_is_crash_recoverable(
    tmp_path, monkeypatch, cleanup_kill
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    (source / "nested").mkdir()
    (source / "nested" / "data.txt").write_text("data\n")
    home = tmp_path / "home"
    dest, _lock, journal, _receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "activate:done"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user")

    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == cleanup_kill
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.recover(
            "todo-manager", target="codex", scope="user", rollback=True
        )
    assert journal.exists()
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 0
    assert not dest.exists()
    assert not journal.exists()


def test_partial_rollback_stage_cleanup_rejects_replacement_drift(
    tmp_path, monkeypatch
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    (source / "nested").mkdir()
    (source / "nested" / "data.txt").write_text("data\n")
    home = tmp_path / "home"
    _dest, _lock, journal, _receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "activate:done"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.install("todo-manager", target="codex", scope="user")
    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "rollback-stage-cleanup:partial"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        skill_installer.recover(
            "todo-manager", target="codex", scope="user", rollback=True
        )
    stage = Path(json.loads(journal.read_text())["stage"])
    (stage / "replacement.txt").write_text("drift\n")
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", rollback=True
    ) == 1
    assert journal.exists()
    assert (stage / "replacement.txt").exists()


@pytest.mark.parametrize("operation", ["install", "reinstall", "uninstall"])
def test_postcommit_cleanup_resumes_after_each_removed_entry(
    tmp_path, monkeypatch, operation
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    if operation != "install":
        dest.mkdir(parents=True)
        (dest / "nested").mkdir()
        (dest / "nested" / "old.txt").write_text("old\n")
        if operation == "uninstall":
            identity = skill_installer._identity(dest)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps({"destination_digest": identity["digest"]}) + "\n"
            )

    seen = 0

    def kill(point: str) -> None:
        nonlocal seen
        if point == "cleanup:entry-removed":
            seen += 1
            if seen == 1:
                raise skill_installer.InjectedCrash(point)
        if operation == "install" and point == "cleanup:pending":
            raise skill_installer.InjectedCrash(point)

    monkeypatch.setattr(skill_installer, "_checkpoint", kill)
    with pytest.raises(skill_installer.InjectedCrash):
        if operation == "uninstall":
            skill_installer.uninstall(
                "todo-manager", target="codex", scope="user", yes=True
            )
        else:
            skill_installer.install(
                "todo-manager",
                target="codex",
                scope="user",
                reinstall=operation == "reinstall",
            )
    assert journal.exists()
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", finish=True
    ) == 0
    assert not journal.exists()
    if operation == "uninstall":
        assert not dest.exists()
    else:
        assert (dest / "SKILL.md").read_text() == "# bundled\n"


@pytest.mark.parametrize("operation", ["install", "reinstall", "uninstall"])
def test_postcommit_cleanup_rejects_replacement_after_interruption(
    tmp_path, monkeypatch, operation
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, receipt = _paths(home)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    if operation != "install":
        dest.mkdir(parents=True)
        (dest / "nested").mkdir()
        (dest / "nested" / "old.txt").write_text("old\n")
        if operation == "uninstall":
            identity = skill_installer._identity(dest)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps({"destination_digest": identity["digest"]}) + "\n"
            )

    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point
        == ("cleanup:pending" if operation == "install" else "cleanup:entry-removed")
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        if operation == "uninstall":
            skill_installer.uninstall(
                "todo-manager", target="codex", scope="user", yes=True
            )
        else:
            skill_installer.install(
                "todo-manager",
                target="codex",
                scope="user",
                reinstall=operation == "reinstall",
            )

    state = json.loads(journal.read_text())
    cleanup_path = Path(state["stage" if operation == "install" else "backup"])
    cleanup_path.mkdir(parents=True, exist_ok=True)
    (cleanup_path / "replacement.txt").write_text("attacker\n")
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", finish=True
    ) == 1
    assert journal.exists()
    assert (cleanup_path / "replacement.txt").exists()


@pytest.mark.parametrize("operation", ["reinstall", "uninstall"])
def test_postcommit_cleanup_rejects_same_manifest_root_replacement(
    tmp_path, monkeypatch, operation
):
    from hermes_pipeline import skill_installer

    source = _source(tmp_path)
    home = tmp_path / "home"
    dest, _lock, journal, receipt = _paths(home)
    dest.mkdir(parents=True)
    (dest / "nested").mkdir()
    (dest / "nested" / "old.txt").write_text("old\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(skill_installer, "_skill_source", lambda _name: source)
    if operation == "uninstall":
        identity = skill_installer._identity(dest)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"destination_digest": identity["digest"]}) + "\n")

    monkeypatch.setattr(
        skill_installer,
        "_checkpoint",
        lambda point: (_ for _ in ()).throw(skill_installer.InjectedCrash(point))
        if point == "cleanup:entry-removed"
        else None,
    )
    with pytest.raises(skill_installer.InjectedCrash):
        if operation == "uninstall":
            skill_installer.uninstall(
                "todo-manager", target="codex", scope="user", yes=True
            )
        else:
            skill_installer.install(
                "todo-manager", target="codex", scope="user", reinstall=True
            )

    state = json.loads(journal.read_text())
    cleanup_path = Path(state["backup"])
    original = cleanup_path.with_name(cleanup_path.name + ".displaced")
    cleanup_path.rename(original)
    shutil.copytree(original, cleanup_path)
    monkeypatch.setattr(skill_installer, "_checkpoint", lambda _point: None)
    assert skill_installer.recover(
        "todo-manager", target="codex", scope="user", finish=True
    ) == 1
    assert journal.exists()
    assert cleanup_path.exists()
