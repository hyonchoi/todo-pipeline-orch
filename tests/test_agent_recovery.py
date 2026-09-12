import subprocess

import pytest

from hermes_pipeline.agent_execution import ExecutionError, ExecutionStore
from hermes_pipeline.agent_recovery import (
    approve_recovery,
    consume_recovery,
    prepare_recovery,
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
    store, _ = recovery
    with pytest.raises(ExecutionError):
        prepare_recovery(store, 'run', 'resume')
    preview = prepare_recovery(store, 'run', 'recovery_only')
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'worker')
    with pytest.raises(ExecutionError):
        approve_recovery(store, 'run', preview)


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


@pytest.mark.parametrize('tamper', ['version', 'field', 'symlink', 'oversize'])
def test_private_intent_rejects_malformed_or_redirected_records(recovery, tamper):
    import json

    store, tree = recovery
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
    else:
        intent['version' if tamper == 'version' else 'unknown'] = 99
        path.write_text(json.dumps(intent))
    with pytest.raises(ExecutionError):
        validate_recovery(store, 'run', event)


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
