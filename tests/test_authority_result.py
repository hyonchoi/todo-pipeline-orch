"""Downstream acceptance requires supervisor evidence, independent of Hermes claims."""
import json
import sys

import pytest

from hermes_pipeline import _agent_supervisor as supervisor
from hermes_pipeline import kanban_tasks, review_reconciliation
from hermes_pipeline.agent_execution import ExecutionStore
from hermes_pipeline.phases import IMPLEMENTATION_KEY
from hermes_pipeline.result_contract import (
    ResultContractError,
    load_validated_registration,
)
from tests.test_result_contract import _commit, _registered_repo, _worker_payload


def registered(tmp_path, mocker, *, phase=IMPLEMENTATION_KEY, legacy=False):
    repo, work, state, base = _registered_repo(tmp_path, legacy=legacy)
    mocker.patch.object(supervisor, 'installed_entrypoint', return_value=sys.executable)
    identity = supervisor.register_execution(
        project_dir=repo, state_dir=state, root=state / 'agent-executions',
        tick_id='01TICK', phase=phase, prompt='approved task', client='claude',
        tools='Read,Write,Edit,Bash', worktree=work, timeout=30, todo_id='TODO-42')
    store = ExecutionStore(state / 'agent-executions')
    store.admit(identity)
    head = _commit(work, 'change.txt')
    payload = _worker_payload(step_key=phase, parent=base, head=head, changed=['change.txt'])
    if phase != IMPLEMENTATION_KEY:
        payload['runs'][0]['metadata']['tpo_result']['acceptance'] = []
    return repo, work, state, base, head, store, identity, payload


def test_timed_out_worker_cannot_advance_implementation(tmp_path, mocker):
    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker)
    store.update_attempt(identity, 1, status='timed_out', exit_code=0, cleanup='confirmed')
    assert not supervisor.status(store, identity)['completion_allowed']
    tasks = {IMPLEMENTATION_KEY: kanban_tasks.KanbanTaskInfo('worker', IMPLEMENTATION_KEY, 'done', 'TODO-42')}
    mocker.patch.object(kanban_tasks, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(kanban_tasks, '_show_task_payload', return_value=payload)
    mocker.patch.object(review_reconciliation, '_show_task_payload', return_value=payload)
    assert not kanban_tasks.reconcile_plan_task_results(project_dir=repo, state_dir=state, tenant='demo', tick_id='01TICK')
    with pytest.raises(ResultContractError, match='supervisor'):
        review_reconciliation._implementation_head(tasks=tasks, registration=load_validated_registration(repo, state, '01TICK'), tick_id='01TICK')


def test_new_registration_requires_supervisor_when_record_and_marker_missing(tmp_path, mocker):
    repo, work, state, base = _registered_repo(tmp_path)
    head = _commit(work, 'change.txt')
    tasks = {IMPLEMENTATION_KEY: kanban_tasks.KanbanTaskInfo('worker', IMPLEMENTATION_KEY, 'done', 'TODO-42')}
    mocker.patch.object(kanban_tasks, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(kanban_tasks, '_show_task_payload', return_value=_worker_payload(
        step_key=IMPLEMENTATION_KEY, parent=base, head=head, changed=['change.txt']))
    assert not kanban_tasks.reconcile_plan_task_results(project_dir=repo, state_dir=state, tenant='demo', tick_id='01TICK')


def accept_result(store, identity, head, payload):
    """Install trusted control receipts, then use the real result promotion."""
    from hermes_pipeline.agent_checkpoint import ProgressJournal
    progress = ProgressJournal(store, identity)
    if store.load(identity)['registration']['manifest'] is not None:
        progress.record_receipt(1, 'task-1', head, kind='verification', evidence={
            'checks': [{'argv': ['uv', 'run', 'pytest'], 'exit_code': 0}]})
        progress.record_receipt(1, 'task-1', head, kind='review', evidence={
            'reviewer': 'independent', 'receipt_id': 'review-1', 'outcome': 'accepted'})
        path = progress.staging_directory(1) / 'checkpoint.json'
        path.write_text(json.dumps(dict(version=1, execution_id=identity, generation=1,
                                       plan_identity=store.load(identity)['registration']['plan_identity'],
                                       task_id='task-1', commit=head)))
        progress.promote(1, path.name)
    stage = supervisor.staging_directory(store, identity, 1)
    stage.mkdir(parents=True, exist_ok=True)
    (stage / 'result.json').write_text(json.dumps(payload['runs'][0]['metadata']['tpo_result']))
    supervisor.validated_result(store, identity, 1, promote=True)
    store.update_attempt(identity, 1, status='exited', exit_code=0, cleanup='confirmed')


@pytest.mark.parametrize('damage', ['result_missing', 'record_missing', 'journal_missing', 'mismatch', 'stale_generation', 'cleanup_pending'])
def test_supervisor_evidence_is_required_and_bound_to_latest_attempt(tmp_path, mocker, damage):
    from hermes_pipeline.authority_result import require_authorized_result
    from hermes_pipeline.result_contract import parse_worker_result
    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker)
    accept_result(store, identity, head, payload)
    if damage == 'result_missing':
        (store.root / identity / 'result-1.json').unlink()
    elif damage == 'record_missing':
        (store.root / identity / 'record.json').unlink()
    elif damage == 'journal_missing':
        (store.root / identity / 'progress.json').unlink()
    elif damage == 'mismatch':
        payload['runs'][0]['metadata']['tpo_result']['git']['changed_files'] = ['different.txt']
    elif damage == 'stale_generation':
        store.authorize_retry(identity, expected_generation=1, event_id='retry')
        store.admit(identity, recovery_event='retry')
        store.update_attempt(identity, 2, status='exited', exit_code=0, cleanup='confirmed')
    else:
        # Corrupt terminal cleanup is rejected without trusting card completion.
        path = store.root / identity / 'record.json'
        record = json.loads(path.read_text())
        record['attempts'][-1]['cleanup'] = 'pending'
        path.write_text(json.dumps(record))
    result = parse_worker_result(payload, tick_id='01TICK', todo_id='TODO-42',
                                 step_key=IMPLEMENTATION_KEY, acceptance_criteria=('Observable criterion',))
    with pytest.raises(ResultContractError, match='supervisor'):
        with require_authorized_result(registration=load_validated_registration(repo, state, '01TICK'),
                                       state_dir=state, tick_id='01TICK', step_key=IMPLEMENTATION_KEY, result=result):
            pytest.fail('untrusted result accepted')


def test_successful_authority_remains_valid_after_review_advances_head(tmp_path, mocker):
    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker)
    accept_result(store, identity, head, payload)
    _commit(work, 'review-fix.txt')
    tasks = {IMPLEMENTATION_KEY: kanban_tasks.KanbanTaskInfo('worker', IMPLEMENTATION_KEY, 'done', 'TODO-42'),
             'review:0': kanban_tasks.KanbanTaskInfo('review', 'review:0', 'running', 'TODO-42')}
    mocker.patch.object(kanban_tasks, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(kanban_tasks, '_show_task_payload', return_value=payload)
    mocker.patch.object(review_reconciliation, '_show_task_payload', return_value=payload)
    assert kanban_tasks.reconcile_plan_task_results(project_dir=repo, state_dir=state, tenant='demo', tick_id='01TICK')
    assert review_reconciliation._implementation_head(tasks=tasks, registration=load_validated_registration(repo, state, '01TICK'), tick_id='01TICK') == head


def test_timed_out_finish_blocks_before_delivery_or_marker(tmp_path, mocker):
    from hermes_pipeline import todos_completion
    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker, phase='finish')
    store.update_attempt(identity, 1, status='timed_out', exit_code=0, cleanup='confirmed')
    payload['runs'][0]['metadata']['tpo_result']['delivery'] = {
        'pr_url': 'https://github.com/acme/repo/pull/7', 'branch': 'todo-42', 'head_sha': head,
        'checks': [{'command': 'uv run pytest', 'exit_code': 0}]}
    tasks = {'finish': kanban_tasks.KanbanTaskInfo('finish-card', 'finish', 'done', 'TODO-42')}
    mocker.patch.object(todos_completion, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(todos_completion, '_show_task_payload', return_value=payload)
    github = mocker.patch.object(todos_completion, '_github_identity', side_effect=AssertionError('network boundary'))
    delivery = mocker.patch.object(todos_completion, '_pr_view', side_effect=AssertionError('network boundary'))
    assert not todos_completion.reconcile_todo_completion(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo', repo='acme/repo')
    assert not (state / 'runs' / '01TICK' / 'finish-verified').exists()
    github.assert_not_called()
    delivery.assert_not_called()


def test_timed_out_review_cannot_write_accepted_head(tmp_path, mocker):
    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker, phase='review:0')
    store.update_attempt(identity, 1, status='timed_out', exit_code=0, cleanup='confirmed')
    tasks = {'review:0': kanban_tasks.KanbanTaskInfo('review-card', 'review:0', 'done', 'TODO-42')}
    mocker.patch.object(review_reconciliation, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(review_reconciliation, '_show_task_payload', return_value=payload)
    mocker.patch.object(review_reconciliation, '_ensure_initial_review')
    mocker.patch.object(review_reconciliation, '_implementation_head', return_value=base)
    assert not review_reconciliation.reconcile_reviews(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo')
    assert not review_reconciliation._accepted_head_path(state, '01TICK').exists()


def test_legacy_registration_remains_inspectable_until_supervisor_enrollment(tmp_path, mocker):
    import shutil

    from hermes_pipeline.authority_result import mark_supervised_run
    repo, work, state, base = _registered_repo(tmp_path, legacy=True)
    authority = load_validated_registration(repo, state, '01TICK')
    assert not authority.supervised_execution
    head = _commit(work, 'change.txt')
    tasks = {IMPLEMENTATION_KEY: kanban_tasks.KanbanTaskInfo('worker', IMPLEMENTATION_KEY, 'done', 'TODO-42')}
    mocker.patch.object(kanban_tasks, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(kanban_tasks, '_show_task_payload', return_value=_worker_payload(
        step_key=IMPLEMENTATION_KEY, parent=base, head=head, changed=['change.txt']))
    assert kanban_tasks.reconcile_plan_task_results(project_dir=repo, state_dir=state, tenant='demo', tick_id='01TICK')
    mark_supervised_run(state, '01TICK')
    # Losing all execution records cannot turn an enrolled run back into legacy.
    root = state / 'agent-executions'
    root.mkdir(exist_ok=True)
    shutil.rmtree(root)
    assert not kanban_tasks.reconcile_plan_task_results(project_dir=repo, state_dir=state, tenant='demo', tick_id='01TICK')


def test_review_generation_is_locked_until_accepted_head_is_persisted(tmp_path, mocker):
    from hermes_pipeline.agent_execution import LockUnconfirmed
    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker, phase='review:0')
    accept_result(store, identity, head, payload)
    tasks = {'review:0': kanban_tasks.KanbanTaskInfo('review-card', 'review:0', 'done', 'TODO-42')}
    mocker.patch.object(review_reconciliation, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(review_reconciliation, '_show_task_payload', return_value=payload)
    mocker.patch.object(review_reconciliation, '_ensure_initial_review')
    mocker.patch.object(review_reconciliation, '_implementation_head', return_value=base)
    persist = review_reconciliation._persist_accepted_head
    def competing_retry(*args):
        other = ExecutionStore(store.root)
        with pytest.raises(LockUnconfirmed):
            other.authorize_retry(identity, expected_generation=1, event_id='racing-retry')
        persist(*args)
    mocker.patch.object(review_reconciliation, '_persist_accepted_head', side_effect=competing_retry)
    assert review_reconciliation.reconcile_reviews(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo')
    assert review_reconciliation._accepted_head_path(state, '01TICK').read_text().strip() == head


def test_missing_control_receipts_reject_promoted_result(tmp_path, mocker):
    from hermes_pipeline.agent_checkpoint import _digest
    from hermes_pipeline.authority_result import require_authorized_result
    from hermes_pipeline.result_contract import parse_worker_result
    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker)
    accept_result(store, identity, head, payload)
    directory = store.root / identity
    journal = json.loads((directory / 'progress.json').read_text())
    journal['receipts'] = []
    (directory / 'progress.json').write_text(json.dumps(journal))
    (directory / 'progress-anchor.json').write_text(json.dumps({'version': 1, 'digest': _digest(journal)}))
    result = parse_worker_result(payload, tick_id='01TICK', todo_id='TODO-42',
                                 step_key=IMPLEMENTATION_KEY, acceptance_criteria=('Observable criterion',))
    with pytest.raises(ResultContractError, match='supervisor'):
        with require_authorized_result(registration=load_validated_registration(repo, state, '01TICK'),
                                       state_dir=state, tick_id='01TICK', step_key=IMPLEMENTATION_KEY, result=result):
            pytest.fail('receipt-free completion accepted')


def test_register_execution_marks_enrolled_legacy_run_before_dispatch(tmp_path, mocker):
    import shutil
    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker, legacy=True)
    assert not load_validated_registration(repo, state, '01TICK').supervised_execution
    assert json.loads((state / 'runs' / '01TICK' / 'supervisor-required.json').read_text()) == {
        'version': 1, 'authority': 'agent-executions'}
    shutil.rmtree(store.root)
    tasks = {IMPLEMENTATION_KEY: kanban_tasks.KanbanTaskInfo('worker', IMPLEMENTATION_KEY, 'done', 'TODO-42')}
    mocker.patch.object(kanban_tasks, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(kanban_tasks, '_show_task_payload', return_value=payload)
    assert not kanban_tasks.reconcile_plan_task_results(project_dir=repo, state_dir=state, tenant='demo', tick_id='01TICK')


def accepted_chain(tmp_path, mocker, *, finish=False):
    """Real modern implementation and no-change review, with trusted receipts."""
    repo, work, state, base, head, store, implementation, implementation_payload = registered(tmp_path, mocker)
    accept_result(store, implementation, head, implementation_payload)
    payloads = {'worker': implementation_payload}
    tasks = {IMPLEMENTATION_KEY: kanban_tasks.KanbanTaskInfo('worker', IMPLEMENTATION_KEY, 'done', 'TODO-42')}
    identities = {IMPLEMENTATION_KEY: implementation}
    for phase in (['review:0', 'finish'] if finish else ['review:0']):
        identity = supervisor.register_execution(
            project_dir=repo, state_dir=state, root=store.root, tick_id='01TICK', phase=phase,
            prompt='approved phase', client='claude', tools='Read,Write,Edit,Bash', worktree=work,
            timeout=30, todo_id='TODO-42')
        store.admit(identity)
        payload = _worker_payload(step_key=phase, parent=head, head=head, changed=[])
        raw = payload['runs'][0]['metadata']['tpo_result']
        raw['acceptance'] = []
        if phase == 'finish':
            raw['delivery'] = {'pr_url': 'https://github.com/acme/repo/pull/7', 'branch': 'todo-42',
                               'head_sha': head, 'checks': [{'command': 'uv run pytest', 'exit_code': 0}]}
        accept_result(store, identity, head, payload)
        payloads[phase] = payload
        identities[phase] = identity
        tasks[phase] = kanban_tasks.KanbanTaskInfo(phase, phase, 'done', 'TODO-42')
    return repo, work, state, head, store, identities, tasks, payloads


def approved_retry(store, identity):
    from hermes_pipeline.agent_recovery import (
        approve_recovery,
        consume_recovery,
        prepare_recovery,
    )
    preview = prepare_recovery(store, identity)
    approve_recovery(store, identity, preview)
    event = preview['event_id']
    consume_recovery(store, identity, event)
    store.authorize_retry(identity, expected_generation=preview['generation'], event_id=event)
    return event


def test_implementation_retry_cannot_race_review_acceptance(tmp_path, mocker):
    from hermes_pipeline.agent_execution import LockUnconfirmed
    repo, work, state, head, store, identities, tasks, payloads = accepted_chain(tmp_path, mocker)
    mocker.patch.object(review_reconciliation, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(review_reconciliation, '_show_task_payload', side_effect=payloads.__getitem__)
    persist = review_reconciliation._persist_accepted_head
    def retry_before_persist(*args):
        other = ExecutionStore(store.root)
        with pytest.raises(LockUnconfirmed):
            event = approved_retry(other, identities[IMPLEMENTATION_KEY])
            other.admit(identities[IMPLEMENTATION_KEY], recovery_event=event)
        persist(*args)
    mocker.patch.object(review_reconciliation, '_persist_accepted_head', side_effect=retry_before_persist)
    assert review_reconciliation.reconcile_reviews(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo')
    assert len(store.load(identities[IMPLEMENTATION_KEY])['attempts']) == 1


@pytest.mark.parametrize('phase', [IMPLEMENTATION_KEY, 'review:0'])
@pytest.mark.parametrize('outcome', ['admitted', 'timed_out'])
def test_finish_rejects_stale_accepted_head_after_upstream_retry(tmp_path, mocker, phase, outcome):
    from hermes_pipeline import todos_completion
    repo, work, state, head, store, identities, tasks, payloads = accepted_chain(tmp_path, mocker, finish=True)
    review_reconciliation._persist_accepted_head(state, '01TICK', head)
    event = approved_retry(store, identities[phase])
    store.admit(identities[phase], recovery_event=event)
    if outcome == 'timed_out':
        store.update_attempt(identities[phase], 2, status='timed_out', exit_code=0, cleanup='confirmed')
    # Even a result copied into the new generation cannot substitute for its
    # current terminal acceptance; exercise more than the missing-file guard.
    directory = store.root / identities[phase]
    (directory / 'result-2.json').write_bytes((directory / 'result-1.json').read_bytes())
    mocker.patch.object(todos_completion, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(todos_completion, '_show_task_payload', side_effect=payloads.__getitem__)
    external = mocker.patch.object(todos_completion, '_github_identity', side_effect=AssertionError('external boundary'))
    assert not todos_completion.reconcile_todo_completion(project_dir=repo, state_dir=state,
                                                           tick_id='01TICK', tenant='demo', repo='acme/repo')
    assert not (state / 'runs' / '01TICK' / 'finish-verified').exists()
    external.assert_not_called()


@pytest.mark.parametrize('consumer', ['review', 'finish'])
def test_active_worktree_owner_is_polled_without_acceptance(tmp_path, mocker, consumer):
    from hermes_pipeline import todos_completion
    repo, work, state, head, store, identities, tasks, payloads = accepted_chain(tmp_path, mocker, finish=True)
    module = review_reconciliation if consumer == 'review' else todos_completion
    board = mocker.patch.object(module, 'get_todo_kanban_tasks', side_effect=AssertionError('busy run must wait'))
    # Same kernel lock that a live supervisor holds throughout its attempt.
    with store.worktree_locked(identities['finish']):
        if consumer == 'review':
            waiting = module.reconcile_reviews(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo')
        else:
            waiting = module.reconcile_todo_completion(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo', repo='acme/repo')
    assert waiting
    board.assert_not_called()
    assert not (state / 'runs' / '01TICK' / 'finish-verified').exists()
    assert not review_reconciliation._accepted_head_path(state, '01TICK').exists()


def test_review_retry_cannot_race_finish_delivery(tmp_path, mocker):
    from hermes_pipeline import todos_completion
    from hermes_pipeline.agent_execution import LockUnconfirmed
    from tests.test_result_contract import _git
    repo, work, state, head, store, identities, tasks, payloads = accepted_chain(tmp_path, mocker, finish=True)
    review_reconciliation._persist_accepted_head(state, '01TICK', head)
    _git(work, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
    todos_completion._delivery_authority(state, '01TICK', work, repo='acme/repo', create=True)
    mocker.patch.object(todos_completion, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(todos_completion, '_show_task_payload', side_effect=payloads.__getitem__)
    def retry_at_delivery(*args):
        other = ExecutionStore(store.root)
        with pytest.raises(LockUnconfirmed):
            event = approved_retry(other, identities['review:0'])
            other.admit(identities['review:0'], recovery_event=event)
        return {'url': 'https://github.com/acme/repo/pull/7', 'state': 'OPEN',
                'headRefName': 'todo-42', 'baseRefName': 'main', 'headRefOid': head,
                'headRepository': {'nameWithOwner': 'acme/repo'}, 'isCrossRepository': False}
    view = mocker.patch.object(todos_completion, '_pr_view', side_effect=retry_at_delivery)
    mocker.patch.object(todos_completion, '_remote_head', return_value=head)
    mocker.patch.object(todos_completion, '_check_state', return_value='pending')
    assert todos_completion.reconcile_todo_completion(project_dir=repo, state_dir=state,
                                                       tick_id='01TICK', tenant='demo', repo='acme/repo')
    view.assert_called_once()
    assert (state / 'runs' / '01TICK' / 'finish-verified').read_text().strip() == head
    assert len(store.load(identities['review:0'])['attempts']) == 1


def test_implementation_retry_before_review_entry_blocks_acceptance(tmp_path, mocker):
    repo, work, state, head, store, identities, tasks, payloads = accepted_chain(tmp_path, mocker)
    event = approved_retry(store, identities[IMPLEMENTATION_KEY])
    store.admit(identities[IMPLEMENTATION_KEY], recovery_event=event)
    mocker.patch.object(review_reconciliation, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(review_reconciliation, '_show_task_payload', side_effect=payloads.__getitem__)
    assert not review_reconciliation.reconcile_reviews(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo')
    assert not review_reconciliation._accepted_head_path(state, '01TICK').exists()


def test_unsupported_worktree_lock_blocks_instead_of_reporting_busy(tmp_path, mocker):
    import errno
    repo, work, state, head, store, identities, tasks, payloads = accepted_chain(tmp_path, mocker)
    mocker.patch('hermes_pipeline.agent_execution.fcntl.flock',
                 side_effect=OSError(errno.EOPNOTSUPP, 'unsupported locking'))
    board = mocker.patch.object(review_reconciliation, 'get_todo_kanban_tasks',
                               side_effect=AssertionError('unconfirmed lock must block'))
    assert not review_reconciliation.reconcile_reviews(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo')
    board.assert_not_called()
    assert not review_reconciliation._accepted_head_path(state, '01TICK').exists()


def test_immediate_publication_dispatch_waits_then_admits_once(tmp_path, mocker):
    import io
    import subprocess
    from contextlib import redirect_stdout
    from types import SimpleNamespace

    repo, work, state, base, head, store, identity, payload = registered(tmp_path, mocker)
    accept_result(store, identity, head, payload)
    tasks = {IMPLEMENTATION_KEY: kanban_tasks.KanbanTaskInfo('worker', IMPLEMENTATION_KEY, 'done', 'TODO-42')}
    mocker.patch.object(review_reconciliation, 'get_todo_kanban_tasks', return_value=tasks)
    mocker.patch.object(review_reconciliation, '_show_task_payload', return_value=payload)
    mocker.patch.object(review_reconciliation, '_find_task_id_in_snapshot', return_value=None)
    clock = [0.0]
    def monotonic():
        clock[0] += 0.1
        return clock[0]
    mocker.patch.object(supervisor, 'time', SimpleNamespace(monotonic=monotonic, sleep=lambda _: None))
    original_run = subprocess.run
    published = []
    def immediate_dispatch(argv, **kwargs):
        if argv[:3] != ['hermes', 'kanban', 'create']:
            return original_run(argv, **kwargs)
        header = json.loads(argv[argv.index('--body') + 1].splitlines()[0])
        execution = header['execution_id']
        stream = io.StringIO()
        with redirect_stdout(stream):
            rc = supervisor.main(['run', '--root', str(store.root), '--execution', execution])
        report = json.loads(stream.getvalue())
        assert rc == 0
        assert report['status'] == 'waiting_for_admission'
        assert report['generation'] == 0
        assert not report['completion_allowed']
        assert store.load(execution)['attempts'] == []
        published.append(execution)
        return subprocess.CompletedProcess(argv, 0, json.dumps({'id': 't_1234abcd'}), '')
    mocker.patch.object(subprocess, 'run', side_effect=immediate_dispatch)
    assert review_reconciliation.reconcile_reviews(project_dir=repo, state_dir=state, tick_id='01TICK', tenant='demo')
    assert len(published) == 1
    execution = published[0]
    # Once publication releases the worktree, reentry launches one daemon and
    # real admission happens once. Substitute only the external process boundary.
    mocker.patch.object(supervisor, '_prepare_launch', return_value=([], {}))
    def daemon(*args, **kwargs):
        another = ExecutionStore(store.root)
        another.admit(execution)
        another.update_attempt(execution, 1, status='running', deadline_monotonic=123.0)
    launch = mocker.patch.object(subprocess, 'Popen', side_effect=daemon)
    for _ in range(2):
        assert supervisor.attach(ExecutionStore(store.root), execution)['generation'] == 1
    launch.assert_called_once()
    attempts = store.load(execution)['attempts']
    assert len(attempts) == 1
    assert attempts[0]['deadline_monotonic'] == 123.0


@pytest.mark.parametrize('version', [True, 1.0])
def test_enrollment_marker_requires_exact_integer_version(tmp_path, mocker, version):
    repo, work, state, base = _registered_repo(tmp_path, legacy=True)
    marker = state / 'runs' / '01TICK' / 'supervisor-required.json'
    marker.write_text(json.dumps({'version': version, 'authority': 'agent-executions'}))
    assert not review_reconciliation.reconcile_reviews(project_dir=repo, state_dir=state,
                                                       tick_id='01TICK', tenant='demo')
