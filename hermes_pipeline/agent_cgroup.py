"""Linux cgroup v2 ownership via the existing delegated user manager.

The bootstrap is inert until its cgroup receipt is durable. This is process
ownership, not a sandbox: trusted same-user workloads must not migrate out.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

_MOUNT = Path('/sys/fs/cgroup')
_MAX_ENV_BYTES = 1024 * 1024
_FIELDS = {'version', 'path', 'device', 'inode', 'boot_id', 'host', 'unit'}
_BOOTSTRAP = '''import json,os,sys
ready,release,executed,environment=map(int,sys.argv[1:5])
with os.fdopen(environment,"rb") as stream: payload=stream.read(1024*1024+1)
if len(payload)>1024*1024: sys.exit(125)
client_env=json.loads(payload)
os.set_inheritable(executed,False)
path=next(line[3:] for line in open('/proc/self/cgroup').read().splitlines() if line.startswith('0::'))
os.write(ready,json.dumps({'path':path}).encode()+b'\\n')
os.close(ready)
allowed=os.read(release,1)
os.close(release)
if allowed != b'1': sys.exit(125)
try: os.execvpe(sys.argv[5],sys.argv[5:],client_env)
except (OSError,ValueError):
    os.write(executed,b'E')
    os._exit(127)
'''


def confirm_cgroup_capability() -> None:
    """Check the existing v2/user-manager route before admitting an attempt."""
    if (not (_MOUNT / 'cgroup.controllers').is_file()
            or shutil.which('systemd-run') is None or shutil.which('systemctl') is None):
        raise OSError('cgroup_v2_unavailable')
    try:
        manager = subprocess.run(
            ['systemctl', '--user', 'show', '--property=ControlGroup', '--value'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
    except subprocess.TimeoutExpired:
        raise OSError('cgroup_v2_unavailable') from None
    root = manager.stdout.strip()
    if manager.returncode or not re.fullmatch(r'/user\.slice/user-[0-9]+\.slice/user@[0-9]+\.service', root):
        raise OSError('cgroup_v2_unavailable')
    fd = _open_directory(str(_MOUNT) + root)
    try:
        # cgroup.kill requires a sufficiently recent v2 kernel. Scope creation
        # subsequently confirms delegated write access before client release.
        os.stat('cgroup.kill', dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)


def validate_receipt(receipt: dict) -> None:
    if not isinstance(receipt, dict):
        raise ValueError('invalid cgroup receipt')
    fields = _FIELDS | ({'root_device', 'root_inode'} if receipt.get('version') == 2 else set())
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


def _root_identity() -> tuple[int, int]:
    fd = os.open(_MOUNT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _same_root(receipt: dict) -> bool:
    try:
        return receipt['version'] == 2 and _root_identity() == (receipt['root_device'], receipt['root_inode'])
    except OSError:
        return False


def _open_directory(path: str) -> int:
    parts = Path(path).relative_to(_MOUNT).parts
    fd = os.open(_MOUNT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if part in ('.', '..'):
                raise ValueError('invalid cgroup path')
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd: int, name: str) -> str:
    stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        data = os.read(stream, 1024 * 1024 + 1)
        if len(data) > 1024 * 1024 or (data and not data.endswith(b'\n')):
            raise ValueError('cgroup observation exceeds bound')
        return data.decode('ascii')
    finally:
        os.close(stream)


def _write(fd: int, name: str, value: bytes) -> None:
    stream = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        os.write(stream, value)
    finally:
        os.close(stream)


def _ready(fd: int, deadline: float, *, write: bool = False) -> bool:
    poller = select.poll()
    poller.register(fd, select.POLLOUT if write else select.POLLIN)
    return bool(poller.poll(max(0, min(50, (deadline-time.monotonic()) * 1000))))


def launch(argv, *, cwd, env, pass_fds, deadline, on_cgroup):
    """Return the direct, gated child and receipt; caller releases after launch receipt."""
    from .agent_process import ProcessLaunchError

    if env is not None and not isinstance(env, Mapping):
        raise ProcessLaunchError()
    try:
        client_env = dict(os.environ if env is None else env)
    except Exception:
        raise ProcessLaunchError() from None
    if any(
        not isinstance(key, str) or not key or '=' in key or '\0' in key
        or not isinstance(value, str) or '\0' in value
        for key, value in client_env.items()
    ):
        raise ProcessLaunchError()
    payload = json.dumps(client_env, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if len(payload) > _MAX_ENV_BYTES:
        raise ProcessLaunchError()
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    exec_r, exec_w = os.pipe()
    env_r, env_w = os.pipe()
    child = None
    unit = 'tpo-' + uuid.uuid4().hex + '.scope'
    try:
        child = subprocess.Popen(
            ['systemd-run', '--user', '--scope', '--quiet', '--property=Delegate=yes',
             '--unit=' + unit, sys.executable, '-I', '-c', _BOOTSTRAP,
             str(ready_w), str(release_r), str(exec_w), str(env_r), *argv],
            cwd=cwd, env=None, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
            pass_fds=(*pass_fds, ready_w, release_r, exec_w, env_r),
        )
        os.close(env_r)
        env_r = -1
        os.set_blocking(env_w, False)
        offset = 0
        while offset < len(payload):
            if time.monotonic() >= deadline:
                raise ProcessLaunchError()
            if _ready(env_w, deadline, write=True):
                try:
                    offset += os.write(env_w, payload[offset:offset + 65536])
                except BlockingIOError:
                    continue
        os.close(env_w)
        env_w = -1
        os.close(exec_w)
        exec_w = -1
        os.close(ready_w)
        ready_w = -1
        os.close(release_r)
        release_r = -1
        data = b''
        while b'\n' not in data:
            if not _ready(ready_r, deadline):
                if time.monotonic() >= deadline:
                    raise ProcessLaunchError()
                continue
            chunk = os.read(ready_r, 8192)
            if not chunk or len(data) + len(chunk) > 8192:
                raise ProcessLaunchError()
            data += chunk
        path = str(_MOUNT) + json.loads(data)['path']
        root_device, root_inode = _root_identity()
        fd = _open_directory(path)
        try:
            info = os.fstat(fd)
            receipt = dict(version=2, path=path, device=info.st_dev, inode=info.st_ino,
                           root_device=root_device, root_inode=root_inode,
                           boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                           host=socket.gethostname(), unit=unit)
            validate_receipt(receipt)
            if str(child.pid) not in _read(fd, 'cgroup.procs').split():
                raise ProcessLaunchError()
            # Verify kill availability before making the external client runnable.
            probe = os.open('cgroup.kill', os.O_WRONLY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(probe)
            if on_cgroup:
                on_cgroup(receipt)
        finally:
            os.close(fd)
        answer = child, receipt, release_w, exec_r
        release_w = -1
        exec_r = -1
        return answer
    except BaseException:
        if child is not None:
            # Child is unreaped, still gated, and cannot have external descendants.
            try:
                child.kill()
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                from .agent_process import ProcessOwnershipError, process_snapshot
                try:
                    identity = process_snapshot(child.pid)
                except (OSError, ValueError, IndexError):
                    identity = None
                raise ProcessOwnershipError([identity] if identity is not None else []) from None
            finally:
                if child.stdin is not None:
                    child.stdin.close()
        raise
    finally:
        for fd in (ready_r, ready_w, release_r, release_w, exec_r, exec_w, env_r, env_w):
            if fd >= 0:
                os.close(fd)


def await_exec(fd: int, deadline: float) -> None:
    """Observe CLOEXEC acknowledgement, never treating bootstrap errors as exits."""
    from .agent_process import ProcessLaunchError

    while time.monotonic() < deadline:
        if _ready(fd, deadline):
            if os.read(fd, 1):
                raise ProcessLaunchError()
            return
    # The caller's monitoring loop records the expired attempt as timed_out.


def _signal_members(fd: int, sig: int, deadline: float) -> None:
    from .agent_process import _pidfd_open, _pidfd_signal

    for value in _read(fd, 'cgroup.procs').split():
        if time.monotonic() >= deadline:
            return
        pid = int(value)
        handle = None
        try:
            handle = _pidfd_open(pid)
            # Recheck membership after pinning the PID against reuse.
            if value in _read(fd, 'cgroup.procs').split():
                _pidfd_signal(handle, sig)
        except ProcessLookupError:
            pass
        finally:
            if handle is not None:
                os.close(handle)
    for name in os.listdir(fd):
        if time.monotonic() >= deadline:
            return
        try:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except (NotADirectoryError, FileNotFoundError):
            continue
        try:
            _signal_members(child, sig, deadline)
        finally:
            os.close(child)


def cleanup_cgroup(receipt: dict, *, cleanup_timeout: float = 60) -> dict:
    """Terminate members without ancestry scans; refuse changed host/boot/inode."""
    result = {'cleanup': 'cleanup_unconfirmed', 'processes': []}
    fd = None
    verified = False
    try:
        validate_receipt(receipt)
        if (receipt['host'] != socket.gethostname()
                or receipt['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()):
            return result
        if receipt['version'] == 2 and not _same_root(receipt):
            return result
        try:
            fd = _open_directory(receipt['path'])
        except FileNotFoundError:
            # The kernel permits removing a scope only after it is empty.
            return {**result, 'cleanup': 'confirmed'} if _same_root(receipt) else result
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (receipt['device'], receipt['inode']):
            return result
        verified = True
        started = time.monotonic()
        deadline = started + min(60, max(0, cleanup_timeout))
        force_at = started + min(5, cleanup_timeout / 2)
        while True:
            try:
                events = dict(line.split() for line in _read(fd, 'cgroup.events').splitlines())
            except OSError:
                if _same_root(receipt) and not Path(receipt['path']).exists():
                    return {**result, 'cleanup': 'confirmed'}
                raise
            if events.get('populated') == '0':
                return {**result, 'cleanup': 'confirmed'}
            if time.monotonic() >= deadline:
                return result
            if time.monotonic() >= force_at:
                _write(fd, 'cgroup.kill', b'1')
            else:
                try:
                    _signal_members(fd, signal.SIGTERM, deadline)
                    _signal_members(fd, signal.SIGCONT, deadline)
                except (OSError, ValueError, RecursionError):
                    # Graceful observation failure cannot prevent the kernel's
                    # whole-cgroup forced cleanup on the next bounded pass.
                    force_at = time.monotonic()
            time.sleep(min(0.02, max(0, deadline-time.monotonic())))
    except OSError:
        if verified and _same_root(receipt) and not Path(receipt['path']).exists():
            return {**result, 'cleanup': 'confirmed'}
        return result
    except (ValueError, KeyError, RecursionError):
        return result
    finally:
        if fd is not None:
            os.close(fd)
