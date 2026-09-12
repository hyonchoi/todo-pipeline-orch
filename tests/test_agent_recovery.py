import json
import subprocess

import pytest

from hermes_pipeline.agent_checkpoint import ProgressJournal
from hermes_pipeline.agent_execution import ExecutionError, ExecutionStore
from hermes_pipeline.agent_recovery import (
    approve_recovery,
    auto_approve_resume,
    consume_recovery,
    invalidate_recovery,
    pending_auto_recovery,
    prepare_recovery,
    recovery_state,
    validate_recovery,
)


def git(path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True)


@pytest.fixture
def recovery(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    git(tree, 'init', '-b', 'task')
    git(tree, 'config', 'user.email', 'test@example.invalid')
    git(tree, 'config', 'user.name', 'Test')
    (tree / 'tracked').write_bytes(b'original\x00')
    git(tree, 'add', 'tracked')
    git(tree, 'commit', '-m', 'base')

    store = ExecutionStore(tmp_path / 'state')
    store.register('run', registration_id='registration', plan_identity='a'*64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(tree), branch='task', result_contract={}, timeout=30)

    ProgressJournal(store, 'run').initialize()

    store.admit('run')
    store.update_attempt('run', 1, status='interrupted', cleanup='confirmed')

    return store, tree


def test_approved_recovery_preserves_binary_partial_work_and_is_single_use(recovery):
    store, tree = recovery
    (tree / 'tracked').write_bytes(b'staged\x00')
    git(tree, 'add', 'tracked')
    (tree / 'tracked').write_bytes(b'unstaged\x00')
    (tree / 'untracked').write_bytes(b'partial\xff')
    preview = prepare_recovery(store, 'run', 'recovery_only')
    event = approve_recovery(store, 'run', preview)
    assert validate_recovery(store, 'run', event)['mode'] == 'recovery_only'
    consume_recovery(store, 'run', event)
    with pytest.raises(ExecutionError):
        validate_recovery(store, 'run', event)
    assert (tree / 'tracked').read_bytes() == b'unstaged\x00'
    assert git(tree, 'show', ':tracked').stdout == b'staged\x00'


@pytest.mark.parametrize('kind', ['tracked', 'untracked', 'index', 'head'])
def test_changed_state_invalidates_approval(recovery, kind):
    store, tree = recovery
    (tree / 'untracked').write_bytes(b'one')
    preview = prepare_recovery(store, 'run', 'recovery_only')
    event = approve_recovery(store, 'run', preview)
    if kind == 'head':
        git(tree, 'commit', '--allow-empty', '-m', 'new')
    elif kind == 'index':
        git(tree, 'add', 'untracked')
    else:
        (tree / kind).write_bytes(b'two')
    with pytest.raises(ExecutionError):
        validate_recovery(store, 'run', event)


def test_worker_cannot_approve_and_legacy_cannot_resume(recovery, monkeypatch):
    store, tree = recovery
    # Test: legacy evidence cannot resume
    # Create a new execution without progress.json
    store.register('legacy', registration_id='registration', plan_identity='a'*64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(tree), branch='task', result_contract={}, timeout=30)
    store.admit('legacy')
    store.update_attempt('legacy', 1, status='interrupted', cleanup='confirmed')
    # Don't create progress.json for this execution

    with pytest.raises(ExecutionError):
        prepare_recovery(store, 'legacy', 'resume')
    preview = prepare_recovery(store, 'legacy', 'recovery_only')
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker')
    with pytest.raises(ExecutionError):
        approve_recovery(store, 'legacy', preview)


@pytest.mark.parametrize('state', ['index.lock', 'MERGE_HEAD', 'rebase-merge'])
def test_unsafe_git_state_blocks_preview(recovery, state):
    store, tree = recovery
    (tree / '.git' / state).write_text('unsafe')
    with pytest.raises(ExecutionError):
        prepare_recovery(store, 'run', 'recovery_only')


def test_preview_tampering_and_unconfirmed_cleanup_block(recovery):
    store, _ = recovery
    preview = prepare_recovery(store, 'run', 'recovery_only')
    with pytest.raises(ExecutionError):
        approve_recovery(store, 'run', {**preview, 'unexpected': True})
    event = approve_recovery(store, 'run', preview)
    store.update_attempt('run', 1, cleanup='unconfirmed')
    with pytest.raises(ExecutionError):
        validate_recovery(store, 'run', event)


@pytest.mark.parametrize('tamper', ['version', 'field', 'symlink', 'oversize', 'bad_approver'])
def test_private_intent_rejects_malformed_or_redirected_records(recovery, tamper):
    store, tree = recovery
    if tamper == 'bad_approver':
        result = auto_approve_resume(store, 'run')
        event = result['event_id']
    else:
        preview = prepare_recovery(store, 'run', 'recovery_only')
        event = approve_recovery(store, 'run', preview)

    path = store.root / 'run' / 'recovery-intent.json'
    intent = json.loads(path.read_text())

    if tamper == 'symlink':
        target = tree / 'forged-intent'
        target.write_text(path.read_text())
        path.unlink()
        path.symlink_to(target)
    elif tamper == 'oversize':
        path.write_bytes(b' ' * (256 * 1024 + 1))
    elif tamper == 'bad_approver':
        intent['approver'] = 'worker'
        path.write_text(json.dumps(intent))
    else:
        intent['version' if tamper == 'version' else 'unknown'] = 99
        path.write_text(json.dumps(intent))

    with pytest.raises(ExecutionError):
        validate_recovery(store, 'run', event)


def test_pending_auto_recovery_raises_on_malformed_intent(recovery):
    """Malformed intent should raise in pending_auto_recovery and recovery_state."""
    store, tree = recovery
    result = auto_approve_resume(store, 'run')
    assert result['approved'] is True

    # Write garbage JSON with correct keys but invalid status
    path = store.root / 'run' / 'recovery-intent.json'
    intent = json.loads(path.read_text())
    intent['status'] = 'bogus'
    path.write_text(json.dumps(intent))

    with pytest.raises(ExecutionError):
        pending_auto_recovery(store, 'run')

    with pytest.raises(ExecutionError):
        recovery_state(store, 'run')


def test_untracked_symlink_is_not_followed(recovery):
    store, tree = recovery
    (tree / 'escape').symlink_to(store.root / 'run' / 'record.json')
    with pytest.raises(ExecutionError):
        prepare_recovery(store, 'run', 'recovery_only')


def test_preview_cannot_be_approved_after_binary_change(recovery):
    store, tree = recovery
    preview = prepare_recovery(store, 'run', 'recovery_only')
    (tree / 'tracked').write_bytes(b'changed\x00')
    with pytest.raises(ExecutionError):
        approve_recovery(store, 'run', preview)


def test_auto_approve_resume_approves_timed_out_attempt(recovery):
    """Auto-approval should work for terminal status (fixture uses 'interrupted')."""
    store, tree = recovery
    # Fixture already sets attempt to 'interrupted' (which is TERMINAL)

    result = auto_approve_resume(store, 'run')
    assert result['approved'] is True
    assert result['reason'] == 'recovery_approved'
    assert result['event_id'] is not None
    assert result['generation'] == 1
    assert result['mode'] == 'resume'

    # Check intent has correct approver
    path = store.root / 'run' / 'recovery-intent.json'
    intent = json.loads(path.read_text())
    assert intent['approver'] == 'tick'
    assert intent['status'] == 'approved'

    # validate_recovery should succeed
    event = result['event_id']
    preview = validate_recovery(store, 'run', event)
    assert preview['mode'] == 'resume'

    # pending_auto_recovery should return the event
    assert pending_auto_recovery(store, 'run') == event

    # recovery_state should show approved
    state = recovery_state(store, 'run')
    assert state['state'] == 'approved'
    assert state['approver'] == 'tick'


def test_auto_approve_resume_is_idempotent_until_state_changes(recovery):
    """Second call with same event should return already_approved; new state creates new event."""
    store, tree = recovery
    # Fixture already has 'interrupted' (TERMINAL) status

    # First call
    result1 = auto_approve_resume(store, 'run')
    event1 = result1['event_id']
    assert result1['reason'] == 'recovery_approved'

    # Second call (idempotent)
    result2 = auto_approve_resume(store, 'run')
    assert result2['approved'] is True
    assert result2['event_id'] == event1
    assert result2['reason'] == 'recovery_already_approved'

    # Write an untracked file (state change)
    (tree / 'newfile').write_bytes(b'content')

    # Third call (new event due to state change)
    result3 = auto_approve_resume(store, 'run')
    assert result3['approved'] is True
    assert result3['event_id'] != event1
    assert result3['reason'] == 'recovery_approved'


@pytest.mark.parametrize('refusal', [
    'recovery_outcome_ineligible',
    'recovery_cleanup_unconfirmed',
    'recovery_generation_exhausted',
    'recovery_worktree_unsafe',
])
def test_auto_approve_resume_refusals(recovery, refusal):
    """Test various refusal scenarios."""
    store, tree = recovery
    execution_id = 'run'

    # For outcome_ineligible, we need a non-terminal status
    # Create a fresh execution for this case
    if refusal == 'recovery_outcome_ineligible':
        execution_id = 'run_exited'
        store.register(execution_id, registration_id='registration', plan_identity='a'*64,
                      phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                      worktree=str(tree), branch='task', result_contract={}, timeout=30)
        store.admit(execution_id)
        store.update_attempt(execution_id, 1, status='exited', cleanup='confirmed', exit_code=1)
    elif refusal == 'recovery_cleanup_unconfirmed':
        store.update_attempt(execution_id, 1, cleanup='unconfirmed')
    elif refusal == 'recovery_worktree_unsafe':
        (tree / '.git' / 'index.lock').write_text('unsafe')

    # For generation exhausted test, set max_generations=1
    max_gen = 1 if refusal == 'recovery_generation_exhausted' else 3

    result = auto_approve_resume(store, execution_id, max_generations=max_gen)

    assert result['approved'] is False
    assert result['reason'] == refusal
    assert result['event_id'] is None

    # Verify no intent was written for refusals
    path = store.root / execution_id / 'recovery-intent.json'
    assert not path.exists()

def test_auto_approve_ignores_worker_environment(recovery, monkeypatch):
    """auto_approve_resume should work even with worker env vars set."""
    store, tree = recovery
    # Fixture already has 'interrupted' (TERMINAL) status

    monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker-task')

    # auto_approve_resume should succeed
    result = auto_approve_resume(store, 'run')
    assert result['approved'] is True

    # But validate_recovery and consume_recovery should succeed (no _operator call)
    event = result['event_id']
    preview = validate_recovery(store, 'run', event)
    assert preview is not None

    consume_recovery(store, 'run', event)

    # But prepare_recovery should still fail
    with pytest.raises(ExecutionError):
        prepare_recovery(store, 'run', 'recovery_only')


def test_auto_approve_with_worktree_lock_held_skips_relock(recovery):
    """When worktree_lock_held=True, should not try to acquire worktree lock."""
    store, tree = recovery
    # Fixture already has 'interrupted' (TERMINAL) status

    # Create a second store instance and hold the worktree lock
    other_store = ExecutionStore(store.root)
    worktree_id = store.worktree_lock_id('run')

    with other_store.locked(worktree_id):
        # Should still succeed because worktree_lock_held=True
        result = auto_approve_resume(store, 'run', worktree_lock_held=True)
        assert result['approved'] is True

    # Without the flag, should fail with the lock held (lock contention)
    with other_store.locked(worktree_id):
        result = auto_approve_resume(store, 'run', worktree_lock_held=False)
        assert result['approved'] is False
        assert result['reason'] == 'recovery_busy'


def test_invalidate_recovery_marks_intent_and_clears_pending(recovery):
    """invalidate_recovery should mark intent as invalidated."""
    store, tree = recovery
    # Fixture already has 'interrupted' (TERMINAL) status

    # Create approval
    result = auto_approve_resume(store, 'run')
    event = result['event_id']

    # Verify it's pending
    assert pending_auto_recovery(store, 'run') == event

    # Invalidate
    assert invalidate_recovery(store, 'run', event) is True

    # Should no longer be pending
    assert pending_auto_recovery(store, 'run') is None

    # Intent should have status invalidated
    path = store.root / 'run' / 'recovery-intent.json'
    intent = json.loads(path.read_text())
    assert intent['status'] == 'invalidated'
    assert intent['approver'] == 'tick'


def test_legacy_three_field_intent_still_reads(recovery):
    """_read should accept legacy 3-field intent without approver."""
    store, tree = recovery

    from hermes_pipeline.agent_recovery import _snapshot
    event_id = 'a' * 32
    preview = _snapshot(store, 'run', 'recovery_only', event_id)
    legacy_intent = {
        'version': 1,
        'status': 'approved',
        'preview': preview
    }

    path = store.root / 'run' / 'recovery-intent.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy_intent))

    legacy_preview = validate_recovery(store, 'run', event_id)
    assert legacy_preview is not None

    state = recovery_state(store, 'run')
    assert state['approver'] == 'operator'


def test_read_rejects_non_dict_and_malformed_preview(recovery):
    """Non-dict JSON and malformed preview scalars should raise."""
    store, tree = recovery
    path = store.root / 'run' / 'recovery-intent.json'

    for payload_str in ['5', 'null', '[{}]']:
        path.write_bytes(payload_str.encode())
        with pytest.raises(ExecutionError):
            recovery_state(store, 'run')
        with pytest.raises(ExecutionError):
            pending_auto_recovery(store, 'run')
        assert auto_approve_resume(store, 'run')['reason'] == 'recovery_approval_failed'

    path.unlink()
    result = auto_approve_resume(store, 'run')
    assert result['approved'] is True

    valid_intent = json.loads(path.read_text())
    valid_intent['preview']['event_id'] = 'x' * 31
    path.write_text(json.dumps(valid_intent))

    with pytest.raises(ExecutionError):
        recovery_state(store, 'run')
    with pytest.raises(ExecutionError):
        pending_auto_recovery(store, 'run')
    assert auto_approve_resume(store, 'run')['reason'] == 'recovery_approval_failed'


def test_operator_intent_blocks_auto_approve(recovery):
    """Existing operator intent should block auto_approve_resume."""
    store, tree = recovery
    preview = prepare_recovery(store, 'run', 'recovery_only')

    result = auto_approve_resume(store, 'run')
    assert result['approved'] is False
    assert result['reason'] == 'recovery_operator_intent_pending'

    event = approve_recovery(store, 'run', preview)
    assert event is not None


def test_tick_intent_reissues_tracking(recovery):
    """Tick intents should track reissues count."""
    store, tree = recovery

    result1 = auto_approve_resume(store, 'run')
    assert result1['approved'] is True
    event1 = result1['event_id']

    invalidate_recovery(store, 'run', event1)

    result2 = auto_approve_resume(store, 'run')
    assert result2['approved'] is True
    assert result2['event_id'] != event1

    path = store.root / 'run' / 'recovery-intent.json'
    intent = json.loads(path.read_text())
    assert intent.get('reissues', 0) == 1


def test_max_reissues_limit(recovery):
    """After MAX_REISSUES, further re-approvals fail."""
    from hermes_pipeline.agent_recovery import MAX_REISSUES
    store, tree = recovery

    for i in range(MAX_REISSUES + 1):
        result = auto_approve_resume(store, 'run')
        assert result['approved'] is True, f"Failed at i={i}"
        state = recovery_state(store, 'run')
        assert state['reissues'] == i, f"Expected reissues={i}, got {state['reissues']}"
        invalidate_recovery(store, 'run', result['event_id'])

    result = auto_approve_resume(store, 'run')
    assert result['approved'] is False
    assert result['reason'] == 'recovery_state_changed'
    assert pending_auto_recovery(store, 'run') is None


def test_reissues_reset_on_generation_advance(recovery):
    """Reissues counter should reset to 0 when generation advances."""
    store, tree = recovery

    # First approval at generation 1
    result1 = auto_approve_resume(store, 'run')
    assert result1['approved'] is True
    assert result1['generation'] == 1
    state1 = recovery_state(store, 'run')
    assert state1['reissues'] == 0

    # Mutate and approve again (still generation 1, reissues increments)
    (tree / 'file1').write_bytes(b'content')
    result2 = auto_approve_resume(store, 'run')
    assert result2['approved'] is True
    assert result2['generation'] == 1
    state2 = recovery_state(store, 'run')
    assert state2['reissues'] == 1

    # Now create generation 2 by authorizing and admitting a retry
    event = result2['event_id']
    store.authorize_retry('run', expected_generation=1, event_id=event)
    store.admit('run', recovery_event=event)
    store.update_attempt('run', 2, status='interrupted', cleanup='confirmed')

    # Approve at generation 2 (should reset reissues to 0)
    result3 = auto_approve_resume(store, 'run')
    assert result3['approved'] is True
    assert result3['generation'] == 2
    state3 = recovery_state(store, 'run')
    assert state3['reissues'] == 0


def test_refusals_retract_stale_tick_approval(recovery):
    """Refusal conditions should retract stale tick approvals."""
    store, tree = recovery

    result = auto_approve_resume(store, 'run', max_generations=3)
    assert result['approved'] is True
    event1 = result['event_id']
    assert pending_auto_recovery(store, 'run', max_generations=3) == event1

    result = auto_approve_resume(store, 'run', max_generations=1)
    assert result['approved'] is False
    assert result['reason'] == 'recovery_generation_exhausted'
    assert pending_auto_recovery(store, 'run') is None

    with pytest.raises(ExecutionError):
        validate_recovery(store, 'run', event1)


def test_pending_auto_recovery_validation_gates(recovery):
    """pending_auto_recovery should validate all preconditions."""
    store, tree = recovery

    result = auto_approve_resume(store, 'run')
    assert result['approved'] is True
    event = result['event_id']

    assert pending_auto_recovery(store, 'run') == event
    assert pending_auto_recovery(store, 'run', max_generations=1) is None

    store.update_attempt('run', 1, cleanup='unconfirmed')
    assert pending_auto_recovery(store, 'run') is None


def test_operator_intent_three_key_form(recovery):
    """Operator approvals should write 3-key form without approver field."""
    store, tree = recovery

    preview = prepare_recovery(store, 'run', 'recovery_only')

    path = store.root / 'run' / 'recovery-intent.json'
    intent = json.loads(path.read_text())

    assert set(intent.keys()) == {'version', 'status', 'preview'}
    assert 'approver' not in intent

    event = approve_recovery(store, 'run', preview)
    intent_after_approve = json.loads(path.read_text())
    assert set(intent_after_approve.keys()) == {'version', 'status', 'preview'}
    assert 'approver' not in intent_after_approve
    assert intent_after_approve['status'] == 'approved'

    consume_recovery(store, 'run', event)
    intent_after_consume = json.loads(path.read_text())
    assert set(intent_after_consume.keys()) == {'version', 'status', 'preview'}
    assert 'approver' not in intent_after_consume
    assert intent_after_consume['status'] == 'consumed'


def test_invalidate_recovery_returns_bool(recovery):
    """invalidate_recovery should return bool and only invalidate tick intents."""
    store, tree = recovery

    preview = prepare_recovery(store, 'run', 'recovery_only')
    event = approve_recovery(store, 'run', preview)

    result = invalidate_recovery(store, 'run', event)
    assert result is False

    path = store.root / 'run' / 'recovery-intent.json'
    intent = json.loads(path.read_text())
    assert intent['status'] == 'approved'


def test_auto_approve_no_attempt(recovery):
    """Auto-approve with no attempts should return recovery_no_attempt."""
    store, tree = recovery

    store.register('empty', registration_id='registration', plan_identity='a'*64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(tree), branch='task', result_contract={}, timeout=30)

    result = auto_approve_resume(store, 'empty')
    assert result['approved'] is False
    assert result['reason'] == 'recovery_no_attempt'
    assert result['generation'] == 0


@pytest.mark.parametrize('status', ['timed_out', 'interrupted'])
def test_auto_approve_terminal_statuses(recovery, status):
    """Auto-approve should work for all terminal statuses."""
    store, tree = recovery

    if status == 'timed_out':
        store.register('term', registration_id='registration', plan_identity='a'*64,
                       phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                       worktree=str(tree), branch='task', result_contract={}, timeout=30)
        ProgressJournal(store, 'term').initialize()
        store.admit('term')
        store.update_attempt('term', 1, status='timed_out', cleanup='confirmed')
        execution_id = 'term'
    else:
        execution_id = 'run'

    result = auto_approve_resume(store, execution_id)
    assert result['approved'] is True


def test_pending_ignores_operator_and_advanced_generation(recovery):
    """pending_auto_recovery should ignore operator intents and advanced generations."""
    store, tree = recovery

    # Test 1: operator intent is ignored by pending_auto_recovery
    preview = prepare_recovery(store, 'run', 'recovery_only')
    approve_recovery(store, 'run', preview)
    assert pending_auto_recovery(store, 'run') is None

    # Test 2: advanced generation is ignored
    # Invalidate operator intent so we can create a tick approval
    path = store.root / 'run' / 'recovery-intent.json'
    path.unlink()

    # Create a tick approval and verify it's pending
    result = auto_approve_resume(store, 'run')
    assert result['approved'] is True
    event1 = result['event_id']
    assert pending_auto_recovery(store, 'run') == event1

    # Authorize retry and admit with the recovery event
    store.authorize_retry('run', expected_generation=1, event_id=event1)
    store.admit('run', recovery_event=event1)
    store.update_attempt('run', 2, status='interrupted', cleanup='confirmed')

    # Now pending should be None because generation advanced (event is in attempt)
    assert pending_auto_recovery(store, 'run') is None


def test_already_approved_reports_mode(recovery):
    """Already-approved recovery should report the mode."""
    store, tree = recovery

    result1 = auto_approve_resume(store, 'run', mode='recovery_only')
    assert result1['approved'] is True
    assert result1['mode'] == 'recovery_only'

    result2 = auto_approve_resume(store, 'run', mode='recovery_only')
    assert result2['approved'] is True
    assert result2['reason'] == 'recovery_already_approved'
    assert result2['mode'] == 'recovery_only'


def test_refusal_ordering_cleanup_before_generation(recovery):
    """Cleanup check should precede generation check."""
    store, tree = recovery

    store.update_attempt('run', 1, cleanup='unconfirmed')

    result = auto_approve_resume(store, 'run', max_generations=1)
    assert result['reason'] == 'recovery_cleanup_unconfirmed'


def test_corrupt_progress_json_returns_evidence_invalid(recovery):
    """Corrupt progress.json should return recovery_evidence_invalid."""
    store, tree = recovery
    execution_id = 'run'

    # Corrupt the progress.json file (keep progress-anchor.json as per task)
    (store.root / execution_id / 'progress.json').write_text('{ corrupted json ]')

    result = auto_approve_resume(store, execution_id)
    assert result['approved'] is False
    assert result['reason'] == 'recovery_evidence_invalid'
    assert result['generation'] == 1


def test_legacy_execution_without_journal_returns_evidence_legacy(recovery):
    """Execution without progress.json should return recovery_evidence_legacy."""
    store, tree = recovery
    execution_id = 'legacy'

    # Register a new execution without progress.json (legacy pattern)
    store.register(execution_id, registration_id='registration', plan_identity='c'*64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(tree), branch='task', result_contract={}, timeout=30)
    # Don't initialize ProgressJournal
    store.admit(execution_id)
    store.update_attempt(execution_id, 1, status='interrupted', cleanup='confirmed')

    result = auto_approve_resume(store, execution_id)
    assert result['approved'] is False
    assert result['reason'] == 'recovery_evidence_legacy'
    assert result['generation'] == 1


def test_recovery_worktree_busy_with_peer_execution(recovery):
    """auto_approve_resume should detect peer execution on same worktree."""
    store, tree = recovery
    # Fixture has 'run' on the tree in interrupted state

    # Register a peer on the same tree, admit it, leave it non-terminal
    peer_id = 'peer'
    store.register(peer_id, registration_id='registration', plan_identity='b'*64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(tree), branch='task', result_contract={}, timeout=30)
    ProgressJournal(store, peer_id).initialize()
    store.admit(peer_id)
    store.update_attempt(peer_id, 1, status='running', cleanup='unconfirmed')

    # Now call auto_approve_resume on 'run' - should get recovery_worktree_busy
    result = auto_approve_resume(store, 'run')
    assert result['approved'] is False
    assert result['reason'] == 'recovery_worktree_busy'
    assert result['generation'] == 1
    assert result['event_id'] is None


def test_recovery_worktree_busy_with_held_lock(recovery):
    """auto_approve_resume should detect peer even with worktree_lock_held=True."""
    store, tree = recovery
    # Fixture has 'run' on the tree in interrupted state

    # Register a peer on the same tree, admit it, leave it non-terminal
    peer_id = 'peer'
    store.register(peer_id, registration_id='registration', plan_identity='b'*64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(tree), branch='task', result_contract={}, timeout=30)
    ProgressJournal(store, peer_id).initialize()
    store.admit(peer_id)
    store.update_attempt(peer_id, 1, status='running', cleanup='unconfirmed')

    # Create a second store and hold the worktree lock
    other_store = ExecutionStore(store.root)
    worktree_id = store.worktree_lock_id('run')

    with other_store.locked(worktree_id):
        result = auto_approve_resume(store, 'run', worktree_lock_held=True)
        assert result['approved'] is False
        assert result['reason'] == 'recovery_worktree_busy'
        assert result['generation'] == 1
        assert result['event_id'] is None


def test_reissues_cap_retracts_approval(recovery):
    """Reissue cap should retract stale approval and invalidate state."""
    from hermes_pipeline.agent_recovery import MAX_REISSUES

    store, tree = recovery
    execution_id = 'run'

    # Approve initially
    result = auto_approve_resume(store, execution_id)
    assert result['approved'] is True
    assert result['reason'] == 'recovery_approved'

    # Do MAX_REISSUES mutations (which will trigger new approvals due to state change)
    for i in range(MAX_REISSUES):
        # Mutate the worktree so next approval detects state change
        (tree / f'file{i}').write_bytes(b'content')

        result = auto_approve_resume(store, execution_id)
        assert result['approved'] is True, f"Failed at cycle {i+1}"
        assert result['reason'] in {'recovery_approved', 'recovery_already_approved'}, f"Cycle {i+1}: {result['reason']}"

    # Now we've done MAX_REISSUES+1 approvals; the next one should hit the cap
    (tree / f'file{MAX_REISSUES}').write_bytes(b'content')
    result = auto_approve_resume(store, execution_id)
    assert result['approved'] is False
    assert result['reason'] == 'recovery_state_changed'
    assert result['generation'] == 1

    # Verify the intent is invalidated
    state = recovery_state(store, execution_id)
    assert state is not None
    assert state['state'] == 'invalidated'

    # Verify pending is None
    assert pending_auto_recovery(store, execution_id) is None
