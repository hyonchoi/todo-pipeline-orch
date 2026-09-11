"""Linux cgroup launch and cleanup contracts."""
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_pipeline import agent_process

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux cgroup v2")


@pytest.fixture(scope="module")
def native_cgroup():
    import os

    def unavailable(reason):
        if os.environ.get('REQUIRE_NATIVE_CGROUP') == '1':
            pytest.fail(reason)
        pytest.skip(reason)
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        unavailable("native cgroup v2 unavailable")
    try:
        check = subprocess.run(["systemctl", "--user", "show-environment"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        unavailable("existing user manager unavailable")
    if check.returncode:
        unavailable("existing user manager unavailable")


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
    assert receipts and receipts[0]['version'] == 2
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
@pytest.mark.parametrize('mode', ['detached', 'stopped'])
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


def test_target_environment_does_not_control_user_manager_connection(tmp_path, native_cgroup):
    import json

    intended = {'PATH': '/usr/bin', 'LC_CTYPE': 'C.UTF-8', 'TPO_EXACT_VALUE': 'quoted " value\nend'}
    result = agent_process.run_process(
        [sys.executable, '-c', "import os,json;from pathlib import Path;Path('environment').write_text(json.dumps(dict(os.environ)))"],
        cwd=tmp_path, stdin_bytes=b'', timeout=5, env=intended,
    )
    assert result['exit_code'] == 0
    assert json.loads((tmp_path / 'environment').read_text()) == intended


@pytest.mark.parametrize('environment', [{'BAD=KEY': 'x'}, {'NUL': '\0'}, {'TYPE': 1}, {'': 'value'}])
def test_invalid_target_environment_refused_before_bootstrap(tmp_path, monkeypatch, environment):
    from hermes_pipeline import agent_cgroup

    monkeypatch.setattr(agent_cgroup.subprocess, 'Popen', lambda *a, **kw: pytest.fail('invalid environment launched'))
    with pytest.raises(agent_process.ProcessLaunchError, match='^client_not_launched$') as caught:
        agent_cgroup.launch(['/bin/true'], cwd=tmp_path, env=environment,
                            pass_fds=(), deadline=10, on_cgroup=None)
    assert caught.value.cleanup == 'confirmed'
    assert caught.value.processes == []


def test_oversized_environment_refused_without_payload_disclosure(tmp_path, monkeypatch):
    from hermes_pipeline import agent_cgroup

    monkeypatch.setattr(agent_cgroup, '_MAX_ENV_BYTES', 32)
    monkeypatch.setattr(agent_cgroup.subprocess, 'Popen', lambda *a, **kw: pytest.fail('oversized environment launched'))
    with pytest.raises(agent_process.ProcessLaunchError, match='^client_not_launched$') as caught:
        agent_cgroup.launch(['/bin/true'], cwd=tmp_path, env={'PRIVATE_VALUE': 'x' * 100},
                            pass_fds=(), deadline=10, on_cgroup=None)
    assert caught.value.cleanup == 'confirmed'
    assert caught.value.processes == []


def test_environment_delivery_larger_than_pipe_capacity(tmp_path, native_cgroup):
    intended = {'PATH': '/usr/bin', 'LARGE': 'x' * 100_000}
    result = agent_process.run_process(
        [sys.executable, '-c', "import os;from pathlib import Path;Path('large').write_text(os.environ['LARGE'])"],
        cwd=tmp_path, stdin_bytes=b'', timeout=5, env=intended,
    )
    assert result['exit_code'] == 0
    assert (tmp_path / 'large').read_text() == intended['LARGE']


def test_bootstrap_works_above_select_fd_limit(tmp_path, native_cgroup):
    import os
    import resource

    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] < 1100:
        pytest.skip('insufficient fd allowance for high descriptor regression')
    held = []
    try:
        while not held or held[-1] < 1030:
            held.append(os.open('/dev/null', os.O_RDONLY))
        result = agent_process.run_process(['/bin/true'], cwd=tmp_path, stdin_bytes=b'', timeout=5)
        assert result['exit_code'] == 0
    finally:
        for fd in held:
            os.close(fd)


def test_missing_legacy_scope_cannot_prove_empty_group():
    import socket

    from hermes_pipeline import agent_cgroup

    receipt = dict(version=1, path='/sys/fs/cgroup/user.slice/user-1.slice/user@1.service/app.slice/tpo-'+'a'*32+'.scope',
                   device=1, inode=2,
                   boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                   host=socket.gethostname(), unit='tpo-'+'a'*32+'.scope')
    assert agent_cgroup.cleanup_cgroup(receipt)['cleanup'] == 'cleanup_unconfirmed'


def test_changed_cgroup_root_refuses_before_scope_lookup(tmp_path, native_cgroup, monkeypatch):
    from hermes_pipeline import agent_cgroup

    receipts = []
    agent_process.run_process(['/bin/true'], cwd=tmp_path, stdin_bytes=b'', timeout=5, on_cgroup=receipts.append)
    receipt = receipts[0]
    monkeypatch.setattr(agent_cgroup, '_root_identity', lambda: (receipt['root_device'], receipt['root_inode'] + 1))
    monkeypatch.setattr(agent_cgroup, '_open_directory', lambda *_: pytest.fail('changed root traversed'))
    assert agent_cgroup.cleanup_cgroup(receipt)['cleanup'] == 'cleanup_unconfirmed'


def test_remounted_cgroup_namespace_cannot_confirm_hidden_live_scope(tmp_path, native_cgroup):
    import json
    import os

    def unavailable(reason):
        if os.environ.get('REQUIRE_NATIVE_CGROUP') == '1':
            pytest.fail(reason)
        pytest.skip(reason)

    def inspect(receipt):
        members = (Path(receipt['path']) / 'cgroup.procs').read_text()
        code = "import json,sys;from hermes_pipeline.agent_cgroup import cleanup_cgroup;print(json.dumps(cleanup_cgroup(json.loads(sys.argv[1]),cleanup_timeout=0)))"
        # Hosted Ubuntu restricts unprivileged namespaces. Opt in only on CI to
        # a root scratch child; mounts stay private and host policy is unchanged.
        namespace = ['unshare', '--user', '--map-root-user', '--cgroup', '--mount']
        if os.environ.get('NATIVE_CGROUP_NAMESPACE_SUDO') == '1':
            namespace = ['sudo', '-n', 'unshare', '--cgroup', '--mount']
        try:
            probe = subprocess.run(
                [*namespace, '--',
                 'sh', '-c', 'mount --make-rprivate / && mount -t cgroup2 none /sys/fs/cgroup '
                 '&& { printf "tpo-namespace-remounted\\n" >&2; exec "$@"; }',
                 'sh', sys.executable, '-c', code, json.dumps(receipt)],
                capture_output=True, text=True, timeout=10,
            )
        except OSError as error:
            unavailable(f'native namespace tooling unavailable: errno={error.errno}')
        if probe.returncode:
            # This bounded stderr is only from trusted namespace tools and our
            # Python probe, never an agent, provider response, or environment dump.
            diagnostic = f'returncode={probe.returncode}; stderr={probe.stderr[-2000:]!r}'
            if 'tpo-namespace-remounted' in probe.stderr.splitlines():
                pytest.fail(f'native namespace Python probe failed: {diagnostic}')
            unavailable(f'native namespace remount unavailable: {diagnostic}')
        assert json.loads(probe.stdout)['cleanup'] == 'cleanup_unconfirmed'
        assert (Path(receipt['path']) / 'cgroup.procs').read_text() == members
    result = agent_process.run_process(['/bin/true'], cwd=tmp_path, stdin_bytes=b'', timeout=15, on_cgroup=inspect)
    assert result['cleanup'] == 'confirmed'


def test_exec_failure_retains_unconfirmed_cleanup(tmp_path, native_cgroup, monkeypatch):
    from hermes_pipeline import agent_cgroup

    monkeypatch.setattr(agent_cgroup, 'cleanup_cgroup', lambda *a, **kw: {'cleanup': 'cleanup_unconfirmed', 'processes': []})
    with pytest.raises(agent_process.ProcessLaunchError) as caught:
        agent_process.run_process(['/missing-cgroup-exec-target'], cwd=tmp_path, stdin_bytes=b'', timeout=5)
    assert caught.value.cleanup == 'cleanup_unconfirmed'
    assert caught.value.processes


def test_unreaped_bootstrap_does_not_claim_launch_cleanup_confirmed(tmp_path, native_cgroup, monkeypatch):
    import time

    from hermes_pipeline import agent_cgroup

    children = []
    original_popen = subprocess.Popen
    original_wait = original_popen.wait
    def start(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child
    def unavailable_wait(child, *args, **kwargs):
        raise subprocess.TimeoutExpired('bootstrap', 5)
    def reject(_):
        raise agent_process.ProcessLaunchError()
    monkeypatch.setattr(agent_cgroup.subprocess, 'Popen', start)
    monkeypatch.setattr(original_popen, 'wait', unavailable_wait)
    try:
        with pytest.raises(agent_process.ProcessOwnershipError) as caught:
            agent_cgroup.launch(['/bin/true'], cwd=tmp_path, env=None, pass_fds=(),
                                deadline=time.monotonic() + 5, on_cgroup=reject)
        assert caught.value.cleanup == 'cleanup_unconfirmed'
        assert caught.value.processes
    finally:
        for child in children:
            original_wait(child, timeout=5)


def test_environment_mapping_preserves_popen_compatibility(tmp_path, native_cgroup):
    import os

    result = agent_process.run_process(['/bin/true'], cwd=tmp_path, stdin_bytes=b'',
                                       env=os.environ, timeout=5)
    assert result['exit_code'] == 0
    assert result['cleanup'] == 'confirmed'
