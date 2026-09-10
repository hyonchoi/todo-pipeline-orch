import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_pipeline.agent_checkpoint import ProgressJournal
from hermes_pipeline.agent_execution import ExecutionError, ExecutionStore


def git(work, *args):
    return subprocess.check_output(['git', '-C', str(work), *args], text=True).strip()


@pytest.fixture
def candidate(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    git(work, 'init', '-b', 'task')
    git(work, 'config', 'user.email', 'test@example.invalid')
    git(work, 'config', 'user.name', 'Test')
    git(work, 'commit', '--allow-empty', '-m', 'base')
    store = ExecutionStore(tmp_path / 'state')
    store.register('execution', registration_id='card', plan_identity='a' * 64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(work), branch='task', result_contract={'progress_version': 1,
                       'git_metadata': {'common_dir': str(work / '.git'), 'worktree_git_dir': str(work / '.git')}}, timeout=30,
                   manifest={'tasks': [{'id': 'one', 'instructions': 'Create file',
                                        'verification': ['python check.py'],
                                        'acceptance_criteria': ['File exists']} ]})
    journal = ProgressJournal(store, 'execution')
    journal.initialize()
    store.admit('execution')
    (work / 'check.py').write_text('assert True\n')
    git(work, 'add', 'check.py')
    git(work, 'commit', '-m', 'task one')
    return store, journal, work


def fake_clients(monkeypatch, *, verification_exit=0, verdict='accepted', mutate=None):
    from hermes_pipeline import agent_collector as collector
    calls = []
    monkeypatch.setattr(collector, 'verification_argv', lambda argv, snapshot, **kwargs: ['check', *argv])
    monkeypatch.setattr(collector, 'review_argv', lambda client, snapshot, staging, authority, **kwargs: ['review'])

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == 'review':
            request = json.loads(kwargs['stdin_bytes'])
            response = {key: request[key] for key in ('version', 'task_id', 'commit', 'plan_identity', 'diff_sha256')}
            response['outcome'] = verdict
            if mutate:
                mutate(response)
            from pathlib import Path
            Path(kwargs['env']['TPO_REVIEW_RESULT_PATH']).write_text(json.dumps(response))
        return {'outcome': 'exited', 'exit_code': verification_exit if argv[0] == 'check' else 0,
                'signal': None, 'cleanup': 'confirmed', 'processes': []}

    monkeypatch.setattr(collector, 'run_process', run)
    return calls


def test_recovers_commit_without_checkpoint_and_preserves_partial_work(candidate, monkeypatch):
    from hermes_pipeline.agent_collector import collect_checkpoints
    store, journal, work = candidate
    (work / 'partial').write_text('staged')
    git(work, 'add', 'partial')
    (work / 'partial').write_text('unstaged')
    before = git(work, 'status', '--porcelain')
    calls = fake_clients(monkeypatch)
    result = collect_checkpoints(store, 'execution', 1, deadline_monotonic=time.monotonic() + 20)
    assert result['complete']
    assert len(calls) == 2
    assert journal.recovery_context(1)['accepted'][0]['commit'] == git(work, 'rev-parse', 'HEAD')
    assert git(work, 'status', '--porcelain') == before
    assert (work / 'partial').read_text() == 'unstaged'


@pytest.mark.parametrize('failure', ['check', 'review', 'identity', 'deadline'])
def test_rejects_unverified_checkpoint(candidate, monkeypatch, failure):
    from hermes_pipeline.agent_collector import collect_checkpoints
    store, journal, _ = candidate
    calls = fake_clients(monkeypatch, verification_exit=1 if failure == 'check' else 0,
                         verdict='rejected' if failure == 'review' else 'accepted',
                         mutate=(lambda value: value.update(commit='b' * 40)) if failure == 'identity' else None)
    with pytest.raises(ExecutionError):
        collect_checkpoints(store, 'execution', 1,
                            deadline_monotonic=time.monotonic() + (-1 if failure == 'deadline' else 20))
    assert journal.recovery_context(1)['accepted'] == []
    if failure == 'deadline':
        assert calls == []


def test_checks_and_review_share_original_remaining_deadline(candidate, monkeypatch):
    from hermes_pipeline.agent_collector import collect_checkpoints
    store, _, _ = candidate
    calls = fake_clients(monkeypatch)
    deadline = time.monotonic() + 10
    collect_checkpoints(store, 'execution', 1, deadline_monotonic=deadline)
    assert 0 < calls[1][1]['timeout'] <= calls[0][1]['timeout'] <= 10


def test_refuses_shell_syntax_in_pinned_checks():
    from hermes_pipeline.agent_collector import parse_check
    for command in ('pytest && git reset --hard', 'VAR=x pytest', 'echo $(cat secret)', 'pytest > result'):
        with pytest.raises(ExecutionError):
            parse_check(command)


def test_snapshot_rejects_symlinks(candidate, tmp_path):
    from hermes_pipeline.agent_collector import snapshot_commit
    _, _, work = candidate
    (work / 'escape').symlink_to('/etc/passwd')
    git(work, 'add', 'escape')
    git(work, 'commit', '-m', 'unsafe snapshot')
    target = tmp_path / 'snapshot'
    target.mkdir()
    with pytest.raises(ExecutionError, match='snapshot'):
        snapshot_commit(work, git(work, 'rev-parse', 'HEAD'), target, time.monotonic() + 10)


def test_manifest_completion_rejects_missing_authoritative_checkpoints(candidate, monkeypatch):
    from hermes_pipeline import _agent_supervisor as supervisor
    store, _, _ = candidate
    monkeypatch.setattr(supervisor, 'validate_registration', lambda *args: None)
    with pytest.raises(ExecutionError, match='checkpoint_evidence_incomplete'):
        supervisor.validated_result(store, 'execution', 1, promote=True)


def test_real_subprocess_checks_and_fresh_reviewer_create_receipts(candidate, monkeypatch):
    from hermes_pipeline import agent_collector as collector
    store, journal, _ = candidate
    # This exercises real process ownership/exit collection, with fake provider
    # executables. OS sandbox construction has its own argument/fail-closed tests.
    monkeypatch.setattr(collector, 'verification_argv', lambda argv, snapshot, **kwargs: [sys.executable, *argv[1:]])
    reviewer = (
        "import json,os,sys; from pathlib import Path; request=json.load(sys.stdin); "
        "out={key:request[key] for key in ('version','task_id','commit','plan_identity','diff_sha256')}; "
        "out['outcome']='accepted'; Path(os.environ['TPO_REVIEW_RESULT_PATH']).write_text(json.dumps(out))"
    )
    monkeypatch.setattr(collector, 'review_argv', lambda *args, **kwargs: [sys.executable, '-c', reviewer])
    result = collector.collect_checkpoints(store, 'execution', 1, deadline_monotonic=time.monotonic() + 10)
    assert result['complete']
    assert {receipt['kind'] for receipt in journal._load()['receipts']} == {'verification', 'review'}
    attempt = store.load('execution')['attempts'][-1]
    assert attempt['cleanup'] == 'confirmed'
    assert len(attempt['owned_processes']) >= 2


def test_verification_sandbox_cannot_write_authority_or_use_network(tmp_path, monkeypatch):
    from hermes_pipeline import agent_collector as collector
    monkeypatch.setattr(collector.shutil, 'which', lambda name: '/usr/bin/bwrap')
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    authority = tmp_path / 'authority'
    authority.mkdir()
    work = tmp_path / 'work'
    (work / '.venv').mkdir(parents=True)
    argv = collector.verification_argv(['uv', 'run', 'pytest'], snapshot, authority_root=authority, seccomp_fd=10, worktree=work)
    assert '--unshare-all' in argv
    assert argv[argv.index('--ro-bind') + 1:argv.index('--ro-bind') + 3] == ['/', '/']
    assert argv.count('--bind') == 1
    assert argv[argv.index('--bind') + 1:argv.index('--bind') + 3] == [str(snapshot), str(snapshot)]
    assert 'UV_NO_SYNC' in argv
    assert str(work / '.venv') in argv
    assert argv[argv.index('--remount-ro') + 1] == str(authority)
    monkeypatch.setattr(collector.shutil, 'which', lambda name: None)
    with pytest.raises(ExecutionError, match='sandbox unavailable'):
        collector.verification_argv(['pytest'], snapshot, authority_root=authority, seccomp_fd=10)


def test_actual_verification_sandbox_preserves_host_files(tmp_path):
    from hermes_pipeline import agent_collector as collector
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    authority_root = tmp_path / 'authority'
    authority_root.mkdir()
    authority = authority_root / 'authority.json'
    authority.write_text('trusted')
    command = [sys.executable, '-c',
               "from pathlib import Path; import sys; "
               "Path('output').write_text('allowed'); "
               "\ntry: Path(sys.argv[1]).read_text()\n"
               "except OSError: pass\n"
               "else: raise AssertionError('authority readable')\n"
               "\ntry: Path(sys.argv[1]).write_text('forged')\n"
               "except OSError as exc: assert exc.errno in (1, 13, 30)\n"
               "else: raise AssertionError('authority writable')\n", str(authority)]
    with collector.verification_filter() as descriptor:
        try:
            probe_argv = collector.verification_argv(['/usr/bin/true'], snapshot, authority_root=authority_root, seccomp_fd=descriptor)
        except ExecutionError:
            pytest.skip('bwrap unavailable; production fails closed')
        probe = subprocess.run(probe_argv, capture_output=True, pass_fds=(descriptor,))
    if probe.returncode:
        pytest.skip('kernel sandbox unavailable; production fails closed')
    with collector.verification_filter() as descriptor:
        argv = collector.verification_argv(command, snapshot, authority_root=authority_root, seccomp_fd=descriptor)
        outcome = collector.run_process(argv, cwd=snapshot, stdin_bytes=b'', timeout=5,
                                        cleanup_timeout=1, env={}, pass_fds=(descriptor,))
    assert outcome['outcome'] == 'exited'
    assert outcome['exit_code'] == 0
    assert outcome['cleanup'] == 'confirmed'
    assert authority.read_text() == 'trusted'
    assert (snapshot / 'output').read_text() == 'allowed'


def test_verification_sandbox_rejects_authority_overlap(tmp_path, monkeypatch):
    from hermes_pipeline import agent_collector as collector
    monkeypatch.setattr(collector.shutil, 'which', lambda name: '/usr/bin/bwrap')
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    for authority in (snapshot, tmp_path, snapshot / 'private'):
        with pytest.raises(ExecutionError, match='containment'):
            collector.verification_argv(['pytest'], snapshot, authority_root=authority, seccomp_fd=10)


def test_actual_verification_sandbox_cannot_connect_host_unix_socket(tmp_path):
    from hermes_pipeline import agent_collector as collector
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    authority = tmp_path / 'authority'
    authority.mkdir()
    socket_path = Path.cwd() / '.hermes' / f'cs-{os.getpid()}'
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(socket_path))
        listener.listen()
        command = [sys.executable, '-c',
                   'import socket,sys\ntry:\n s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])\n'
                   'except OSError: pass\nelse: raise AssertionError("host socket reachable")', str(socket_path)]
        with collector.verification_filter() as descriptor:
            argv = collector.verification_argv(command, snapshot, authority_root=authority, seccomp_fd=descriptor)
            outcome = collector.run_process(argv, cwd=snapshot, stdin_bytes=b'', timeout=5, cleanup_timeout=1,
                                            env={}, pass_fds=(descriptor,))
    socket_path.unlink()
    assert outcome['exit_code'] == 0


def test_recovery_cannot_confirm_cleanup_after_pending_collector_launch(candidate, monkeypatch):
    from hermes_pipeline import _agent_supervisor as supervisor
    from hermes_pipeline import agent_collector as collector
    from hermes_pipeline.agent_execution import process_identity
    store, _, work = candidate
    known = [process_identity(os.getpid())]
    store.update_attempt('execution', 1, status='interrupted', cleanup='confirmed', owned_processes=known)
    monkeypatch.setattr(collector, 'run_process', lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        collector._run_owned(store, 'execution', 1, ['fake'], cwd=work, stdin_bytes=b'', env={}, deadline=time.monotonic() + 10)
    monkeypatch.setattr(supervisor, 'cleanup_processes', lambda *args, **kwargs: {'cleanup': 'confirmed', 'processes': known})
    supervisor.recover(store, 'execution', cleanup_timeout=0)
    assert store.load('execution')['attempts'][-1]['cleanup'] == 'unconfirmed'


def test_proven_no_launch_clears_collector_pending_marker(candidate, monkeypatch):
    from hermes_pipeline import agent_collector as collector
    from hermes_pipeline.agent_process import ProcessLaunchError
    store, _, work = candidate
    monkeypatch.setattr(collector, 'run_process', lambda *args, **kwargs: (_ for _ in ()).throw(ProcessLaunchError()))
    with pytest.raises(ExecutionError, match='launch failed'):
        collector._run_owned(store, 'execution', 1, ['fake'], cwd=work, stdin_bytes=b'', env={}, deadline=time.monotonic() + 10)
    assert not collector.collector_launch_pending(store, 'execution')
    assert store.load('execution')['attempts'][-1]['cleanup'] == 'confirmed'


def test_unknown_collector_marker_schema_blocks_cleanup(candidate):
    from hermes_pipeline import agent_collector as collector
    store, _, _ = candidate
    (store.root / 'execution' / 'collector-launch.json').write_text('{"version": 99, "pending": false}')
    assert collector.collector_launch_pending(store, 'execution')


def test_unsupported_verification_syscall_architecture_blocks_launch(monkeypatch):
    from hermes_pipeline import agent_collector as collector
    monkeypatch.setattr(collector.platform, 'machine', lambda: 'unknown-platform')
    with pytest.raises(ExecutionError, match='architecture unsupported'), collector.verification_filter():
        pytest.fail('unsupported syscall architecture accepted')
