"""Best-effort owned process supervision; no shell or provider output capture.

Linux process birth identities and pidfds prevent signaling reused PIDs. Other
platforms fail closed. Discovery cannot guarantee capture of a descendant which
escapes its session and ancestry between samples; this is not a containment
boundary. Missing observations or unverifiable known owners block cleanup.
"""

from __future__ import annotations

import ctypes
import math
import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

Identity = dict[str, object]
_IDENTITY_KEYS = ("pid", "start_ticks", "boot_id", "host")


class ProcessLaunchError(RuntimeError):
    """The client was provably not created; no process cleanup is outstanding."""

    def __init__(self):
        super().__init__("client_not_launched")
        self.cleanup = "confirmed"
        self.processes = []


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


def process_snapshot(pid: int) -> Identity | None:
    """Return a verified birth identity, or None only for a disappeared process.

    Permission errors and unsupported identity mechanisms intentionally propagate.
    """
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    return {
        "pid": pid, "start_ticks": int(fields[19]), "boot_id": boot,
        "host": socket.gethostname(), "state": fields[0], "ppid": int(fields[1]),
        "pgrp": int(fields[2]), "session": int(fields[3]),
    }


def _same(left: Identity, right: Identity) -> bool:
    return all(key in left and left[key] == right.get(key) for key in _IDENTITY_KEYS)


def _discover(known: dict[int, Identity]) -> bool:
    """Capture descendants and members of still-verified owned sessions."""
    snapshots = {}
    try:
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                snapshot = process_snapshot(int(entry.name))
                if snapshot is not None:
                    snapshots[int(entry.name)] = snapshot
    except (OSError, ValueError, IndexError):
        return False
    live = {pid for pid, old in known.items() if pid in snapshots and _same(old, snapshots[pid])}
    sessions = {pid for pid in live if snapshots[pid]["session"] == pid}
    former_sessions = {pid for pid, old in known.items() if old.get("session") == pid} - sessions
    if any(snapshot["session"] in former_sessions and pid not in live
           for pid, snapshot in snapshots.items()):
        # A leader can exit before its child is observed. Do not assert cleanup
        # or adopt an unverified orphan solely from a historical numeric SID.
        return False
    changed = True
    while changed:
        changed = False
        for pid, snapshot in snapshots.items():
            if pid not in live and (snapshot["ppid"] in live or snapshot["session"] in sessions):
                if pid in known and not _same(known[pid], snapshot):
                    return False
                relation = "ppid" if snapshot["ppid"] in live else "session"
                anchor_pid = snapshot[relation]
                try:
                    # Read the child, then reverify its previously sampled
                    # ancestry anchor. A numeric PPID/SID from a non-atomic
                    # /proc scan is insufficient authority to own a process.
                    current = process_snapshot(pid)
                    anchor = process_snapshot(anchor_pid)
                except (OSError, ValueError, IndexError):
                    return False
                if (current is None or anchor is None
                        or not _same(snapshot, current)
                        or not _same(known[anchor_pid], anchor)
                        or current[relation] != anchor_pid
                        or current["start_ticks"] < anchor["start_ticks"]):
                    return False
                known[pid] = snapshot
                live.add(pid)
                changed = True
    return True


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


def _signal(identity: Identity, sig: int) -> bool:
    """Pin kernel identity before signaling, closing the verify/kill race."""
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
    on_processes: Callable[[list[Identity]], None] | None = None,
) -> dict:
    """Terminate verified owners, including stopped processes, within the allowance.

    The caller remains the only collector of its direct child's exit status.
    Recovery callers never infer client success from process disappearance.
    """
    if not math.isfinite(cleanup_timeout) or not 0 <= cleanup_timeout <= 60:
        raise ValueError("cleanup_timeout must be between zero and 60 seconds")
    known = {item.get("pid"): dict(item) for item in identities}
    uncertain = len(known) != len(identities)
    started = time.monotonic()
    deadline = started + cleanup_timeout
    graceful_end = started + min(5, cleanup_timeout / 2)
    sent = set()
    while True:
        previous = len(known)
        uncertain |= not _discover(known)
        if on_processes is not None and len(known) != previous:
            try:
                on_processes(list(known.values()))
            except Exception:
                # Failed evidence persistence must not interrupt termination.
                uncertain = True
        living = []
        for identity in known.values():
            live = _live(identity)
            uncertain |= live is None
            if live:
                living.append(identity)
        if not living:
            break
        now = time.monotonic()
        sig = signal.SIGKILL if now >= graceful_end else signal.SIGTERM
        for identity in reversed(living):
            key = (identity["pid"], sig)
            if key not in sent:
                uncertain |= not _signal(identity, sig)
                if sig == signal.SIGTERM:
                    uncertain |= not _signal(identity, signal.SIGCONT)
                sent.add(key)
        if now >= deadline:
            uncertain = True
            break
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    return {"cleanup": "cleanup_unconfirmed" if uncertain else "confirmed",
            "processes": list(known.values())}


def _cleanup_owned_child(
    child: subprocess.Popen, fd: int | None, deadline: float,
    identity: Identity | None = None,
) -> None:
    """Use the unreaped child's retained kernel handle if birth lookup failed.

    This does not establish descendant cleanup or create a durable birth
    receipt. The original exception still reaches the supervisor, which must
    retain an interrupted attempt with unconfirmed cleanup.
    """
    if (fd is None and identity is None) or child.poll() is not None:
        return

    def send(sig: int) -> None:
        if fd is not None:
            _pidfd_signal(fd, sig)
        elif identity is not None and _live(identity) is True and child.poll() is None:
            # This function is the exclusive collector of this direct child.
            # While unreaped, its PID cannot be recycled; Popen.send_signal
            # checks again before sending. Birth evidence is still mandatory
            # for this fallback; never apply it to discovered descendants.
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


def run_process(
    argv: Sequence[str], *, cwd: Path, stdin_bytes: bytes, timeout: float,
    cleanup_timeout: float = 60,
    env: dict[str, str] | None = None,
    deadline_monotonic: float | None = None,
    pass_fds: tuple[int, ...] = (),
    on_launch: Callable[[dict], None] | None = None,
    on_processes: Callable[[list[Identity]], None] | None = None,
) -> dict:
    """Launch one internally constructed argv and exclusively collect its exit.

    Callbacks must synchronously persist receipts. Callback failure cleans up
    before propagating; no output or arbitrary provider exception is persisted.
    The deadline starts immediately before launch, conservatively charging spawn
    overhead. Exit eligibility is based on observation before that deadline.
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
    # Establish support before admitting any external process.
    try:
        if process_snapshot(os.getpid()) is None:
            raise RuntimeError("process_identity_unconfirmed")
        probe = _pidfd_open(os.getpid())
        try:
            _pidfd_signal(probe, 0)
        finally:
            os.close(probe)
    except (OSError, RuntimeError, ValueError):
        raise ProcessLaunchError() from None
    started = time.monotonic()
    deadline = started + timeout
    if deadline_monotonic is not None:
        deadline = min(deadline, deadline_monotonic)
    if deadline <= started:
        raise ProcessLaunchError()
    try:
        child = subprocess.Popen(
            list(argv), cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, env=env, pass_fds=pass_fds,
        )
    except OSError:
        raise ProcessLaunchError() from None
    known = {}
    child_fd = None
    cleanup = {"cleanup": "cleanup_unconfirmed", "processes": []}
    uncertain = False
    try:
        # Before polling/waiting, this direct child has not been reaped and its
        # PID cannot be reused. Retain the kernel handle before /proc lookup,
        # so even a denied birth-identity read cannot strand the direct child.
        try:
            child_fd = _pidfd_open(child.pid)
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
            previous = len(known)
            uncertain |= not _discover(known)
            if on_processes and len(known) != previous:
                on_processes(list(known.values()))
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
                on_processes=on_processes,
            )
            # Never block beyond cleanup; a live child leaves exit unobservable.
            exit_code = child.poll()
        finally:
            if child_fd is not None:
                os.close(child_fd)
    if uncertain or not known:
        cleanup["cleanup"] = "cleanup_unconfirmed"
    return {
        "outcome": outcome, "exit_code": exit_code,
        "signal": -exit_code if exit_code is not None and exit_code < 0 else None,
        "cleanup": cleanup["cleanup"], "processes": cleanup["processes"],
        "launched_monotonic": started, "deadline": deadline,
    }
