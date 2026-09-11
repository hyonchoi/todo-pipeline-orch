"""Linux cgroup launch and cleanup contracts."""
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_pipeline import agent_process

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux cgroup v2")


@pytest.fixture(scope="module")
def native_cgroup():
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        pytest.skip("native cgroup v2 unavailable")
    try:
        check = subprocess.run(["systemctl", "--user", "show-environment"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("existing user manager unavailable")
    if check.returncode:
        pytest.skip("existing user manager unavailable")


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux cgroup v2')
def test_cgroup_receipt_precedes_client_execution(tmp_path, native_cgroup):
    receipts = []
    def persist(receipt):
        assert not (tmp_path / 'executed').exists()
        receipts.append(receipt)
    result = agent_process.run_process(
        [sys.executable, '-c', "from pathlib import Path; Path('executed').touch()"],
        cwd=tmp_path, stdin_bytes=b'', timeout=5, cleanup_timeout=1,
        on_cgroup=persist,
    )
    assert receipts and receipts[0]['version'] == 1
    assert result['exit_code'] == 0
    assert result['cleanup'] == 'confirmed'


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux cgroup v2')
def test_linux_does_not_discover_host_processes(tmp_path, monkeypatch, native_cgroup):
    monkeypatch.setattr(agent_process, '_discover', lambda *_: pytest.fail('host process scan'))
    result = agent_process.run_process(
        [sys.executable, '-c', 'pass'], cwd=tmp_path, stdin_bytes=b'', timeout=5,
    )
    assert result['cleanup'] == 'confirmed'


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux cgroup v2')
@pytest.mark.parametrize('mode', ['detached', 'stopped', 'resistant'])
def test_cleanup_after_success_contains_orphaned_descendants(tmp_path, mode, native_cgroup):
    descendant = (
        "import os,signal,time; from pathlib import Path; os.setsid(); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('descendant').write_text(str(os.getpid())); "
        + ("os.kill(os.getpid(),signal.SIGSTOP); " if mode == 'stopped' else '')
        + "time.sleep(30)"
    )
    source = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
        "\nwhile not Path('descendant').exists(): time.sleep(.01)"
    )
    result = agent_process.run_process(
        [sys.executable, '-c', source], cwd=tmp_path, stdin_bytes=b'',
        timeout=5, cleanup_timeout=1,
    )
    assert result['exit_code'] == 0
    assert result['cleanup'] == 'confirmed'
    pid = int((tmp_path / 'descendant').read_text())
    snapshot = agent_process.process_snapshot(pid)
    assert snapshot is None or snapshot['state'] == 'Z'


def test_cgroup_receipt_callback_failure_never_executes_client(tmp_path, native_cgroup):
    def reject(_):
        raise ValueError('receipt unavailable')
    with pytest.raises(ValueError, match='receipt unavailable'):
        agent_process.run_process(
            [sys.executable, '-c', "from pathlib import Path; Path('executed').touch()"],
            cwd=tmp_path, stdin_bytes=b'', timeout=5, on_cgroup=reject,
        )
    assert not (tmp_path / 'executed').exists()


def test_missing_user_manager_blocks_without_external_execution(tmp_path, monkeypatch):
    from hermes_pipeline import agent_cgroup
    monkeypatch.setattr(agent_cgroup.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(agent_process.ProcessLaunchError):
        agent_process.run_process([sys.executable, '-c', 'pass'], cwd=tmp_path, stdin_bytes=b'', timeout=2)


@pytest.mark.parametrize('change', [{'version': 9}, {'path': '/tmp/escape'}, {'inode': True}, {'unit': '../escape'}])
def test_reject_invalid_receipt(change, monkeypatch):
    from hermes_pipeline import agent_cgroup
    receipt = dict(version=1, path='/sys/fs/cgroup/user.slice/user-1.slice/user@1.service/app.slice/tpo-'+'a'*32+'.scope',
                   device=1, inode=2, boot_id='boot', host='host', unit='tpo-'+'a'*32+'.scope')
    monkeypatch.setattr(agent_cgroup, '_open_directory', lambda *_: pytest.fail('invalid receipt opened'))
    assert agent_cgroup.cleanup_cgroup({**receipt, **change})['cleanup'] == 'cleanup_unconfirmed'


def test_reboot_receipt_never_signals(monkeypatch):
    import socket

    from hermes_pipeline import agent_cgroup
    receipt = dict(version=1, path='/sys/fs/cgroup/user.slice/user-1.slice/user@1.service/app.slice/tpo-'+'a'*32+'.scope',
                   device=1, inode=2, boot_id='old-boot', host=socket.gethostname(), unit='tpo-'+'a'*32+'.scope')
    monkeypatch.setattr(agent_cgroup, '_open_directory', lambda *_: pytest.fail('old boot opened'))
    assert agent_cgroup.cleanup_cgroup(receipt)['cleanup'] == 'cleanup_unconfirmed'


def test_reused_cgroup_inode_never_signals(tmp_path, monkeypatch):
    import os
    import socket
    from pathlib import Path

    from hermes_pipeline import agent_cgroup
    receipt = dict(version=1, path='/sys/fs/cgroup/user.slice/user-1.slice/user@1.service/app.slice/tpo-'+'a'*32+'.scope',
                   device=tmp_path.stat().st_dev, inode=tmp_path.stat().st_ino + 1,
                   boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                   host=socket.gethostname(), unit='tpo-'+'a'*32+'.scope')
    monkeypatch.setattr(agent_cgroup, '_open_directory', lambda *_: os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY))
    monkeypatch.setattr(agent_cgroup, '_write', lambda *_: pytest.fail('reused group signaled'))
    assert agent_cgroup.cleanup_cgroup(receipt)['cleanup'] == 'cleanup_unconfirmed'


def test_cgroup_path_rejects_symlink_components(tmp_path, monkeypatch):
    from hermes_pipeline import agent_cgroup
    monkeypatch.setattr(agent_cgroup, '_MOUNT', tmp_path)
    (tmp_path / 'actual').mkdir()
    (tmp_path / 'alias').symlink_to(tmp_path / 'actual', target_is_directory=True)
    with pytest.raises(OSError):
        agent_cgroup._open_directory(str(tmp_path / 'alias'))


@pytest.mark.parametrize('before_release', [True, False])
def test_supervisor_loss_recovery_uses_durable_group(tmp_path, native_cgroup, before_release):
    import json
    import time

    from hermes_pipeline.agent_cgroup import cleanup_cgroup

    target = "from pathlib import Path; import time; Path('client-started').touch(); time.sleep(30)"
    source = f'''
import json,sys,time
from pathlib import Path
from hermes_pipeline.agent_process import run_process
root=Path({str(tmp_path)!r})
def persist(receipt):
    (root/'receipt').write_text(json.dumps(receipt))
    if {before_release!r}: time.sleep(30)
run_process([sys.executable,'-c',{target!r}],cwd=root,stdin_bytes=b'',timeout=30,on_cgroup=persist)
'''
    owner = subprocess.Popen([sys.executable, '-c', source])
    try:
        ready = tmp_path / ('receipt' if before_release else 'client-started')
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        receipt = json.loads((tmp_path / 'receipt').read_text())
        owner.kill()
        owner.wait(timeout=3)
        assert cleanup_cgroup(receipt, cleanup_timeout=2)['cleanup'] == 'confirmed'
        assert (tmp_path / 'client-started').exists() is not before_release
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=3)
        if (tmp_path / 'receipt').exists():
            cleanup_cgroup(json.loads((tmp_path / 'receipt').read_text()), cleanup_timeout=2)


def test_cgroup_preserves_argv_stdin_and_deadline_outcome(tmp_path, native_cgroup):
    import json

    argument = 'literal $HOME ${HOME} $(touch bad) "quoted" \\ slash'
    prompt = b'prompt\x00\n"$HOME"\xff' * 10000
    source = "import sys,json; from pathlib import Path; Path('argv').write_text(json.dumps(sys.argv[1:])); Path('stdin').write_bytes(sys.stdin.buffer.read())"
    result = agent_process.run_process(
        [sys.executable, '-c', source, argument], cwd=tmp_path,
        stdin_bytes=prompt, timeout=5,
    )
    assert result['exit_code'] == 0
    assert json.loads((tmp_path / 'argv').read_text()) == [argument]
    assert (tmp_path / 'stdin').read_bytes() == prompt
    result = agent_process.run_process(
        [sys.executable, '-c', 'import signal,time,sys; signal.signal(signal.SIGTERM,lambda *_:sys.exit(0)); time.sleep(30)'],
        cwd=tmp_path, stdin_bytes=b'', timeout=.5, cleanup_timeout=1,
    )
    assert result['outcome'] == 'timed_out'
    assert result['exit_code'] == 0
    assert result['cleanup'] == 'confirmed'


def test_failed_graceful_inventory_still_uses_cgroup_kill(tmp_path, native_cgroup, monkeypatch):
    from hermes_pipeline import agent_cgroup

    def unavailable(*_):
        raise PermissionError('member inventory unavailable')
    monkeypatch.setattr(agent_cgroup, '_signal_members', unavailable)
    result = agent_process.run_process(
        [sys.executable, '-c', 'import time;time.sleep(30)'],
        cwd=tmp_path, stdin_bytes=b'', timeout=.5, cleanup_timeout=1,
    )
    assert result['outcome'] == 'timed_out'
    assert result['exit_code'] == -9
    assert result['cleanup'] == 'confirmed'


def test_capability_preflight_rejects_missing_user_manager(monkeypatch):
    from hermes_pipeline import agent_cgroup

    monkeypatch.setattr(agent_cgroup.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout=''))
    with pytest.raises(agent_process.ProcessLaunchError):
        agent_process.confirm_process_capability()


def test_expired_launch_callback_never_releases_external_client(tmp_path, native_cgroup, monkeypatch):
    import os
    import time

    original_write = os.write
    def slow_release(fd, data):
        written = original_write(fd, data)
        if data == b'1':
            time.sleep(.15)
        return written
    monkeypatch.setattr(os, 'write', slow_release)
    result = agent_process.run_process(
        [sys.executable, '-c', "from pathlib import Path; Path('executed').touch()"],
        cwd=tmp_path, stdin_bytes=b'', timeout=.5, cleanup_timeout=1,
        on_launch=lambda _: time.sleep(.7),
    )
    assert result['outcome'] == 'timed_out'
    assert not (tmp_path / 'executed').exists()
    assert result['cleanup'] == 'confirmed'


def test_missing_target_is_launch_failure_not_collected_exit(tmp_path, native_cgroup):
    with pytest.raises(agent_process.ProcessLaunchError):
        agent_process.run_process(
            ['/nonexistent-tpo-cgroup-target'], cwd=tmp_path,
            stdin_bytes=b'', timeout=5, cleanup_timeout=1,
        )
    result = agent_process.run_process(
        [sys.executable, '-c', 'raise SystemExit(127)'], cwd=tmp_path,
        stdin_bytes=b'', timeout=5, cleanup_timeout=1,
    )
    assert result['outcome'] == 'exited'
    assert result['exit_code'] == 127


def test_bootstrap_does_not_import_worktree_python_modules(tmp_path, native_cgroup):
    (tmp_path / 'json.py').write_text("raise RuntimeError('worktree json imported by bootstrap')")
    result = agent_process.run_process(
        ['/bin/true'], cwd=tmp_path, stdin_bytes=b'', timeout=5,
    )
    assert result['exit_code'] == 0
    assert result['cleanup'] == 'confirmed'
