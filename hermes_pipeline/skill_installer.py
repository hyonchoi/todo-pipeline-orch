"""Crash-recoverable installation of bundled agent skills."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_TARGET_DIRS = {"codex": ".agents/skills", "claude": ".claude/skills"}
_JOURNAL_SCHEMA = 1


class InjectedCrash(RuntimeError):
    """Test-only crash raised by the checkpoint hook."""


def _checkpoint(_point: str) -> None:
    """Crash-injection seam used by durability tests."""


def _skill_source(name: str) -> Path:
    return Path(__file__).parent / "data" / "skills" / name


def _git_toplevel() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("project scope requires a Git worktree")
    return Path(result.stdout.strip()).resolve()


def _locations(name: str, target: str, scope: str) -> dict[str, Path]:
    if name != "todo-manager" or target not in _TARGET_DIRS or scope not in {
        "user",
        "project",
    }:
        raise ValueError("unsupported skill target or scope")
    base = Path.home() if scope == "user" else _git_toplevel()
    parent = base / _TARGET_DIRS[target]
    stem = f".{name}.tpo"
    return {
        "base": base,
        "parent": parent,
        "dest": parent / name,
        "lock": parent / f"{stem}-lock",
        "journal": parent / f"{stem}-journal.json",
        "receipt": parent / f"{stem}-receipt.json",
    }


def _prepare_parent(paths: dict[str, Path]) -> None:
    current = paths["base"]
    for part in paths["parent"].relative_to(current).parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"refusing symlink install directory: {current}")
    paths["parent"].mkdir(parents=True, exist_ok=True)
    current = paths["base"]
    for part in paths["parent"].relative_to(current).parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise RuntimeError(f"unsafe install directory: {current}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda p: p.relative_to(path).as_posix()):
        relative = item.relative_to(path).as_posix().encode()
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise RuntimeError(f"unsupported filesystem entry: {item}")
        digest.update(b"D\0" if stat.S_ISDIR(info.st_mode) else b"F\0")
        digest.update(relative)
        digest.update(b"\0")
        if stat.S_ISREG(info.st_mode):
            with item.open("rb") as stream:
                for block in iter(lambda: stream.read(131072), b""):
                    digest.update(block)
    return digest.hexdigest()


def _tree_manifest(path: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for item in [path, *sorted(path.rglob("*"), key=lambda p: p.relative_to(path).as_posix())]:
        info = item.lstat()
        relative = "." if item == path else item.relative_to(path).as_posix()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"refusing symlink in rollback staging tree: {item}")
        if stat.S_ISDIR(info.st_mode):
            manifest[relative] = {"kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode):
            manifest[relative] = {
                "kind": "file",
                "mode": stat.S_IMODE(info.st_mode),
                "digest": hashlib.sha256(item.read_bytes()).hexdigest(),
            }
        else:
            raise RuntimeError(f"unsupported rollback staging entry: {item}")
    return manifest


def _validate_manifest_subset(path: Path, expected: dict[str, dict[str, Any]]) -> None:
    if not path.exists():
        return
    actual = _tree_manifest(path)
    for relative, identity in actual.items():
        if expected.get(relative) != identity:
            raise RuntimeError("rollback staging tree identity drift")


def _identity(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"refusing symlink: {path}")
    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
    if kind == "other":
        raise RuntimeError(f"unsupported filesystem object: {path}")
    result: dict[str, Any] = {
        "kind": kind,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
    }
    if kind == "directory":
        result["digest"] = _tree_digest(path)
    else:
        result["digest"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _same_file_identity(actual: dict[str, Any] | None, expected: dict[str, Any] | None) -> bool:
    return actual == expected


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    _atomic_json(path, journal)


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_dir(path.parent)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink lock: {path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise RuntimeError("another skill operation holds the target lock") from exc
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _copy_source(source: Path, stage: Path) -> dict[str, Any]:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError(f"bundled skill is unavailable: {source}")
    shutil.copytree(source, stage, symlinks=False)
    for item in stage.rglob("*"):
        if item.is_file():
            with item.open("rb") as stream:
                os.fsync(stream.fileno())
    directories = [stage, *(item for item in stage.rglob("*") if item.is_dir())]
    for directory in sorted(
        directories, key=lambda item: len(item.relative_to(stage).parts), reverse=True
    ):
        _fsync_dir(directory)
    _fsync_dir(stage.parent)
    identity = _identity(stage)
    assert identity is not None
    return identity


def _step_stage(journal_path: Path, journal: dict[str, Any]) -> None:
    source = Path(journal["source"])
    stage = Path(journal["stage"])
    if not _same_file_identity(_identity(source), journal["source_identity"]):
        raise RuntimeError("bundled skill identity drift before staging")
    if journal.get("new_identity") is not None and _same_file_identity(
        _identity(stage), journal["new_identity"]
    ):
        return
    journal["step"] = {"name": "stage", "status": "pending"}
    _write_journal(journal_path, journal)
    _checkpoint("stage:pending")
    if stage.exists() or stage.is_symlink():
        if stage.is_symlink() or not stage.is_dir():
            raise RuntimeError("staging path identity drift")
        shutil.rmtree(stage)
        _fsync_dir(stage.parent)
    new_identity = _copy_source(source, stage)
    _checkpoint("stage:copied")
    journal["new_identity"] = new_identity
    journal["step"] = {"name": "stage", "status": "done"}
    _write_journal(journal_path, journal)
    _checkpoint("stage:done")


def _step_rename(
    journal_path: Path,
    journal: dict[str, Any],
    *,
    name: str,
    source_key: str,
    destination_key: str,
    identity_key: str,
) -> None:
    source = Path(journal[source_key])
    destination = Path(journal[destination_key])
    expected = journal[identity_key]
    source_state = _identity(source)
    destination_state = _identity(destination)
    pre = _same_file_identity(source_state, expected) and destination_state is None
    post = source_state is None and _same_file_identity(destination_state, expected)
    if not (pre or post):
        raise RuntimeError(f"identity drift during {name}")
    journal["step"] = {"name": name, "status": "pending"}
    _write_journal(journal_path, journal)
    _checkpoint(f"{name}:pending")
    if pre:
        try:
            os.replace(source, destination)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise RuntimeError("cross-device rename refused") from exc
            raise
        _fsync_dir(source.parent)
        _checkpoint(f"{name}:renamed")
    if not _same_file_identity(_identity(destination), expected) or _identity(source) is not None:
        raise RuntimeError(f"identity drift after {name}")
    journal["step"] = {"name": name, "status": "done"}
    _write_journal(journal_path, journal)
    _checkpoint(f"{name}:done")


def _receipt_value(journal: dict[str, Any]) -> dict[str, Any]:
    installed = journal["new_identity"]
    return {
        "schema_version": 1,
        "skill": journal["skill"],
        "target": journal["target"],
        "scope": journal["scope"],
        "destination_digest": installed["digest"],
    }


def _receipt_text(journal: dict[str, Any]) -> str:
    return json.dumps(_receipt_value(journal), indent=2, sort_keys=True) + "\n"


def _step_receipt(journal_path: Path, journal: dict[str, Any]) -> None:
    receipt = Path(journal["receipt"])
    value = _receipt_value(journal)
    actual = _identity(receipt)
    previous = journal.get("receipt_previous_identity")
    if actual is not None and not _same_file_identity(actual, previous):
        try:
            if json.loads(receipt.read_text(encoding="utf-8")) != value:
                raise RuntimeError("receipt identity drift before update")
        except json.JSONDecodeError as exc:
            raise RuntimeError("receipt identity drift before update") from exc
    journal["step"] = {"name": "receipt", "status": "pending"}
    _write_journal(journal_path, journal)
    _checkpoint("receipt:pending")
    _atomic_json(receipt, value)
    _checkpoint("receipt:written")
    journal["receipt_identity"] = _identity(receipt)
    journal["step"] = {"name": "receipt", "status": "done"}
    _write_journal(journal_path, journal)
    _checkpoint("receipt:done")


def _commit(journal_path: Path, journal: dict[str, Any]) -> None:
    journal["committed"] = True
    journal["step"] = {"name": "commit", "status": "done"}
    _write_journal(journal_path, journal)
    _checkpoint("commit")


def _step_remove_receipt(journal_path: Path, journal: dict[str, Any]) -> None:
    receipt = Path(journal["receipt"])
    actual = _identity(receipt)
    expected = journal.get("receipt_previous_identity")
    if actual is not None and not _same_file_identity(actual, expected):
        raise RuntimeError("receipt identity drift before removal")
    journal["step"] = {"name": "receipt-remove", "status": "pending"}
    _write_journal(journal_path, journal)
    _checkpoint("receipt-remove:pending")
    if actual is not None:
        _remove_file(receipt)
    _checkpoint("receipt-remove:removed")
    journal["step"] = {"name": "receipt-remove", "status": "done"}
    _write_journal(journal_path, journal)
    _checkpoint("receipt-remove:done")


def _cleanup(journal_path: Path, journal: dict[str, Any]) -> None:
    backup = Path(journal["backup"])
    journal["step"] = {"name": "cleanup", "status": "pending"}
    _write_journal(journal_path, journal)
    _checkpoint("cleanup:pending")
    if backup.exists():
        if not _same_file_identity(_identity(backup), journal.get("old_identity")):
            raise RuntimeError("backup identity drift before cleanup")
        shutil.rmtree(backup)
        _fsync_dir(backup.parent)
    stage = Path(journal["stage"])
    if stage.exists():
        if not _same_file_identity(_identity(stage), journal.get("new_identity")):
            raise RuntimeError("stage identity drift before cleanup")
        shutil.rmtree(stage)
        _fsync_dir(stage.parent)
    _checkpoint("cleanup:removed")
    journal["step"] = {"name": "cleanup", "status": "done"}
    _write_journal(journal_path, journal)
    _checkpoint("cleanup:done")


def _finish_install(path: Path, journal: dict[str, Any]) -> None:
    if journal.get("new_identity") is None:
        _step_stage(path, journal)
    dest = Path(journal["dest"])
    backup = Path(journal["backup"])
    if journal.get("old_identity") is not None and _identity(backup) is None:
        if _same_file_identity(_identity(dest), journal["old_identity"]):
            _step_rename(
                path,
                journal,
                name="backup",
                source_key="dest",
                destination_key="backup",
                identity_key="old_identity",
            )
        elif not _same_file_identity(_identity(dest), journal["new_identity"]):
            raise RuntimeError("destination identity drift before backup")
    if _identity(dest) is None:
        _step_rename(
            path,
            journal,
            name="activate",
            source_key="stage",
            destination_key="dest",
            identity_key="new_identity",
        )
    elif not _same_file_identity(_identity(dest), journal["new_identity"]):
        raise RuntimeError("destination identity drift before activation")
    expected_receipt = _receipt_value(journal)
    try:
        current_receipt = json.loads(Path(journal["receipt"]).read_text())
    except FileNotFoundError:
        current_receipt = None
    if current_receipt != expected_receipt:
        _step_receipt(path, journal)
    if not journal.get("committed"):
        _commit(path, journal)
    _cleanup(path, journal)
    _remove_file(path)


def _finish_uninstall(path: Path, journal: dict[str, Any]) -> None:
    if _identity(Path(journal["dest"])) is not None:
        _step_rename(
            path,
            journal,
            name="backup",
            source_key="dest",
            destination_key="backup",
            identity_key="old_identity",
        )
    if not journal.get("committed"):
        _commit(path, journal)
    receipt = Path(journal["receipt"])
    if receipt.exists():
        _step_remove_receipt(path, journal)
    _cleanup(path, journal)
    _remove_file(path)


def _load_journal(path: Path, expected: dict[str, Path]) -> dict[str, Any]:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
        raise RuntimeError("recovery journal is not a private regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("recovery journal identity changed while opening")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            data = json.load(stream)
    finally:
        os.close(fd)
    required = {
        "schema_version", "operation", "skill", "target", "scope", "dest",
        "source", "source_identity", "stage", "backup", "receipt", "old_identity", "new_identity",
        "receipt_previous", "receipt_previous_identity", "receipt_written_text",
        "receipt_rollback_path", "receipt_rollback_identity", "rollback_stage_manifest",
        "committed", "step",
    }
    if set(data) - (required | {"receipt_identity"}) or not required <= set(data):
        raise RuntimeError("recovery journal metadata is incomplete")
    if data["schema_version"] != _JOURNAL_SCHEMA:
        raise RuntimeError("unsupported recovery journal")
    for key in ("dest", "receipt"):
        if Path(data[key]) != expected[key]:
            raise RuntimeError("recovery journal target mismatch")
    for key in ("stage", "backup"):
        if Path(data[key]).parent != expected["parent"]:
            raise RuntimeError("recovery path escapes target directory")
    return data


def install(
    name: str, *, target: str, scope: str, reinstall: bool = False
) -> int:
    try:
        paths = _locations(name, target, scope)
        _prepare_parent(paths)
        with _locked(paths["lock"]):
            if paths["journal"].exists() or paths["journal"].is_symlink():
                raise RuntimeError("unfinished transaction requires `tpo skills recover`")
            old_identity = _identity(paths["dest"])
            if old_identity is not None and old_identity["kind"] != "directory":
                raise RuntimeError("existing skill destination is not a directory")
            if old_identity is not None and not reinstall:
                raise RuntimeError("skill is already installed; use --reinstall")
            token = uuid.uuid4().hex
            stage = paths["parent"] / f".{name}.stage-{token}"
            backup = paths["parent"] / f".{name}.backup-{token}"
            source = _skill_source(name)
            source_identity = _identity(source)
            if source_identity is None or source_identity["kind"] != "directory":
                raise RuntimeError(f"bundled skill is unavailable: {source}")
            receipt_previous = (
                paths["receipt"].read_text(encoding="utf-8")
                if paths["receipt"].exists()
                else None
            )
            journal = {
                "schema_version": _JOURNAL_SCHEMA,
                "operation": "install",
                "skill": name,
                "target": target,
                "scope": scope,
                "dest": str(paths["dest"]),
                "source": str(source),
                "source_identity": source_identity,
                "stage": str(stage),
                "backup": str(backup),
                "receipt": str(paths["receipt"]),
                "old_identity": old_identity,
                "new_identity": None,
                "receipt_previous": receipt_previous,
                "receipt_previous_identity": _identity(paths["receipt"]),
                "receipt_written_text": None,
                "receipt_rollback_path": None,
                "receipt_rollback_identity": None,
                "rollback_stage_manifest": None,
                "committed": False,
                "step": {"name": "prepare", "status": "done"},
            }
            journal["receipt_written_text"] = _receipt_text(
                {**journal, "new_identity": source_identity}
            )
            _write_journal(paths["journal"], journal)
            _step_stage(paths["journal"], journal)
            _finish_install(paths["journal"], journal)
        print(f"installed {name} for {target} ({scope}) at {paths['dest']}")
        return 0
    except InjectedCrash:
        raise
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Problem: {exc}")
        return 1


def uninstall(
    name: str, *, target: str, scope: str, yes: bool, force: bool = False
) -> int:
    if not yes:
        print("Problem: uninstall requires --yes")
        return 1
    try:
        paths = _locations(name, target, scope)
        _prepare_parent(paths)
        with _locked(paths["lock"]):
            if paths["journal"].exists() or paths["journal"].is_symlink():
                raise RuntimeError("unfinished transaction requires `tpo skills recover`")
            old_identity = _identity(paths["dest"])
            if old_identity is None:
                return 0
            if old_identity["kind"] != "directory":
                raise RuntimeError("existing skill destination is not a directory")
            if not force:
                if not paths["receipt"].exists():
                    raise RuntimeError("installation receipt is missing; use --force")
                receipt = json.loads(paths["receipt"].read_text())
                if receipt.get("destination_digest") != old_identity.get("digest"):
                    raise RuntimeError("installed skill differs from its receipt; use --force")
            token = uuid.uuid4().hex
            journal = {
                "schema_version": _JOURNAL_SCHEMA,
                "operation": "uninstall",
                "skill": name,
                "target": target,
                "scope": scope,
                "dest": str(paths["dest"]),
                "source": str(paths["dest"]),
                "source_identity": old_identity,
                "stage": str(paths["parent"] / f".{name}.unused-{token}"),
                "backup": str(paths["parent"] / f".{name}.backup-{token}"),
                "receipt": str(paths["receipt"]),
                "old_identity": old_identity,
                "new_identity": None,
                "receipt_previous": paths["receipt"].read_text() if paths["receipt"].exists() else None,
                "receipt_previous_identity": _identity(paths["receipt"]),
                "receipt_written_text": None,
                "receipt_rollback_path": None,
                "receipt_rollback_identity": None,
                "rollback_stage_manifest": None,
                "committed": False,
                "step": {"name": "prepare", "status": "done"},
            }
            _write_journal(paths["journal"], journal)
            _finish_uninstall(paths["journal"], journal)
        print(f"uninstalled {name} for {target} ({scope})")
        return 0
    except InjectedCrash:
        raise
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Problem: {exc}")
        return 1


def _validate_rollback_receipt(journal: dict[str, Any]) -> None:
    receipt = Path(journal["receipt"])
    actual = _identity(receipt)
    previous_identity = journal["receipt_previous_identity"]
    previous_text = journal["receipt_previous"]
    written_identity = journal.get("receipt_identity")
    written_text = journal.get("receipt_written_text")
    rollback_identity = journal.get("receipt_rollback_identity")
    if actual is None:
        if previous_identity is not None:
            raise RuntimeError("receipt disappeared before rollback")
        return
    text = receipt.read_text(encoding="utf-8")
    is_previous = (
        previous_identity is not None
        and _same_file_identity(actual, previous_identity)
        and text == previous_text
    )
    is_written = (
        written_text is not None
        and text == written_text
        and (written_identity is None or _same_file_identity(actual, written_identity))
    )
    is_rollback_replacement = (
        previous_text is not None
        and rollback_identity is not None
        and _same_file_identity(actual, rollback_identity)
        and text == previous_text
    )
    if not (is_previous or is_written or is_rollback_replacement):
        raise RuntimeError("receipt identity drift prevents rollback")


def _restore_rollback_receipt(path: Path, journal: dict[str, Any]) -> None:
    receipt = Path(journal["receipt"])
    previous = journal["receipt_previous"]
    replacement_raw = journal.get("receipt_rollback_path")
    replacement = Path(replacement_raw) if replacement_raw else None
    replacement_identity = journal.get("receipt_rollback_identity")

    if previous is not None and replacement is None:
        fd, raw_name = tempfile.mkstemp(
            prefix=f".{receipt.name}.rollback-", dir=receipt.parent
        )
        replacement = Path(raw_name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
        replacement_identity = _identity(replacement)
        journal["receipt_rollback_path"] = str(replacement)
        journal["receipt_rollback_identity"] = replacement_identity

    current = _identity(receipt)
    staged = _identity(replacement) if replacement is not None else None
    already_restored = (
        previous is None and current is None
    ) or (
        previous is not None
        and replacement_identity is not None
        and _same_file_identity(current, replacement_identity)
        and receipt.read_text(encoding="utf-8") == previous
    )
    if replacement is not None and staged is not None and not _same_file_identity(
        staged, replacement_identity
    ):
        raise RuntimeError("receipt rollback replacement identity drift")
    if not already_restored:
        _validate_rollback_receipt(journal)
        if previous is not None and not _same_file_identity(staged, replacement_identity):
            raise RuntimeError("receipt rollback replacement is unavailable")

    journal["step"] = {"name": "rollback-receipt", "status": "pending"}
    _write_journal(path, journal)
    _checkpoint("rollback-receipt:pending")
    if not already_restored:
        if previous is None:
            _remove_file(receipt)
        else:
            assert replacement is not None
            os.replace(replacement, receipt)
            _fsync_dir(receipt.parent)
    _checkpoint("rollback-receipt:replaced")
    current = _identity(receipt)
    if previous is None:
        if current is not None:
            raise RuntimeError("receipt rollback deletion did not persist")
    elif not _same_file_identity(current, replacement_identity) or receipt.read_text(
        encoding="utf-8"
    ) != previous:
        raise RuntimeError("receipt rollback replacement did not persist")
    journal["step"] = {"name": "rollback-receipt", "status": "done"}
    _write_journal(path, journal)
    _checkpoint("rollback-receipt:done")


def _cleanup_rollback_stage(path: Path, journal: dict[str, Any]) -> None:
    stage = Path(journal["stage"])
    manifest = journal.get("rollback_stage_manifest")
    if manifest is None:
        manifest = _tree_manifest(stage) if stage.exists() else {}
        journal["rollback_stage_manifest"] = manifest
    else:
        _validate_manifest_subset(stage, manifest)
    journal["step"] = {"name": "rollback-stage-cleanup", "status": "pending"}
    _write_journal(path, journal)
    _checkpoint("rollback-stage-cleanup:pending")
    if stage.exists():
        _validate_manifest_subset(stage, manifest)
        entries = sorted(
            stage.rglob("*"),
            key=lambda item: len(item.relative_to(stage).parts),
            reverse=True,
        )
        partial_checkpointed = False
        for item in entries:
            if item.is_dir():
                item.rmdir()
            else:
                item.unlink()
            _fsync_dir(item.parent)
            if not partial_checkpointed:
                partial_checkpointed = True
                _checkpoint("rollback-stage-cleanup:partial")
        stage.rmdir()
        _fsync_dir(stage.parent)
    _checkpoint("rollback-stage-cleanup:removed")
    journal["step"] = {"name": "rollback-stage-cleanup", "status": "done"}
    _write_journal(path, journal)
    _checkpoint("rollback-stage-cleanup:done")


def _rollback(path: Path, journal: dict[str, Any]) -> None:
    if journal.get("committed"):
        raise RuntimeError("transaction passed its commit point; only --finish is allowed")
    dest = Path(journal["dest"])
    stage = Path(journal["stage"])
    backup = Path(journal["backup"])
    old_identity = journal["old_identity"]
    new_identity = journal["new_identity"]
    dest_identity = _identity(dest)
    backup_identity = _identity(backup)
    stage_identity = _identity(stage)
    _validate_rollback_receipt(journal)
    if journal["operation"] == "install":
        valid_dest = dest_identity is None or (
            old_identity is not None and _same_file_identity(dest_identity, old_identity)
        ) or (
            new_identity is not None and _same_file_identity(dest_identity, new_identity)
        )
        valid_backup = backup_identity is None or (
            old_identity is not None and _same_file_identity(backup_identity, old_identity)
        )
        valid_stage = stage_identity is None or (
            new_identity is not None and _same_file_identity(stage_identity, new_identity)
        )
        # A stage copied before its identity was journaled is transaction-owned,
        # but must still be a real directory at the exact reserved path.
        cleanup_manifest = journal.get("rollback_stage_manifest")
        if cleanup_manifest is not None:
            try:
                _validate_manifest_subset(stage, cleanup_manifest)
                valid_stage = True
            except RuntimeError:
                valid_stage = False
        elif new_identity is None and stage_identity is not None:
            source_identity = journal["source_identity"]
            valid_stage = all(
                stage_identity.get(field) == source_identity.get(field)
                for field in ("kind", "mode", "digest")
            )
        if not (valid_dest and valid_backup and valid_stage):
            raise RuntimeError("filesystem identity drift prevents rollback")
        if new_identity is not None and _same_file_identity(dest_identity, new_identity):
            if stage_identity is not None:
                raise RuntimeError("staging destination occupied during rollback")
            os.replace(dest, stage)
            _fsync_dir(dest.parent)
            dest_identity = None
        if backup_identity is not None:
            if dest_identity is not None:
                raise RuntimeError("destination drift prevents rollback")
            os.replace(backup, dest)
            _fsync_dir(dest.parent)
        elif old_identity is not None and not _same_file_identity(dest_identity, old_identity):
            raise RuntimeError("original destination is unavailable for rollback")
    elif journal["operation"] == "uninstall":
        valid_dest = dest_identity is None or _same_file_identity(dest_identity, old_identity)
        valid_backup = backup_identity is None or _same_file_identity(
            backup_identity, old_identity
        )
        if not (valid_dest and valid_backup):
            raise RuntimeError("filesystem identity drift prevents rollback")
        if backup_identity is not None:
            if dest_identity is not None:
                raise RuntimeError("destination occupied during rollback")
            os.replace(backup, dest)
            _fsync_dir(dest.parent)
        elif not _same_file_identity(dest_identity, old_identity):
            raise RuntimeError("original destination is unavailable for rollback")
    else:
        raise RuntimeError("unknown recovery operation")
    _cleanup_rollback_stage(path, journal)
    _restore_rollback_receipt(path, journal)
    _remove_file(path)


def recover(
    name: str,
    *,
    target: str,
    scope: str,
    finish: bool = False,
    rollback: bool = False,
) -> int:
    if finish == rollback:
        print("Problem: choose exactly one of --finish or --rollback")
        return 1
    try:
        paths = _locations(name, target, scope)
        _prepare_parent(paths)
        with _locked(paths["lock"]):
            if not paths["journal"].is_file() or paths["journal"].is_symlink():
                raise RuntimeError("no valid recovery journal exists")
            journal = _load_journal(paths["journal"], paths)
            if journal["skill"] != name or journal["target"] != target or journal["scope"] != scope:
                raise RuntimeError("recovery journal metadata does not match the request")
            if rollback:
                _rollback(paths["journal"], journal)
            elif journal["operation"] == "install":
                _finish_install(paths["journal"], journal)
            elif journal["operation"] == "uninstall":
                _finish_uninstall(paths["journal"], journal)
            else:
                raise RuntimeError("unknown recovery operation")
        print(f"recovered {name} for {target} ({scope})")
        return 0
    except InjectedCrash:
        raise
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Problem: {exc}")
        return 1
