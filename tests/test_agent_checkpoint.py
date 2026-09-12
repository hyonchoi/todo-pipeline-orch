import json
import subprocess

import pytest

from hermes_pipeline.agent_checkpoint import ProgressJournal
from hermes_pipeline.agent_execution import ExecutionError, ExecutionStore


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


@pytest.fixture
def journal(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    git(work, 'init', '-b', 'task')
    git(work, 'config', 'user.email', 'test@example.invalid')
    git(work, 'config', 'user.name', 'Test')
    git(work, 'commit', '--allow-empty', '-m', 'base')
    store = ExecutionStore(tmp_path / 'state')
    store.register('execution', registration_id='card', plan_identity='a' * 64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(work), branch='task', result_contract={}, timeout=30,
                   manifest={'schema_version': 1, 'tasks': [{'id': 'one'}, {'id': 'two'}]})
    progress = ProgressJournal(store, 'execution')
    progress.initialize()
    store.admit('execution')
    return progress, work


def submission(progress, commit, task='one', **extra):
    path = progress.staging_directory(1) / 'checkpoint.json'
    path.write_text(json.dumps(dict(version=1, execution_id='execution', generation=1,
                                    plan_identity='a' * 64, task_id=task, commit=commit, **extra)))
    return path


def receipts(progress, commit, task='one'):
    for kind in ('verification', 'review'):
        evidence = {'checks': [{'argv': ['uv', 'run', 'pytest'], 'exit_code': 0}]} if kind == 'verification' else {'reviewer': 'independent-reviewer', 'receipt_id': 'review-1', 'outcome': 'accepted'}
        progress.record_receipt(1, task, commit, kind=kind, evidence=evidence)


def test_agent_assertion_cannot_skip_without_control_receipts(journal):
    progress, work = journal
    git(work, 'commit', '--allow-empty', '-m', 'one')
    submission(progress, git(work, 'rev-parse', 'HEAD'))
    with pytest.raises(ExecutionError, match='receipt'):
        progress.promote(1, 'checkpoint.json')
    assert progress.recovery_context(1)['current_task'] == 'one'


def test_missing_journal_with_surviving_anchor_blocks_recovery(journal):
    progress, _ = journal
    (progress.store.root / 'execution' / 'progress.json').unlink()
    with pytest.raises(ExecutionError, match='missing modern progress'):
        progress.recovery_context(1)


def test_modern_registration_cannot_downgrade_when_both_progress_files_disappear(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    git(work, 'init', '-b', 'task')
    git(work, 'config', 'user.email', 'test@example.invalid')
    git(work, 'config', 'user.name', 'Test')
    git(work, 'commit', '--allow-empty', '-m', 'base')
    store = ExecutionStore(tmp_path / 'state')
    store.register('execution', registration_id='card', plan_identity='a' * 64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(work), branch='task', result_contract={'progress_version': 1},
                   timeout=30, manifest=None)
    progress = ProgressJournal(store, 'execution')
    progress.initialize()
    store.admit('execution')
    for name in ('progress.json', 'progress-anchor.json'):
        (store.root / 'execution' / name).unlink()
    with pytest.raises(ExecutionError, match='missing modern progress'):
        progress.recovery_context(1)


def test_accepts_ordered_commits_and_returns_verification_only(journal):
    progress, work = journal
    for task in ('one', 'two'):
        git(work, 'commit', '--allow-empty', '-m', task)
        sha = git(work, 'rev-parse', 'HEAD')
        receipts(progress, sha, task)
        submission(progress, sha, task)
        progress.promote(1, 'checkpoint.json')
    assert progress.recovery_context(1)['mode'] == 'verification_only'


@pytest.mark.parametrize('change', ['extra', 'version', 'generation', 'path', 'symlink', 'oversize'])
def test_rejects_untrusted_submissions(journal, change):
    progress, work = journal
    path = submission(progress, git(work, 'rev-parse', 'HEAD'))
    name = path.name
    if change == 'path':
        name = '../checkpoint.json'
    elif change == 'symlink':
        path.unlink()
        path.symlink_to(work / 'missing')
    elif change == 'oversize':
        path.write_text('x' * 20000)
    else:
        data = json.loads(path.read_text())
        data[change] = 99
        path.write_text(json.dumps(data))
    with pytest.raises((ExecutionError, OSError)):
        progress.promote(1, name)


def test_resume_preserves_partial_changes_and_commit_candidate(journal):
    progress, work = journal
    git(work, 'commit', '--allow-empty', '-m', 'commit before checkpoint')
    (work / 'partial').write_text('keep me')
    git(work, 'add', 'partial')
    (work / 'partial').write_text('also keep me')
    before = git(work, 'status', '--porcelain')
    context = progress.recovery_context(1)
    assert context['mode'] == 'recovery_validation'
    assert context['candidate_commits'] == [git(work, 'rev-parse', 'HEAD')]
    assert context['dirty']
    assert git(work, 'status', '--porcelain') == before


@pytest.mark.parametrize('problem', ['branch', 'index.lock', 'MERGE_HEAD', 'rewind'])
def test_resume_blocks_unsafe_git_state(journal, problem):
    progress, work = journal
    if problem == 'branch':
        git(work, 'checkout', '-b', 'other')
    elif problem == 'rewind':
        git(work, 'checkout', '--orphan', 'other')
        git(work, 'commit', '--allow-empty', '-m', 'rewrite')
        git(work, 'branch', '-M', 'task')
    else:
        (work / '.git' / problem).write_text('blocked')
    with pytest.raises(ExecutionError):
        progress.recovery_context(1)


def test_native_profile_requires_registered_resume_context():
    from pathlib import Path

    from hermes_pipeline.phases import load_phase_profile
    path = Path(__file__).parents[1] / 'hermes_pipeline/data/phase-profiles/native-sdd/phases.yaml'
    prompt = load_phase_profile(path).phases[0].prompt.format(todo_id='TODO-103', plan_path='approved.md')
    assert 'TPO_RECOVERY_CONTEXT_PATH' in prompt
    assert 'original registered worktree and branch' in prompt
    assert 'verification_only' in prompt
    assert 'Start from main and create' not in prompt


def test_out_of_order_and_forged_receipt_rejected(journal):
    progress, work = journal
    git(work, 'commit', '--allow-empty', '-m', 'two')
    sha = git(work, 'rev-parse', 'HEAD')
    submission(progress, sha, 'two')
    with pytest.raises(ExecutionError, match='order'):
        progress.promote(1, 'checkpoint.json')
    submission(progress, sha, receipts={'verification': 'passed', 'review': 'passed'})
    with pytest.raises(ExecutionError, match='schema'):
        progress.promote(1, 'checkpoint.json')


def test_interrupted_atomic_promotion_preserves_previous_journal(journal, monkeypatch):
    import hermes_pipeline.agent_execution as execution
    progress, work = journal
    git(work, 'commit', '--allow-empty', '-m', 'one')
    sha = git(work, 'rev-parse', 'HEAD')
    receipts(progress, sha)
    submission(progress, sha)
    def interrupted(*args, **kwargs):
        raise OSError('interrupted write')
    monkeypatch.setattr(execution.os, 'replace', interrupted)
    with pytest.raises(OSError):
        progress.promote(1, 'checkpoint.json')
    assert progress.recovery_context(1)['accepted'] == []


def test_checkpoint_history_and_plan_drift_rejected(journal):
    progress, _ = journal
    path = progress.store.root / progress.execution_id / 'progress.json'
    data = json.loads(path.read_text())
    data['plan_identity'] = 'b' * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ExecutionError, match='Plan drift'):
        progress.recovery_context(1)


def test_missing_legacy_journal_never_invents_completed_history(journal):
    progress, work = journal
    (progress.store.root / progress.execution_id / 'progress.json').unlink()
    (progress.store.root / progress.execution_id / 'progress-anchor.json').unlink()
    progress.store.update_attempt(progress.execution_id, 1, status='interrupted', cleanup='confirmed')
    git(work, 'commit', '--allow-empty', '-m', 'unknown historical implementation')
    with pytest.raises(ExecutionError, match='legacy'):
        progress.initialize()
    context = progress.recovery_context(1)
    assert context['mode'] == 'recovery_validation'
    assert not context['subtask_guarantee']
    assert context['accepted'] == []


def test_fresh_validation_rejects_partial_work_before_admission(journal):
    progress, work = journal
    (work / 'partial.txt').write_text('unfinished implementation')
    with pytest.raises(ExecutionError, match='clean pinned base'):
        progress.validate_fresh()
    assert (work / 'partial.txt').read_text() == 'unfinished implementation'


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_hidden_tracked_changes_block_recovery(journal, flag):
    progress, work = journal
    path = work / 'source.txt'
    path.write_text('original')
    git(work, 'add', 'source.txt')
    git(work, 'commit', '-m', 'one')
    git(work, 'update-index', flag, 'source.txt')
    path.write_text('hidden modification')
    assert git(work, 'status', '--porcelain') == ''
    with pytest.raises(ExecutionError, match='hidden index flags'):
        progress.recovery_context(1)


def test_rewritten_accepted_history_rejected(journal):
    progress, work = journal
    git(work, 'commit', '--allow-empty', '-m', 'one')
    sha = git(work, 'rev-parse', 'HEAD')
    receipts(progress, sha)
    submission(progress, sha)
    progress.promote(1, 'checkpoint.json')
    path = progress.store.root / progress.execution_id / 'progress.json'
    data = json.loads(path.read_text())
    data['accepted'][0]['commit'] = data['base']
    path.write_text(json.dumps(data))
    with pytest.raises(ExecutionError, match='rewritten'):
        progress.recovery_context(1)


def test_commit_gap_cannot_be_promoted_as_single_task(journal):
    progress, work = journal
    for _ in range(2):
        git(work, 'commit', '--allow-empty', '-m', 'unknown')
    sha = git(work, 'rev-parse', 'HEAD')
    receipts(progress, sha)
    submission(progress, sha)
    with pytest.raises(ExecutionError, match='parent'):
        progress.promote(1, 'checkpoint.json')


def test_raw_or_failing_evidence_rejected(journal):
    progress, work = journal
    sha = git(work, 'rev-parse', 'HEAD')
    for evidence in ({'raw_provider': 'secret'}, {'checks': [{'argv': ['pytest'], 'exit_code': 1}]}, {'checks': [{'argv': ['curl', 'Authorization: secret'], 'exit_code': 0}]}):
        with pytest.raises(ExecutionError):
            progress.record_receipt(1, 'one', sha, kind='verification', evidence=evidence)


def test_dropped_checkpoint_history_detected(journal):
    progress, work = journal
    git(work, 'commit', '--allow-empty', '-m', 'one')
    sha = git(work, 'rev-parse', 'HEAD')
    receipts(progress, sha)
    submission(progress, sha)
    progress.promote(1, 'checkpoint.json')
    path = progress.store.root / progress.execution_id / 'progress.json'
    data = json.loads(path.read_text())
    data['accepted'] = []
    path.write_text(json.dumps(data))
    with pytest.raises(ExecutionError, match='rewritten'):
        progress.recovery_context(1)


def test_excess_commits_block_recovery(journal):
    progress, work = journal
    for _ in range(3):
        git(work, 'commit', '--allow-empty', '-m', 'unknown')
    with pytest.raises(ExecutionError, match='unexpected HEAD'):
        progress.recovery_context(1)


def test_accepted_count_reads_progress_without_git(journal):
    """accepted_count() should read progress.json directly without invoking git."""
    progress, work = journal
    git(work, 'commit', '--allow-empty', '-m', 'one')
    sha = git(work, 'rev-parse', 'HEAD')
    receipts(progress, sha)
    submission(progress, sha)
    progress.promote(1, 'checkpoint.json')

    # Patch run_git to fail if called
    import hermes_pipeline.agent_checkpoint
    original_run_git = hermes_pipeline.agent_checkpoint.run_git
    def failing_run_git(*args, **kwargs):
        pytest.fail("run_git should not be called by accepted_count()")

    try:
        hermes_pipeline.agent_checkpoint.run_git = failing_run_git
        # This should succeed without calling run_git
        count = progress.accepted_count()
        assert count == 1
    finally:
        hermes_pipeline.agent_checkpoint.run_git = original_run_git
