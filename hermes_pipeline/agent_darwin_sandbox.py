"""Deny-by-default Seatbelt verification, with no host IPC grants.

Profile primitives: Chromium sandbox/policy/mac/common.sb and OpenAI
codex-rs/sandboxing/src/seatbelt_base_policy.sbpl. Deliberately no Mach lookup,
POSIX named IPC, network, or host-process signal allowances. Anonymous Unix
socketpairs need no outbound endpoint grant (XNU bsd/kern/uipc_syscalls.c).
Native enforcement and runtime compatibility require the Darwin test suite.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .agent_execution import ExecutionError, _open_directory

_PROFILE = '''(version 1)
(deny default)
(allow process-fork)
(allow process-exec (require-not (subpath (param "AUTHORITY"))))
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow file-read* (require-not (subpath (param "AUTHORITY"))))
(allow file-write* (subpath (param "SNAPSHOT")))
(allow file-write-data (require-all (literal "/dev/null") (vnode-type CHARACTER-DEVICE)))
'''


def verification_argv(argv, snapshot, *, authority_root, worktree=None):
    """Checks inherit Seatbelt across fork/exec; only the snapshot is writable.

    macOS aliases /var and /tmp through /private. Resolve both trusted roots
    before checking disjointness and passing paths as separate -D arguments,
    so neither symlink aliases nor profile-language quoting can bypass denial.
    Temporary runtime state lives inside the disposable snapshot.
    """
    executable = shutil.which('sandbox-exec')
    if not executable:
        raise ExecutionError('checkpoint verification sandbox unavailable')
    snapshot, authority_root = Path(snapshot), Path(authority_root)
    if not snapshot.is_absolute() or not authority_root.is_absolute():
        raise ExecutionError('checkpoint sandbox authority containment invalid')
    try:
        snapshot, authority_root = snapshot.resolve(strict=True), authority_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExecutionError('checkpoint sandbox authority containment invalid') from exc
    if (snapshot == authority_root or snapshot in authority_root.parents
            or authority_root in snapshot.parents):
        raise ExecutionError('checkpoint sandbox authority containment invalid')
    for directory in (snapshot, authority_root):
        with _open_directory(directory):
            pass
    # mkdir through the no-symlink directory helper, including on repeat checks
    # where an earlier untrusted command might have replaced this directory.
    runtime = snapshot / '.tpo-runtime'
    with _open_directory(runtime, create=True):
        pass
    environment = [
        'PATH=' + os.environ.get('PATH', '/usr/bin:/bin'),
        'HOME=' + str(runtime), 'TMPDIR=' + str(runtime),
        'UV_CACHE_DIR=' + str(runtime / 'uv-cache'), 'UV_OFFLINE=1',
        'PYTHONPATH=' + str(snapshot), 'PYTHONDONTWRITEBYTECODE=1',
    ]
    if worktree is not None and (Path(worktree) / '.venv').is_dir():
        environment.extend(['UV_PROJECT_ENVIRONMENT=' + str((Path(worktree) / '.venv').resolve()),
                            'UV_NO_SYNC=1'])
    return [executable, '-p', _PROFILE, '-D', 'SNAPSHOT=' + str(snapshot),
            '-D', 'AUTHORITY=' + str(authority_root), '/usr/bin/env', '-i',
            *environment, *argv]
