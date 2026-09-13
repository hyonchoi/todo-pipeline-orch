"""Private durable execution identities and fail-closed launch admission.

The supervisor retains ``store.locked(identity)`` for its entire ownership
interval. Methods nest that lock on the same thread; a second store or process
cannot admit an attempt while that descriptor is held. Retry authorization is
a control-plane operation, never an operation exposed to Hermes workers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fails closed
    fcntl = None


SCHEMA_VERSION = 3
MAX_RECORD_BYTES = 8 * 1024 * 1024
TERMINAL = {"exited", "timed_out", "interrupted", "blocked"}
STATUSES = TERMINAL | {"admitted", "running", "running_detached", "cleanup_unconfirmed", "lock_unconfirmed"}
_RECORD_FIELDS = {"version", "execution_id", "registration", "attempts", "approved_recovery"}
_REGISTRATION_FIELDS = {
    "registration_id", "plan_identity", "phase", "prompt_base64", "prompt_sha256",
    "client", "worktree", "branch", "result_contract", "timeout", "manifest",
}
_ATTEMPT_FIELDS = {
    "generation", "status", "host", "boot_id", "supervisor", "client_process",
    "started_monotonic", "deadline_monotonic", "exit_code", "exit_signal", "cleanup",
    "reason", "recovery_event", "recovery_context", "owned_processes", "owned_cgroups", "direct_processes",
}


class ExecutionError(ValueError):
    """Execution identity, persisted evidence, or admission cannot be trusted."""


class LockUnconfirmed(ExecutionError):
    """Exclusive kernel-held ownership could not be established."""


def host_boot_identity() -> dict:
    """Return boot identity; unsupported platforms cannot verify process ownership."""
    try:
        if sys.platform == "darwin":
            from .agent_darwin import Backend
            boot_id = Backend().boot_id()
        else:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot_id = None
    return {"host": socket.gethostname(), "boot_id": boot_id}


def process_identity(pid: int) -> dict:
    """Pin PID to native birth identity, host and boot, never to PID alone."""
    identity = {**host_boot_identity(), "pid": pid, "start_ticks": None}
    if type(pid) is not int or pid <= 0:
        return identity
    try:
        if sys.platform == "darwin":
            from .agent_darwin import Backend
            snapshot = Backend().snapshot(pid)
            if snapshot is not None:
                identity["start_ticks"] = snapshot["start_ticks"]
            return identity
        # comm may contain spaces and parentheses, so split after its final ')'.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        identity["start_ticks"] = int(fields[19])
    except (OSError, IndexError, ValueError):
        pass
    return identity


def identity_matches(identity: dict | None) -> bool:
    if not isinstance(identity, dict) or not identity.get("boot_id") or not identity.get("start_ticks"):
        return False
    return process_identity(identity.get("pid")) == {
        key: identity.get(key) for key in ("pid", "start_ticks", "boot_id", "host")
    }


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ExecutionError("invalid execution identity")
    return value


def _no_symlinks(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ExecutionError("symlink in execution storage path")


@contextmanager
def _open_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Resolve every directory component through no-follow directory handles."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in path.absolute().parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                # Existing entries may be leftovers from an earlier failed sync.
                os.fsync(descriptor)
            following = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        yield descriptor
    finally:
        os.close(descriptor)


def _safe_read(path: Path, *, directory_fd: int | None = None) -> bytes:
    if directory_fd is None:
        with _open_directory(path.parent) as directory:
            return _safe_read(path, directory_fd=directory)
    fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ExecutionError("execution record is not a regular file")
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ExecutionError("execution record exceeds size limit")
    return raw


def _atomic_write(path: Path, record: dict, *, directory_fd: int) -> None:
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_RECORD_BYTES:
        raise ExecutionError("execution record exceeds size limit")
    temporary = ".record-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                 dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _validate_legacy_cgroup(receipt: dict) -> None:
    if not isinstance(receipt, dict):
        raise ValueError('invalid cgroup receipt')
    fields = {'version', 'path', 'device', 'inode', 'boot_id', 'host', 'unit'} | ({'root_device', 'root_inode'} if receipt.get('version') == 2 else set())
    if set(receipt) != fields:
        raise ValueError('invalid cgroup receipt')
    unit = receipt['unit']
    if (type(receipt['version']) is not int or receipt['version'] not in (1, 2)
            or not isinstance(unit, str) or not re.fullmatch(r'tpo-[0-9a-f]{32}\.scope', unit)
            or not isinstance(receipt['path'], str)
            or any(part in ('.', '..') for part in receipt['path'].split('/'))
            or not re.fullmatch(r'/sys/fs/cgroup/user\.slice/user-[0-9]+\.slice/user@[0-9]+\.service/(?:[A-Za-z0-9_.@-]+/)*' + re.escape(unit), receipt['path'])
            or any(type(receipt[key]) is not int or receipt[key] <= 0 for key in fields & {'device', 'inode', 'root_device', 'root_inode'})
            or any(not isinstance(receipt[key], str) or not receipt[key] or len(receipt[key]) > 256 for key in ('boot_id', 'host'))):
        raise ValueError('invalid cgroup receipt')


def _validate(record: dict) -> None:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS or type(record["version"]) is not int or record["version"] not in (1, 2, SCHEMA_VERSION):
        raise ExecutionError("unsupported execution record schema")
    _identifier(record["execution_id"])
    registration = record["registration"]
    if not isinstance(registration, dict) or set(registration) != _REGISTRATION_FIELDS:
        raise ExecutionError("invalid registration schema")
    for key in ("registration_id", "plan_identity", "phase", "worktree", "branch"):
        if not isinstance(registration[key], str) or not registration[key]:
            raise ExecutionError("missing pinned registration identity")
    if not re.fullmatch(r"[0-9a-f]{64}", registration["plan_identity"]):
        raise ExecutionError("invalid pinned Plan digest")
    client = registration["client"]
    if not isinstance(client, dict) or set(client) != {"name", "tools"} or client["name"] not in {"claude", "codex"} or not isinstance(client["tools"], list) or any(not isinstance(tool, str) or not tool for tool in client["tools"]):
        raise ExecutionError("invalid pinned client configuration")
    timeout = registration["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ExecutionError("invalid phase timeout")
    if not isinstance(registration["result_contract"], dict) or not isinstance(registration["manifest"], (dict, type(None))):
        raise ExecutionError("invalid result or manifest contract")
    try:
        prompt = base64.b64decode(registration["prompt_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ExecutionError("invalid pinned prompt") from exc
    if hashlib.sha256(prompt).hexdigest() != registration["prompt_sha256"]:
        raise ExecutionError("pinned prompt digest mismatch")
    if not isinstance(record["attempts"], list):
        raise ExecutionError("invalid attempt journal")
    for generation, attempt in enumerate(record["attempts"], 1):
        if not isinstance(attempt, dict) or set(attempt) != (_ATTEMPT_FIELDS - ({"direct_processes"} if record["version"] < 3 else set()) - ({"owned_cgroups"} if record["version"] == 1 else set())) or type(attempt["generation"]) is not int or attempt["generation"] != generation:
            raise ExecutionError("invalid attempt generation or schema")
        if attempt["status"] not in STATUSES or attempt["cleanup"] not in {"pending", "confirmed", "unconfirmed"}:
            raise ExecutionError("invalid attempt outcome")
        for key in ("started_monotonic", "deadline_monotonic"):
            value = attempt[key]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ExecutionError("invalid monotonic timestamp")
        for key in ("exit_code", "exit_signal"):
            if attempt[key] is not None and type(attempt[key]) is not int:
                raise ExecutionError("invalid collected exit evidence")
        started, deadline = attempt["started_monotonic"], attempt["deadline_monotonic"]
        if started is not None and deadline is not None and deadline <= started:
            raise ExecutionError("deadline must follow client launch")
        code, signal = attempt["exit_code"], attempt["exit_signal"]
        if signal is not None and (signal <= 0 or (code is not None and code != -signal)):
            raise ExecutionError("contradictory collected exit evidence")
        if attempt["status"] == "exited" and code is None and signal is None:
            raise ExecutionError("exited requires durable collected exit evidence")
        for key in ("host", "boot_id", "reason", "recovery_event", "recovery_context"):
            if attempt[key] is not None and (not isinstance(attempt[key], str) or len(attempt[key]) > 65536):
                raise ExecutionError("invalid attempt identity or recovery context")
        if not isinstance(attempt["owned_processes"], list) or len(attempt["owned_processes"]) > 4096:
            raise ExecutionError("invalid owned process inventory")
        groups = attempt.get("owned_cgroups", [])
        if not isinstance(groups, list) or len(groups) > 2048:
            raise ExecutionError("invalid cgroup inventory")

        try:
            for group in groups:
                _validate_legacy_cgroup(group)
        except ValueError as exc:
            raise ExecutionError("invalid cgroup receipt") from exc
        units = [group["unit"] for group in groups]
        if len(set(units)) != len(units):
            raise ExecutionError("duplicate cgroup receipt")
        roots = attempt.get("direct_processes", [])
        if (not isinstance(roots, list) or len(roots) > 2048
                or any(not isinstance(root, dict) for root in roots)
                or len({json.dumps(root, sort_keys=True) for root in roots}) != len(roots)):
            raise ExecutionError("invalid direct process receipts")
        for identity in [attempt["supervisor"], attempt["client_process"], *attempt["owned_processes"], *roots]:
            if identity is None:
                continue
            core = {"pid", "start_ticks", "boot_id", "host"}
            if not isinstance(identity, dict) or not core <= set(identity) or not set(identity) <= core | {"ppid", "pgrp", "session", "state", "cgroup"}:
                raise ExecutionError("invalid process identity schema")
            if "cgroup" in identity and identity["cgroup"] not in units:
                raise ExecutionError("process cgroup is not recorded")
            if type(identity["pid"]) is not int or identity["pid"] <= 0 or any(identity[key] is not None and not isinstance(identity[key], str) for key in ("boot_id", "host")) or (identity["start_ticks"] is not None and (type(identity["start_ticks"]) is not int or identity["start_ticks"] < 0)):
                raise ExecutionError("invalid process identity")
    approval = record["approved_recovery"]
    if approval is not None and (not isinstance(approval, dict) or set(approval) != {"generation", "event_id", "recovery_context"} or type(approval["generation"]) is not int or not isinstance(approval["event_id"], str) or not approval["event_id"] or not isinstance(approval["recovery_context"], str) or len(approval["recovery_context"]) > 65536):
        raise ExecutionError("invalid recovery authorization")


class ExecutionStore:
    """Private supervisor-owned records, outside the registered worktree."""

    def __init__(self, root: Path | str):
        self.root = Path(root).absolute()
        _no_symlinks(self.root)
        self._local = threading.local()
        self._root_identity = None

    def _directory(self, execution_id: str) -> Path:
        directory = self.root / _identifier(execution_id)
        _no_symlinks(directory)
        return directory

    @contextmanager
    def _directory_handle(self, execution_id: str, *, create: bool = False) -> Iterator[int]:
        _identifier(execution_id)
        with _open_directory(self.root, create=create) as root:
            identity = os.fstat(root)
            root_identity = (identity.st_dev, identity.st_ino)
            if self._root_identity is not None and root_identity != self._root_identity:
                raise LockUnconfirmed("lock_unconfirmed: storage root was replaced")
            self._root_identity = root_identity
            if create:
                try:
                    os.mkdir(execution_id, 0o700, dir_fd=root)
                except FileExistsError:
                    pass
                os.fsync(root)
            directory = os.open(execution_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=root)
            try:
                held = getattr(self._local, "held", {})
                if execution_id in held:
                    previous = os.fstat(held[execution_id])
                    current = os.fstat(directory)
                    if (previous.st_dev, previous.st_ino) != (current.st_dev, current.st_ino):
                        raise LockUnconfirmed("lock_unconfirmed: ownership directory was replaced")
                yield directory
            finally:
                os.close(directory)

    def _write(self, execution_id: str, record: dict) -> None:
        with self._directory_handle(execution_id) as directory:
            _atomic_write(self.root / execution_id / "record.json", record, directory_fd=directory)

    @contextmanager
    def locked(self, execution_id: str) -> Iterator[None]:
        """Nonblocking ownership; retain the outer context while supervising."""
        if fcntl is None:
            raise LockUnconfirmed("lock_unconfirmed: advisory locking unsupported")
        held = getattr(self._local, "held", {})
        with ExitStack() as stack:
            try:
                fd = stack.enter_context(self._directory_handle(execution_id, create=True))
            except OSError as exc:
                raise LockUnconfirmed("lock_unconfirmed: cannot open ownership lock") from exc
            if execution_id in held:
                yield
                return
            try:
                # The directory is the stable kernel lock anchor. An owner
                # filename can be replaced without ever releasing this lock.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockUnconfirmed("lock_unconfirmed: another owner or unsupported filesystem") from exc
            held[execution_id] = fd
            self._local.held = held
            try:
                yield
            finally:
                del held[execution_id]
                fcntl.flock(fd, fcntl.LOCK_UN)

    def load(self, execution_id: str) -> dict:
        try:
            with self._directory_handle(execution_id) as directory:
                record = json.loads(_safe_read(self.root / execution_id / "record.json", directory_fd=directory))
        except (OSError, ValueError) as exc:
            raise ExecutionError("execution record unavailable or invalid") from exc
        _validate(record)
        if record["version"] < SCHEMA_VERSION:
            for attempt in record["attempts"]:
                attempt.setdefault("owned_cgroups", [])
                attempt["direct_processes"] = []
            record["version"] = SCHEMA_VERSION
        if record["execution_id"] != execution_id:
            raise ExecutionError("execution identity mismatch")
        self._outside_worktree(record["registration"]["worktree"])
        return record

    def _outside_worktree(self, worktree: str) -> None:
        if not Path(worktree).is_absolute():
            raise ExecutionError("worktree must be absolute")
        if self.root.resolve().is_relative_to(Path(worktree).resolve()):
            raise ExecutionError("execution records must be outside the execution worktree")

    def register(self, execution_id: str, *, registration_id: str, plan_identity: str,
                 phase: str, prompt: bytes, client: dict, worktree: str, branch: str,
                 result_contract: dict, timeout: float, manifest: dict | None = None) -> dict:
        self._outside_worktree(worktree)
        registration = dict(
            registration_id=registration_id, plan_identity=plan_identity, phase=phase,
            prompt_base64=base64.b64encode(prompt).decode("ascii"),
            prompt_sha256=hashlib.sha256(prompt).hexdigest(), client=client,
            worktree=worktree, branch=branch, result_contract=result_contract,
            timeout=timeout, manifest=manifest,
        )
        record = dict(version=SCHEMA_VERSION, execution_id=execution_id,
                      registration=registration, attempts=[], approved_recovery=None)
        _validate(record)
        with self.locked(execution_id):
            path = self._directory(execution_id) / "record.json"
            if path.exists() or path.is_symlink():
                existing = self.load(execution_id)
                if existing["registration"] != registration:
                    raise ExecutionError("pinned registration drift")
                return existing
            self._write(execution_id, record)
        return record

    def prompt(self, execution_id: str) -> bytes:
        return base64.b64decode(self.load(execution_id)["registration"]["prompt_base64"])

    def worktree_lock_id(self, execution_id: str) -> str:
        """Compute the lock id for a worktree without side effects."""
        worktree = Path(self.load(execution_id)["registration"]["worktree"]).resolve()
        return "worktree-" + hashlib.sha256(os.fsencode(worktree)).hexdigest()

    def assert_worktree_peers_resolved(self, execution_id: str) -> None:
        """Verify no peer executions on the worktree have unresolved attempts.

        Call this while holding the worktree lock (via locked(worktree_id)).
        """
        worktree = Path(self.load(execution_id)["registration"]["worktree"]).resolve()
        for path in self.root.glob("*/record.json"):
            if path.parent.name == execution_id:
                continue
            other = self.load(path.parent.name)
            if Path(other["registration"]["worktree"]).resolve() != worktree:
                continue
            if any(attempt["cleanup"] != "confirmed" or attempt["status"] not in TERMINAL for attempt in other["attempts"]):
                raise ExecutionError("worktree has an unresolved owned attempt")

    @contextmanager
    def worktree_locked(self, execution_id: str) -> Iterator[None]:
        """Retain alongside ``locked`` throughout the supervisor ownership interval."""
        lock_id = self.worktree_lock_id(execution_id)
        with self.locked(lock_id):
            self.assert_worktree_peers_resolved(execution_id)
            yield

    def admit(self, execution_id: str, *, recovery_event: str | None = None) -> tuple[dict, bool]:
        with self.worktree_locked(execution_id), self.locked(execution_id):
            record = self.load(execution_id)
            attempts = record["attempts"]
            if attempts:
                if recovery_event is None:
                    return record, False
                approval = record["approved_recovery"]
                if approval is None or approval["generation"] != attempts[-1]["generation"] or approval["event_id"] != recovery_event or attempts[-1]["cleanup"] != "confirmed" or attempts[-1]["status"] not in TERMINAL:
                    raise ExecutionError("retry lacks verified authorization or confirmed cleanup")
            elif recovery_event is not None:
                raise ExecutionError("recovery requires a prior attempt")
            attempts.append(dict(
                generation=len(attempts) + 1, status="admitted", **host_boot_identity(),
                supervisor=None, client_process=None, started_monotonic=None,
                deadline_monotonic=None, exit_code=None, exit_signal=None,
                cleanup="pending", reason=None, recovery_event=recovery_event,
                recovery_context=record["approved_recovery"]["recovery_context"] if record["approved_recovery"] else None,
                owned_processes=[], owned_cgroups=[], direct_processes=[],
            ))
            record["approved_recovery"] = None
            self._write(execution_id, record)
            return record, True

    def authorize_retry(self, execution_id: str, *, expected_generation: int, event_id: str,
                        recovery_context: str = "") -> None:
        """Record a verified operator event; never expose this method to workers."""
        _identifier(event_id)
        with self.locked(execution_id):
            record = self.load(execution_id)
            if not record["attempts"] or record["attempts"][-1]["generation"] != expected_generation:
                raise ExecutionError("stale recovery authorization")
            if any(attempt["recovery_event"] == event_id for attempt in record["attempts"]):
                raise ExecutionError("recovery event already consumed")
            record["approved_recovery"] = {"generation": expected_generation, "event_id": event_id,
                                           "recovery_context": recovery_context}
            _validate(record)
            self._write(execution_id, record)

    def update_attempt(self, execution_id: str, generation: int, **changes) -> dict:
        if not set(changes) <= _ATTEMPT_FIELDS - {"generation", "recovery_event", "recovery_context"}:
            raise ExecutionError("unknown or immutable attempt fields")
        with self.locked(execution_id):
            record = self.load(execution_id)
            if not record["attempts"] or record["attempts"][-1]["generation"] != generation:
                raise ExecutionError("stale attempt cannot overwrite current evidence")
            attempt = record["attempts"][-1]
            if attempt["status"] in TERMINAL and changes.get("status", attempt["status"]) != attempt["status"]:
                raise ExecutionError("terminal attempt outcome is immutable")
            for field in ("exit_code", "exit_signal", "started_monotonic", "deadline_monotonic", "supervisor", "client_process"):
                if field in changes and attempt[field] is not None and changes[field] != attempt[field]:
                    raise ExecutionError("collected exit and launch identity are immutable")
            if "owned_cgroups" in changes:
                new_groups = changes["owned_cgroups"]
                old_groups = attempt["owned_cgroups"]
                if not isinstance(new_groups, list) or new_groups[:len(old_groups)] != old_groups:
                    raise ExecutionError("cgroup ownership receipts are append-only")
            if "direct_processes" in changes:
                roots = changes["direct_processes"]
                if not isinstance(roots, list) or roots[:len(attempt["direct_processes"])] != attempt["direct_processes"]:
                    raise ExecutionError("direct process receipts are append-only")
            attempt.update(changes)
            _validate(record)
            self._write(execution_id, record)
            return record


def _logger_name(store: ExecutionStore, execution_id: str) -> str:
    store_hash = hashlib.sha256(os.fsencode(store.root.resolve())).hexdigest()[:16]
    return f"tpo.execution.{store_hash}.{execution_id}"


def execution_logger(store: ExecutionStore, execution_id: str) -> logging.Logger:
    """Per-execution logger writing to supervisor.log in the execution directory.

    Messages carry only identifiers, generation numbers, statuses, reason codes,
    monotonic seconds, and argv[0]. Never log prompt bytes, environment variables,
    or client output.
    """
    logger = logging.getLogger(_logger_name(store, execution_id))
    logger.propagate = False
    logger.setLevel(logging.INFO)

    if any(not isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        return logger

    try:
        with store._directory_handle(execution_id) as directory:
            fd = os.open(
                "supervisor.log",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                file_obj = os.fdopen(fd, "a", encoding="utf-8")
            except OSError:
                os.close(fd)
                raise
            handler = logging.StreamHandler(file_obj)
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
            formatter.converter = time.gmtime
            handler.setFormatter(formatter)
            logger.addHandler(handler)
    except (OSError, ExecutionError):
        if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
            logger.addHandler(logging.NullHandler())

    return logger


def close_execution_logger(store: ExecutionStore, execution_id: str) -> None:
    """Remove every handler from the logger, closing stream handlers' streams."""
    logger = logging.getLogger(_logger_name(store, execution_id))
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
        stream = getattr(handler, "stream", None)
        if stream is not None:
            stream.close()
