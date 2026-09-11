import json
import os
import subprocess
import sys
import time

import pytest

from hermes_pipeline.agent_checkpoint import ProgressJournal
from hermes_pipeline.agent_execution import ExecutionError, ExecutionStore


def git(work, *args):
    return subprocess.check_output(['git', '-C', str(work), *args], text=True).strip()


@pytest.fixture
def candidate(tmp_path, request):
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
                                        'verification': getattr(request, 'param', ['python check.py']),
                                        'acceptance_criteria': ['File exists']} ]})
    journal = ProgressJournal(store, 'execution')
    journal.initialize()
    store.admit('execution')
    (work / 'check.py').write_text('assert True\n')
    if getattr(request, 'param', None) == ['uv run pytest']:
        (work / '.venv').symlink_to(sys.prefix, target_is_directory=True)
        (work / 'pyproject.toml').write_text('[project]\nname="checkpoint-probe"\nversion="0.0.0"\n')
        (work / 'test_probe.py').write_text('import socket\ndef test_network_available():\n    with socket.socket() as connection:\n        connection.bind(("127.0.0.1", 0))\n')
        git(work, 'add', 'pyproject.toml', 'test_probe.py')
    git(work, 'add', 'check.py')
    git(work, 'commit', '-m', 'task one')
    return store, journal, work


def fake_clients(monkeypatch, *, verification_exit=0, verdict='accepted', mutate=None):
    from hermes_pipeline import agent_collector as collector
    calls = []
    monkeypatch.setattr(collector, 'review_argv', lambda client, snapshot, staging, **kwargs: ['review'])

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
        return {'outcome': 'exited', 'exit_code': verification_exit if argv[0] != 'review' else 0,
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


@pytest.mark.parametrize('context_call', [1, 2, 4])
def test_collection_deadline_includes_journal_revalidation(candidate, monkeypatch, context_call):
    from hermes_pipeline import agent_collector as collector
    store, journal, _ = candidate
    fake_clients(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    original = ProgressJournal.recovery_context
    calls = 0

    def delayed(self, generation):
        nonlocal calls
        result = original(self, generation)
        calls += 1
        if calls == context_call:
            clock[0] = 201.0
        return result

    monkeypatch.setattr(ProgressJournal, 'recovery_context', delayed)
    with pytest.raises(collector.CollectionTimedOut):
        collector.collect_checkpoints(store, 'execution', 1, deadline_monotonic=110.0)
    # Expired scope must not contaminate historical read-only queries.
    history = journal.recovery_context(1)
    if context_call <= 2:
        assert history['accepted'] == []
        assert journal._load()['receipts'] == []


def test_collection_git_queries_share_remaining_budget(candidate, monkeypatch):
    from hermes_pipeline import agent_collector as collector
    from hermes_pipeline import agent_git
    store, journal, _ = candidate
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    original = subprocess.run
    timeouts = []

    def consume(argv, **kwargs):
        timeouts.append(kwargs['timeout'])
        value = original(argv, **kwargs)
        clock[0] += 3
        return value

    with monkeypatch.context() as scoped:
        scoped.setattr(agent_git.subprocess, 'run', consume)
        with pytest.raises(collector.CollectionTimedOut):
            collector.collect_checkpoints(store, 'execution', 1, deadline_monotonic=110.0)
    assert timeouts == [10.0, 7.0, 4.0, 1.0]
    assert journal.recovery_context(1)['accepted'] == []


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
    # Real process ownership and exit collection with a provider-free reviewer.
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


def test_pinned_check_is_executed_directly(candidate, monkeypatch):
    from hermes_pipeline import agent_collector as collector
    store, _, work = candidate
    calls = fake_clients(monkeypatch)
    collector.collect_checkpoints(store, 'execution', 1, deadline_monotonic=time.monotonic() + 10)
    assert calls[0][0] == ['python', 'check.py']
    assert calls[0][1]['env']['PYTHONPATH'] == str(calls[0][1]['cwd'])




@pytest.mark.parametrize('candidate', [['uv run pytest']], indirect=True)
def test_exact_pinned_uv_run_pytest_collects_real_evidence(candidate, monkeypatch):
    from hermes_pipeline import agent_collector as collector
    store, journal, _ = candidate
    reviewer = (
        "import json,os,sys; from pathlib import Path; request=json.load(sys.stdin); "
        "out={key:request[key] for key in ('version','task_id','commit','plan_identity','diff_sha256')}; "
        "out['outcome']='accepted'; Path(os.environ['TPO_REVIEW_RESULT_PATH']).write_text(json.dumps(out))"
    )
    monkeypatch.setattr(collector, 'review_argv', lambda *args, **kwargs: [sys.executable, '-c', reviewer])
    result = collector.collect_checkpoints(store, 'execution', 1, deadline_monotonic=time.monotonic() + 20)
    assert result['complete']
    verification = next(receipt for receipt in journal._load()['receipts'] if receipt['kind'] == 'verification')
    assert verification['evidence']['checks'] == [{'argv': ['uv', 'run', 'pytest'], 'exit_code': 0}]
    assert store.load('execution')['attempts'][-1]['cleanup'] == 'confirmed'


@pytest.mark.parametrize('candidate', [['python check.py'], ['pytest check.py']], indirect=True)
def test_checks_use_project_environment_and_snapshot_src(candidate, monkeypatch):
    import venv

    from hermes_pipeline import agent_collector as collector
    store, journal, work = candidate
    environment = work / '.venv'
    venv.EnvBuilder(with_pip=False).create(environment)
    source = work / 'src' / 'checkpoint_probe'
    source.mkdir(parents=True)
    (source / '__init__.py').write_text('VALUE = "committed"\n')
    site_packages = next((environment / 'lib').glob('python*/site-packages'))
    (site_packages / 'editable-probe.pth').write_text(str(work / 'src') + '\n')
    # A project-local console script also verifies bare command PATH selection.
    runner = environment / 'bin' / 'pytest'
    runner.write_text(f'#!{environment / "bin" / "python"}\nimport runpy,sys\nrunpy.run_path(sys.argv[1])\n')
    runner.chmod(0o755)
    (work / 'check.py').write_text(
        'import sys\nfrom pathlib import Path\nimport checkpoint_probe\n'
        f'assert Path(sys.prefix) == Path({str(environment)!r})\n'
        'assert checkpoint_probe.VALUE == "committed"\n'
        'assert Path(checkpoint_probe.__file__).is_relative_to(Path.cwd() / "src")\n')
    git(work, 'add', 'src', 'check.py')
    git(work, 'commit', '--amend', '--no-edit')
    # The existing editable environment points here; checks must prefer the snapshot.
    (source / '__init__.py').write_text('VALUE = "unfinished"\n')
    reviewer = (
        "import json,os,sys; from pathlib import Path; request=json.load(sys.stdin); "
        "out={key:request[key] for key in ('version','task_id','commit','plan_identity','diff_sha256')}; "
        "out['outcome']='accepted'; Path(os.environ['TPO_REVIEW_RESULT_PATH']).write_text(json.dumps(out))"
    )
    monkeypatch.setattr(collector, 'review_argv', lambda *args, **kwargs: [sys.executable, '-c', reviewer])
    result = collector.collect_checkpoints(store, 'execution', 1, deadline_monotonic=time.monotonic() + 20)
    assert result['complete']
    assert journal.recovery_context(1)['accepted']
    assert (source / '__init__.py').read_text() == 'VALUE = "unfinished"\n'


@pytest.mark.parametrize('dirty', [False, True])
def test_review_handoff_supplies_response_path_git_facts_and_passed_checks(candidate, monkeypatch, dirty):
    from hermes_pipeline import agent_collector as collector
    store, journal, work = candidate
    if dirty:
        (work / 'unfinished.txt').write_text('preserve this')
    expected_head = git(work, 'rev-parse', 'HEAD')
    reviewer = (
        "import json,sys; from pathlib import Path; request=json.load(sys.stdin); "
        f"assert request['original_worktree'] == {{'head': {expected_head!r}, 'clean': {not dirty!r}}}; "
        "assert request['verification'] == [{'argv': ['python', 'check.py'], 'exit_code': 0}]; "
        "assert 'Do not rerun' in request['instruction']; "
        "assert 'no Git metadata' in request['instruction']; "
        "response=Path(request['response_path']); assert response.is_absolute(); "
        "out={key:request[key] for key in ('version','task_id','commit','plan_identity','diff_sha256')}; "
        "out['outcome']='accepted'; response.write_text(json.dumps(out))"
    )
    monkeypatch.setattr(collector, 'review_argv', lambda *args, **kwargs: [sys.executable, '-c', reviewer])
    result = collector.collect_checkpoints(store, 'execution', 1, deadline_monotonic=time.monotonic() + 20)
    assert result['complete']
    assert journal.recovery_context(1)['accepted'][0]['commit'] == expected_head
    if dirty:
        assert (work / 'unfinished.txt').read_text() == 'preserve this'
