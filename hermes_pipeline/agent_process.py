"""Supervise directly launched processes using native birth identity and safe signals."""

from __future__ import annotations

import ctypes
import fcntl
import math
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path

Identity = dict[str, object]
_IDENTITY_KEYS = ("pid", "start_ticks", "boot_id", "host")


class ProcessLaunchError(RuntimeError):
    """The client did not launch; cleanup evidence may still require recovery."""

    def __init__(self, *, cleanup="confirmed", processes=()):
        super().__init__("client_not_launched")
        self.cleanup = cleanup
        self.processes = [dict(identity) for identity in processes]


class ProcessOwnershipError(RuntimeError):
    """Sanitized acquisition failure carrying conservative recovery evidence."""

    def __init__(self, processes: Sequence[Identity]):
        super().__init__("process_ownership_unconfirmed")
        self.processes = [dict(identity) for identity in processes]
        self.cleanup = "cleanup_unconfirmed"


def _pidfd_open(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Some supported standalone Python builds omit these bindings despite a
    # capable host kernel and libc. Use libc's named wrapper, never syscall IDs.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        operation = libc.pidfd_open
    except AttributeError:
        raise OSError("pidfd unavailable") from None
    operation.argtypes = [ctypes.c_int, ctypes.c_uint]
    operation.restype = ctypes.c_int
    result = operation(pid, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), "pidfd unavailable")
    return result


def _pidfd_signal(fd: int, sig: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, sig)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        operation = libc.pidfd_send_signal
    except AttributeError:
        raise OSError("pidfd signaling unavailable") from None
    operation.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    if operation(fd, sig, None, 0) < 0:
        raise OSError(ctypes.get_errno(), "pidfd signaling unavailable")


def _open_handle(pid: int) -> int | dict:
    if sys.platform == "darwin":
        from .agent_darwin import Backend
        backend = Backend()
        record = backend._record(pid)
        if record is None:
            raise ProcessLookupError("process disappeared")
        return {"pid": pid, "start_ticks": record["start_ticks"],
                "boot_id": backend.boot_id(), "host": socket.gethostname()}
    return _pidfd_open(pid)


def _handle_signal(handle: int | dict, sig: int) -> None:
    if isinstance(handle, dict):
        if not _signal(handle, sig):
            raise OSError("owned process signal unconfirmed")
    else:
        _pidfd_signal(handle, sig)


def _close_handle(handle: int | dict) -> None:
    if isinstance(handle, int):
        os.close(handle)


def confirm_process_capability() -> None:
    """Verify native identity and safe signaling availability before launch."""
    try:
        if process_snapshot(os.getpid()) is None:
            raise RuntimeError("process_identity_unconfirmed")
        if sys.platform != "darwin":
            probe = _pidfd_open(os.getpid())
            try:
                _pidfd_signal(probe, 0)
            finally:
                os.close(probe)
    except (OSError, RuntimeError, ValueError):
        raise ProcessLaunchError() from None


def process_snapshot(pid: int) -> Identity | None:
    """Return a verified birth identity, or None only for a disappeared process.

    Permission errors and unsupported identity mechanisms intentionally propagate.
    """
    if sys.platform == "darwin":
        from .agent_darwin import Backend
        return Backend().snapshot(pid)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    return {
        "pid": pid, "start_ticks": int(fields[19]), "boot_id": boot,
        "host": socket.gethostname(), "state": fields[0], "ppid": int(fields[1]),
        "pgrp": int(fields[2]), "session": int(fields[3]),
    }


def _same(left: Identity, right: Identity) -> bool:
    return all(key in left and left[key] == right.get(key) for key in _IDENTITY_KEYS)


def _live(identity: Identity) -> bool | None:
    """None denotes uncertainty, never permission to signal a process."""
    if not all(key in identity for key in _IDENTITY_KEYS):
        return None
    if type(identity["pid"]) is not int or identity["pid"] <= 1:
        return None
    try:
        current = process_snapshot(identity["pid"])
    except (OSError, ValueError, IndexError):
        return None
    if current is None:
        # A record from another boot/host cannot establish local cleanup.
        try:
            local = process_snapshot(os.getpid())
        except OSError:
            return None
        if any(identity[key] != local[key] for key in ("boot_id", "host")):
            return None
        return False
    if not _same(identity, current):
        return None
    return current["state"] not in {"Z", "X"}


def _signal(identity: Identity, sig: int) -> bool | None:
    """Pin kernel identity; None is pending delivery, never proof of cleanup."""
    if sys.platform == "darwin":
        from .agent_darwin import Backend
        try:
            return Backend().signal(identity, sig)
        except OSError:
            return False
    try:
        fd = _pidfd_open(identity["pid"])
    except ProcessLookupError:
        return _live(identity) is False
    except OSError:
        return False
    try:
        live = _live(identity)
        if live is False:
            return True
        if live is None:
            return False
        _pidfd_signal(fd, sig)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def cleanup_processes(
    identities: Sequence[Identity], *, cleanup_timeout: float = 60,
) -> dict:
    """Terminate verified owners, including stopped processes, within the allowance.

    The caller remains the only collector of its direct child's exit status.
    Recovery callers never infer client success from process disappearance.
    """
    if not math.isfinite(cleanup_timeout) or not 0 <= cleanup_timeout <= 60:
        raise ValueError("cleanup_timeout must be between zero and 60 seconds")
    known = [dict(item) for item in identities]
    uncertain = not known
    # Sequential launches can reuse a PID. A later durable birth receipt on
    # the same boot proves the earlier process ended, but never authorizes
    # signaling an otherwise unrecorded replacement.
    active = [identity for identity in known if not any(
        identity.get("pid") == other.get("pid")
        and all(identity.get(key) == other.get(key) for key in ("host", "boot_id"))
        and type(identity.get("start_ticks")) is int
        and type(other.get("start_ticks")) is int
        and identity["start_ticks"] < other["start_ticks"]
        for other in known
    )]
    started = time.monotonic()
    deadline = started + cleanup_timeout
    graceful_end = started + min(5, cleanup_timeout / 2)
    sent = set()
    retry_deadline = None
    while True:
        # Always allow the initial pass, including a zero-timeout request.
        # A sleep can exhaust the allowance or overshoot it, so recheck before
        # starting another observation rather than only before sleeping.
        if retry_deadline is not None and time.monotonic() >= retry_deadline:
            uncertain = True
            break
        living = []
        for identity in active:
            live = _live(identity)
            uncertain |= live is None
            if live:
                living.append(identity)
        now = time.monotonic()
        # A late observation cannot prove cleanup within this allowance.
        if retry_deadline is not None and now >= retry_deadline:
            uncertain = True
            break
        if not living:
            break
        sig = signal.SIGKILL if now >= graceful_end else signal.SIGTERM
        for identity in reversed(living):
            for action in ((sig, signal.SIGCONT) if sig == signal.SIGTERM else (sig,)):
                key = (identity["pid"], action)
                if key not in sent:
                    delivered = _signal(identity, action)
                    uncertain |= delivered is False
                    # A reverified Darwin exit/exec transition can temporarily
                    # reject audit signaling. Retry within this same deadline;
                    # only a subsequent _live(False) establishes termination.
                    if delivered is not None:
                        sent.add(key)
        if now >= deadline:
            uncertain = True
            break
        retry_deadline = deadline
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    return {"cleanup": "cleanup_unconfirmed" if uncertain else "confirmed",
            "processes": known}


def _cleanup_owned_child(
    child: subprocess.Popen, fd: int | dict | None, deadline: float,
    identity: Identity | None = None,
) -> None:
    """Use the unreaped child's retained kernel handle if birth lookup failed.

    This does not create a durable birth receipt. The original exception still
    reaches the supervisor, which must retain an interrupted attempt with
    unconfirmed cleanup.
    """
    if child.poll() is not None:
        return

    def send(sig: int) -> None:
        if fd is not None:
            _handle_signal(fd, sig)
        elif sys.platform == "darwin" and identity is not None:
            _signal(identity, sig)
        elif child.poll() is None:
            # This function is the exclusive collector of this direct child.
            # While unreaped, its PID cannot be recycled; Popen.send_signal
            # checks again before sending. This authority comes only from the
            # exclusive unreaped Popen child, never from a persisted or discovered
            # numeric PID. Failed birth acquisition remains cleanup_unconfirmed.
            child.send_signal(sig)

    for sig in (signal.SIGTERM, signal.SIGCONT):
        try:
            send(sig)
        except OSError:
            pass
    remaining = max(0, deadline - time.monotonic())
    try:
        child.wait(timeout=min(5, remaining / 2))
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        send(signal.SIGKILL)
    except OSError:
        pass
    try:
        child.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass


def _open_output(path: Path | None, stack: ExitStack) -> int:
    """Open a client output capture file, or return DEVNULL when no path is given.

    ``O_NONBLOCK`` makes an unexpected FIFO fail with ``ENXIO`` instead of
    blocking the launch before any deadline exists; the flag is cleared once
    the descriptor is confirmed to be a regular file so the child sees a
    normal blocking file.
    """
    if path is None:
        return subprocess.DEVNULL
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        0o600,
    )
    stack.callback(os.close, fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ProcessLaunchError()
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    return fd


def run_process(
    argv: Sequence[str], *, cwd: Path, stdin_bytes: bytes, timeout: float,
    cleanup_timeout: float = 60,
    env: Mapping[str, str] | None = None,
    deadline_monotonic: float | None = None,
    pass_fds: tuple[int, ...] = (),
    on_launch: Callable[[dict], None] | None = None,
    on_processes: Callable[[list[Identity]], None] | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> dict:
    """Launch one internally constructed argv and exclusively collect its exit.

    Callbacks must synchronously persist receipts. Callback failure cleans up
    before propagating; no output or arbitrary provider exception is persisted.
    The deadline starts immediately before launch, conservatively charging spawn
    overhead. Exit eligibility is based on observation before that deadline.
    Output paths must be regular files inside a TPO-owned staging directory.
    """
    if isinstance(argv, (str, bytes)) or not argv or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("argv must be a nonempty sequence of strings")
    if not isinstance(stdin_bytes, bytes):
        raise TypeError("stdin_bytes must be bytes")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if not math.isfinite(cleanup_timeout) or not 0 <= cleanup_timeout <= 60:
        raise ValueError("cleanup_timeout must be between zero and 60 seconds")
    if deadline_monotonic is not None and (not math.isfinite(deadline_monotonic) or deadline_monotonic < 0):
        raise ValueError("absolute deadline must be finite and nonnegative")
    confirm_process_capability()
    started = time.monotonic()
    deadline = started + timeout
    if deadline_monotonic is not None:
        deadline = min(deadline, deadline_monotonic)
    if deadline <= started:
        raise ProcessLaunchError()

    # The parent's capture descriptors live only until Popen has duplicated them.
    with ExitStack() as stack:
        try:
            stdout_fd = _open_output(stdout_path, stack)
            stderr_fd = _open_output(stderr_path, stack)
        except OSError:
            raise ProcessLaunchError() from None
        try:
            child = subprocess.Popen(
                list(argv), cwd=cwd, stdin=subprocess.PIPE,
                stdout=stdout_fd, stderr=stderr_fd,
                start_new_session=True, env=env, pass_fds=pass_fds,
            )
        except OSError:
            raise ProcessLaunchError() from None

    known = {}
    child_fd = None
    cleanup = {"cleanup": "cleanup_unconfirmed", "processes": []}
    launch_error = None
    try:
        # Before polling/waiting, this direct child has not been reaped and its
        # PID cannot be reused. Retain the kernel handle before /proc lookup,
        # so even a denied birth-identity read cannot strand the direct child.
        try:
            child_fd = _open_handle(child.pid)
        except OSError:
            # Still obtain and persist birth evidence. The preflight probe can
            # succeed while a later child-specific kernel handle is denied.
            pass
        try:
            identity = process_snapshot(child.pid)
        except (OSError, ValueError, IndexError):
            if child_fd is None:
                raise ProcessOwnershipError([]) from None
            raise
        if identity is None:
            raise ProcessOwnershipError([])
        known[child.pid] = identity
        if on_launch:
            on_launch({"identity": identity, "launched_monotonic": started, "deadline": deadline})
        if on_processes:
            on_processes(list(known.values()))
        if child_fd is None:
            raise ProcessOwnershipError(list(known.values()))
        os.set_blocking(child.stdin.fileno(), False)
        offset = 0
        while True:
            # Observe status first, then time: an observation past the deadline
            # cannot become success even if the eventual return code is zero.
            exit_code = child.poll()
            observed = time.monotonic()
            if observed >= deadline:
                outcome = "timed_out"
                break
            if exit_code is not None:
                outcome = "exited"
                break
            if not child.stdin.closed:
                try:
                    if offset < len(stdin_bytes):
                        offset += os.write(child.stdin.fileno(), stdin_bytes[offset:offset + 65536])
                    if offset == len(stdin_bytes):
                        child.stdin.close()
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    child.stdin.close()
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    except ProcessLaunchError as exc:
        launch_error = exc
        raise
    finally:
        cleanup_deadline = time.monotonic() + cleanup_timeout
        if child.stdin is not None:
            child.stdin.close()
        try:
            if not known or child_fd is None:
                _cleanup_owned_child(child, child_fd, cleanup_deadline, known.get(child.pid))
            cleanup = cleanup_processes(
                list(known.values()),
                cleanup_timeout=max(0, cleanup_deadline - time.monotonic()),
            )
            if launch_error is not None:
                launch_error.cleanup = cleanup["cleanup"]
                launch_error.processes = cleanup["processes"]
            # Never block beyond cleanup; a live child leaves exit unobservable.
            exit_code = child.poll()
        finally:
            if child_fd is not None:
                _close_handle(child_fd)
    if not known:
        cleanup["cleanup"] = "cleanup_unconfirmed"
    return {
        "outcome": outcome, "exit_code": exit_code,
        "signal": -exit_code if exit_code is not None and exit_code < 0 else None,
        "cleanup": cleanup["cleanup"], "processes": cleanup["processes"],
        "launched_monotonic": started, "deadline": deadline,
    }
