"""Darwin ABI and race regressions run provider-free on every platform."""
import ctypes
import errno
import os
import signal
import struct
import sys

import pytest

from hermes_pipeline import agent_darwin as darwin


def record(pid=42, unique=901, version=7):
    value = bytearray(192)
    struct.pack_into('=IIII', value, 4, 2, 12345, pid, 1)
    struct.pack_into('=I', value, 100, pid)
    struct.pack_into('=QQi', value, 152, unique, 900, version)
    return bytes(value)


class Operation:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class Library:
    def __init__(self):
        self.data = record()
        self.signals = []
        self.signal_result = 0
        self.proc_pidinfo = Operation(self.info)
        self.proc_signal_with_audittoken = Operation(self.send)
        self.proc_listpids = Operation(self.pids)
        self.sysctlbyname = Operation(self.boot)

    def info(self, pid, flavor, arg, buf, size):
        assert (flavor, arg, size) == (18, 1, 192)
        ctypes.memmove(buf, self.data, len(self.data))
        return len(self.data)

    def send(self, token, sig):
        self.signals.append((list(token), sig))
        return self.signal_result

    def pids(self, kind, arg, buf, size):
        assert (kind, arg) == (1, 0)
        if buf is not None:
            ctypes.memmove(buf, struct.pack('=ii', 42, 43), 8)
        return 8

    def boot(self, name, buf, size, new, newsize):
        assert name == b'kern.bootsessionuuid'
        data = b'01234567-89AB-CDEF-0123-456789ABCDEF\0'
        ctypes.memmove(buf, data, len(data))
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = len(data)
        return 0


@pytest.fixture
def api(monkeypatch):
    library = Library()
    monkeypatch.setattr(darwin.ctypes, 'CDLL', lambda *a, **kw: library)
    monkeypatch.setattr(darwin.os, 'getsid', lambda pid: pid)
    return library, darwin.Backend()


def test_combined_snapshot_and_boot(api):
    _, backend = api
    snapshot = backend.snapshot(42)
    assert snapshot['start_ticks'] == 901
    assert snapshot['ppid'] == 1
    assert snapshot['session'] == 42
    assert snapshot['boot_id'] == 'darwin:01234567-89ab-cdef-0123-456789abcdef'
    assert backend.pids() == [42, 43]


def test_snapshot_accepts_same_birth_reparenting(api, monkeypatch):
    library, backend = api
    before = bytearray(record())
    struct.pack_into('=I', before, 16, 77)
    library.data = bytes(before)

    def reparent_during_session_lookup(pid):
        library.data = record()
        return pid

    monkeypatch.setattr(darwin.os, 'getsid', reparent_during_session_lookup)
    snapshot = backend.snapshot(42)
    assert snapshot['pid'] == 42
    assert snapshot['start_ticks'] == 901
    assert snapshot['ppid'] == 1
    assert snapshot['pgrp'] == snapshot['session'] == 42


def test_audit_signal_pins_version_without_numeric_kill(api, monkeypatch):
    library, backend = api
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('numeric signal'))
    identity = backend.snapshot(42)
    assert backend.signal(identity, signal.SIGTERM)
    assert library.signals == [([0, 0, 0, 0, 0, 42, 0, 7], signal.SIGTERM)]
    library.data = record(unique=902)
    assert not backend.signal(identity, signal.SIGKILL)
    assert len(library.signals) == 1


def test_exec_version_refresh_and_errno_return(api):
    library, backend = api
    identity = backend.snapshot(42)
    library.data = record(version=-1)
    assert backend.signal(identity, signal.SIGTERM)
    assert library.signals[-1][0][7] == 0xffffffff
    library.signal_result = errno.EPERM
    assert not backend.signal(identity, signal.SIGTERM)
    library.signal_result = errno.ESRCH
    assert not backend.signal(identity, signal.SIGTERM)
    assert len(library.signals) <= 5


def test_short_snapshot_and_sid_reuse_fail_closed(api, monkeypatch):
    library, backend = api
    library.data = record()[:136]
    with pytest.raises(OSError):
        backend.snapshot(42)
    library.data = record()
    def reuse(pid):
        library.data = record(unique=999)
        return pid
    monkeypatch.setattr(os, 'getsid', reuse)
    with pytest.raises(OSError):
        backend.snapshot(42)


def test_missing_audit_capability_blocks_backend(monkeypatch):
    library = Library()
    del library.proc_signal_with_audittoken
    monkeypatch.setattr(darwin.ctypes, 'CDLL', lambda *a, **kw: library)
    with pytest.raises(OSError):
        darwin.Backend()


@pytest.mark.skipif(sys.platform != 'darwin', reason='native Darwin required')
def test_native_identity_and_timeout(tmp_path):
    from hermes_pipeline.agent_process import process_snapshot, run_process
    identity = process_snapshot(os.getpid())
    assert identity['start_ticks'] > 0
    assert identity['boot_id'].startswith('darwin:')
    result = run_process([sys.executable, '-c', 'import time; time.sleep(30)'],
                         cwd=tmp_path.resolve(), stdin_bytes=b'', timeout=.2, cleanup_timeout=2)
    assert result['outcome'] == 'timed_out'
    assert result['cleanup'] == 'confirmed'


def test_native_ci_requires_darwin():
    if os.environ.get('REQUIRE_NATIVE_DARWIN') == '1':
        assert sys.platform == 'darwin', 'native qualification requires Darwin'
        darwin.Backend().snapshot(os.getpid())


def test_errno_and_enumeration_fail_closed(api):
    library, backend = api
    def gone(*args):
        ctypes.set_errno(errno.ESRCH)
        return 0
    library._old_info = backend._info
    backend._info = gone
    assert backend.snapshot(42) is None
    def denied(*args):
        ctypes.set_errno(errno.EPERM)
        return 0
    backend._info = denied
    with pytest.raises(OSError):
        backend.snapshot(42)
    for count in (-1, 0, 3, 20 * 1024 * 1024):
        backend._list = lambda *args, count=count: count
        with pytest.raises(OSError):
            backend.pids()


def test_boot_and_host_mismatch_never_signal(api):
    library, backend = api
    identity = backend.snapshot(42)
    for key in ('host', 'boot_id'):
        assert not backend.signal(identity | {key: 'other'}, signal.SIGKILL)
    assert not backend.signal(identity, 0)
    assert library.signals == []


def test_exec_race_retries_stable_birth_and_rejects_reuse(api):
    library, backend = api
    identity = backend.snapshot(42)
    def changed(token, sig):
        library.signals.append(list(token))
        library.data = record(version=8)
        backend._send = library.send
        return errno.ESRCH
    backend._send = changed
    assert backend.signal(identity, signal.SIGTERM)
    assert library.signals[-1][0][7] == 8
    def reused(token, sig):
        library.data = record(unique=902)
        return errno.ESRCH
    backend._send = reused
    assert not backend.signal(identity, signal.SIGTERM)


@pytest.mark.skipif(sys.platform != 'darwin', reason='native Darwin required')
def test_native_exec_preserves_birth_rejects_stale_audit_token(tmp_path):
    import subprocess
    import time
    backend = darwin.Backend()
    trigger = tmp_path.resolve() / 'exec'
    ready = tmp_path.resolve() / 'ready'
    code = ('import os,sys,time; from pathlib import Path; '
            'p=Path(sys.argv[1]); '
            'exec("while not p.exists(): time.sleep(.01)"); '
            'os.execv(sys.executable,[sys.executable,"-c",'
            '"from pathlib import Path; import time; Path("+repr(sys.argv[2])+").touch(); time.sleep(30)"])')
    child = subprocess.Popen([sys.executable, '-c', code, str(trigger), str(ready)])
    try:
        before = backend._record(child.pid)
        identity = backend.snapshot(child.pid)
        trigger.touch()
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        after = backend._record(child.pid)
        assert after['start_ticks'] == before['start_ticks']
        assert after['_version'] != before['_version']
        stale = (ctypes.c_uint32 * 8)()
        stale[5], stale[7] = child.pid, before['_version'] & 0xffffffff
        assert backend._send(stale, signal.SIGTERM) == errno.ESRCH
        assert child.poll() is None
        assert backend.signal(identity, signal.SIGTERM)
        child.wait(timeout=3)
    finally:
        if child.poll() is None:
            backend.signal(backend.snapshot(child.pid), signal.SIGKILL)
            child.wait(timeout=3)


@pytest.mark.skipif(sys.platform != 'darwin', reason='native Darwin required')
@pytest.mark.parametrize('stopped', [False, True])
def test_native_owned_tree_timeout_and_sibling_survives(tmp_path, stopped):
    import subprocess
    import time

    from hermes_pipeline.agent_process import run_process
    backend = darwin.Backend()
    sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    sibling_identity = backend.snapshot(sibling.pid)
    leaf = ('import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            + ('os.kill(os.getpid(),signal.SIGSTOP); ' if stopped else '')
            + 'time.sleep(30)')
    child = ('import signal,subprocess,sys,time; '
             'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
             'subprocess.Popen([sys.executable,"-c",sys.argv[1]]); time.sleep(30)')
    leader = ('import subprocess,sys,time; '
              'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]); time.sleep(30)')
    try:
        started = time.monotonic()
        result = run_process([sys.executable, '-c', leader, child, leaf],
                             cwd=tmp_path.resolve(), stdin_bytes=b'', timeout=1, cleanup_timeout=3)
        assert time.monotonic() - started < 6
        assert result['outcome'] == 'timed_out'
        assert result['cleanup'] == 'confirmed'
        assert len(result['processes']) >= 3
        assert sibling.poll() is None
        for identity in result['processes']:
            snapshot = backend.snapshot(identity['pid'])
            assert snapshot is None or snapshot['state'] == 'Z'
    finally:
        backend.signal(sibling_identity, signal.SIGKILL)
        sibling.wait(timeout=3)


@pytest.mark.skipif(sys.platform != 'darwin', reason='native Darwin required')
def test_native_successful_child_cleanup(tmp_path):
    from hermes_pipeline.agent_execution import host_boot_identity, process_identity
    from hermes_pipeline.agent_process import process_snapshot, run_process
    identity = process_identity(os.getpid())
    assert identity == {key: process_snapshot(os.getpid())[key] for key in identity}
    assert identity['boot_id'] == host_boot_identity()['boot_id']
    result = run_process([sys.executable, '-c', 'pass'], cwd=tmp_path.resolve(),
                         stdin_bytes=b'', timeout=5, cleanup_timeout=2)
    assert result['outcome'] == 'exited'
    assert result['exit_code'] == 0
    assert result['cleanup'] == 'confirmed'


def test_zombie_birth_is_observable_without_getsid(api, monkeypatch):
    library, backend = api
    zombie = bytearray(record())
    struct.pack_into('=I', zombie, 4, 5)
    def info(pid, flavor, arg, buf, size):
        assert flavor == 18 and size == 192
        if arg == 0:
            ctypes.set_errno(errno.ESRCH)
            return 0
        assert arg == 1
        ctypes.memmove(buf, bytes(zombie), size)
        return size
    backend._info = info
    monkeypatch.setattr(os, 'getsid', lambda pid: pytest.fail('zombie SID lookup'))
    snapshot = backend.snapshot(42)
    assert snapshot is not None
    assert snapshot['start_ticks'] == 901
    assert snapshot['state'] == 'Z'
    assert snapshot['session'] == 0
    assert backend.signal(snapshot, signal.SIGTERM)
    assert library.signals == []


def test_discovery_uses_public_metadata_but_verifies_owned_candidates(api, monkeypatch):
    from hermes_pipeline import agent_process
    library, backend = api
    rows = {1: (0, 1, 100), 42: (1, 42, 901), 43: (42, 42, 902)}
    strict_reads = []
    deny_child = False
    def info(pid, flavor, arg, buf, size):
        ppid, pgid, unique = rows[pid]
        if flavor == 18:
            strict_reads.append(pid)
            if pid == 1 or (pid == 43 and deny_child):
                ctypes.set_errno(errno.EPERM)
                return 0
            data = bytearray(record(pid=pid, unique=unique))
            struct.pack_into('=I', data, 16, ppid)
            struct.pack_into('=I', data, 100, pgid)
        elif flavor == 13:
            assert arg == 1 and size == 64
            data = bytearray(64)
            struct.pack_into('=IIII', data, 0, pid, ppid, pgid, 2)
        elif flavor == 17:
            assert arg == 1 and size == 56
            data = bytearray(56)
            struct.pack_into('=QQi', data, 16, unique, 100, 7)
        else:
            pytest.fail(f'unexpected flavor {flavor}')
        ctypes.memmove(buf, bytes(data), len(data))
        return len(data)
    backend._info = info
    monkeypatch.setattr(darwin, 'Backend', lambda: backend)
    monkeypatch.setattr(backend, 'pids', lambda: list(rows))
    monkeypatch.setattr(os, 'getsid', lambda pid: rows[pid][1])
    monkeypatch.setattr(agent_process.sys, 'platform', 'darwin')
    root = backend.snapshot(42)
    known = {42: root}
    assert agent_process._discover(known)
    assert set(known) == {42, 43}
    assert 1 not in strict_reads
    deny_child = True
    assert not agent_process._discover({42: root})
    assert not agent_process._discover(known)


def test_public_discovery_short_record_is_birth_guarded(api, monkeypatch):
    _, backend = api
    unique = 901
    def info(pid, flavor, arg, buf, size):
        assert arg == 1
        data = bytearray(size)
        if flavor == 17:
            assert size == 56
            struct.pack_into('=QQi', data, 16, unique, 100, 7)
        else:
            assert (flavor, size) == (13, 64)
            struct.pack_into('=IIII', data, 0, pid, 1, pid, 2)
        ctypes.memmove(buf, bytes(data), size)
        return size
    backend._info = info
    snapshot = backend.discovery_snapshot(42)
    assert snapshot['start_ticks'] == 901
    assert snapshot['ppid'] == 1
    assert snapshot['session'] == 42
    def reused(pid):
        nonlocal unique
        unique = 902
        return pid
    monkeypatch.setattr(os, 'getsid', reused)
    with pytest.raises(OSError, match='changed during discovery'):
        backend.discovery_snapshot(42)


@pytest.mark.skipif(sys.platform != 'darwin', reason='native Darwin required')
def test_native_exit_before_first_snapshot_keeps_birth_receipt(tmp_path, monkeypatch):
    from hermes_pipeline import agent_process
    popen = agent_process.subprocess.Popen
    backend = darwin.Backend()
    def exited_unreaped(*args, **kwargs):
        import time
        child = popen(*args, **kwargs)
        # Observe a zombie via public BSD metadata without collecting status or
        # depending on os.waitid (absent on Darwin in supported Python 3.12).
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            short = backend._public_info(child.pid, 13, 64)
            if short is not None and struct.unpack_from("=I", short, 12)[0] == 5:
                assert backend.snapshot(child.pid)["state"] == "Z"
                return child
            time.sleep(.01)
        child.kill()
        child.wait(timeout=3)
        pytest.fail("child did not become an observable zombie")
    monkeypatch.setattr(agent_process.subprocess, 'Popen', exited_unreaped)
    receipts = []
    result = agent_process.run_process([sys.executable, '-c', 'pass'], cwd=tmp_path.resolve(),
                                      stdin_bytes=b'', timeout=5, cleanup_timeout=2,
                                      on_launch=receipts.append)
    assert result['outcome'] == 'exited'
    assert result['exit_code'] == 0
    assert result['cleanup'] == 'confirmed'
    assert receipts[0]['identity']['start_ticks'] > 0
    assert receipts[0]['identity']['session'] == receipts[0]['identity']['pid']


@pytest.mark.skipif(sys.platform != 'darwin', reason='native Darwin required')
def test_native_public_discovery_can_inspect_system_owner():
    backend = darwin.Backend()
    snapshot = backend.discovery_snapshot(1)
    assert snapshot is not None
    assert snapshot['pid'] == 1
    assert snapshot['start_ticks'] > 0
    # PID 1 belongs to root. This confirms the test host exercises the exact
    # cross-user difference, rather than hiding it under a privileged runner.
    assert os.geteuid() != 0
    with pytest.raises(PermissionError):
        backend.snapshot(1)


def test_exhausted_esrch_is_pending_only_for_verified_same_birth(api):
    library, backend = api
    identity = backend.snapshot(42)
    library.signal_result = errno.ESRCH
    assert backend.signal(identity, signal.SIGCONT) is None
    library.signal_result = errno.EPERM
    assert backend.signal(identity, signal.SIGCONT) is False
    library.signal_result = errno.ESRCH
    original_send = library.send
    def reused_after_last_send(token, sig):
        result = original_send(token, sig)
        if len(library.signals) == 7:
            library.data = record(unique=902)
        return result
    backend._send = reused_after_last_send
    assert backend.signal(identity, signal.SIGCONT) is False


@pytest.mark.parametrize('final', ['zombie', 'alive', 'reused', 'unreadable', 'discovery_gap', 'permission'])
def test_continue_exit_race_requires_independent_final_death(api, monkeypatch, final):
    from hermes_pipeline import agent_process
    library, backend = api
    identity = backend.snapshot(42)
    sent_term = False
    def send(token, sig):
        nonlocal sent_term
        library.signals.append((list(token), sig))
        if sig == signal.SIGTERM:
            sent_term = True
            return 0
        return errno.EPERM if final == 'permission' else errno.ESRCH
    backend._send = send
    def snapshot(pid):
        if not sent_term or final == 'alive':
            return identity
        if final == 'reused':
            return identity | {'start_ticks': 902}
        if final == 'unreadable':
            raise PermissionError('unavailable')
        return identity | {'state': 'Z'}
    monkeypatch.setattr(darwin, 'Backend', lambda: backend)
    monkeypatch.setattr(agent_process.sys, 'platform', 'darwin')
    monkeypatch.setattr(agent_process, 'process_snapshot', snapshot)
    monkeypatch.setattr(agent_process, '_discover', lambda known: final != 'discovery_gap')
    elapsed = [0.0]
    monkeypatch.setattr(agent_process.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(agent_process.time, 'sleep', lambda delay: elapsed.__setitem__(0, elapsed[0] + delay))
    result = agent_process.cleanup_processes([identity], cleanup_timeout=.06)
    assert elapsed[0] <= .06
    if final == 'alive':
        assert sum(sig == signal.SIGCONT for _, sig in library.signals) >= 6
        assert any(sig == signal.SIGKILL for _, sig in library.signals)
    expected = 'confirmed' if final == 'zombie' else 'cleanup_unconfirmed'
    assert result['cleanup'] == expected
    assert library.signals[0][1] == signal.SIGTERM
    assert library.signals[1][1] == signal.SIGCONT


def test_exhausted_esrch_final_verification_error_is_not_pending(api):
    library, backend = api
    identity = backend.snapshot(42)
    library.signal_result = errno.ESRCH
    original = backend._record
    reads = 0
    def denied_final(pid):
        nonlocal reads
        reads += 1
        if reads == 4:
            raise PermissionError('unavailable')
        return original(pid)
    backend._record = denied_final
    assert backend.signal(identity, signal.SIGCONT) is False
    assert reads == 4
