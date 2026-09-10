"""Private, explicit operator recovery approvals bound to a Git work snapshot.

These controls never modify the execution checkout. Workers may reconnect to an
attempt, but cannot create or approve recovery intent.
"""

import hashlib
import json
import os
import secrets
import stat
import subprocess
from pathlib import Path

from .agent_checkpoint import ProgressJournal
from .agent_execution import TERMINAL, ExecutionError, _atomic_write, _safe_read

_LIMIT = 256 * 1024
_PREVIEW_FIELDS = {'version', 'execution_id', 'event_id', 'generation', 'mode',
                   'registration_sha256', 'head', 'branch', 'worktree',
                   'work_sha256', 'context'}


def _operator():
    if any(name in os.environ for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID')):
        raise ExecutionError('operator recovery is unavailable in worker context')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _git(tree, *arguments):
    try:
        result = subprocess.run(['git', '-C', str(tree), *arguments], check=True,
                                capture_output=True, timeout=30,
                                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutionError('recovery Git validation failed') from exc
    return result.stdout


def _snapshot(store, execution_id, mode, event_id):
    if mode not in {'resume', 'recovery_only'}:
        raise ExecutionError('invalid recovery mode')
    record = store.load(execution_id)
    if not record['attempts']:
        raise ExecutionError('recovery requires a prior attempt')
    attempt = record['attempts'][-1]
    if attempt['status'] not in TERMINAL or attempt['cleanup'] != 'confirmed':
        raise ExecutionError('recovery requires terminal outcome and confirmed cleanup')
    registration = record['registration']
    tree = Path(registration['worktree'])
    context = ProgressJournal(store, execution_id).recovery_context(attempt['generation'])
    if mode == 'resume' and context.get('legacy_evidence_absent'):
        raise ExecutionError('legacy evidence permits recovery_only validation only')
    context = {**context, 'operator_mode': mode}
    if mode == 'recovery_only':
        context['instruction'] = ('Verification and result collection only: do not repeat implementation. '
                                  + context['instruction'])
    digest = hashlib.sha256()
    # Binary diffs include staged and unstaged changes independently.
    for args in [('diff', '--binary', '--no-ext-diff', '--no-textconv'),
                 ('diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv'),
                 ('status', '--porcelain=v1', '-z', '--untracked-files=all')]:
        raw = _git(tree, *args)
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
    for name in _git(tree, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
        if not name:
            continue
        path = tree / os.fsdecode(name)
        if not path.resolve().is_relative_to(tree.resolve()):
            raise ExecutionError('untracked path escapes worktree')
        current = path
        while current != tree:
            if current.is_symlink():
                raise ExecutionError('untracked symlink blocks recovery')
            current = current.parent
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ExecutionError('untracked nonregular file blocks recovery')
                file_digest = hashlib.file_digest(stream, 'sha256').digest()
        except OSError as exc:
            raise ExecutionError('untracked recovery evidence unavailable') from exc
        digest.update(len(name).to_bytes(8, 'big'))
        digest.update(name)
        digest.update(file_digest)
    return dict(version=1, execution_id=execution_id, event_id=event_id,
                generation=attempt['generation'], mode=mode,
                registration_sha256=_digest(registration), head=context['head'],
                branch=registration['branch'], worktree=str(tree),
                work_sha256=digest.hexdigest(), context=context)


def _write(store, execution_id, intent):
    if len(json.dumps(intent).encode()) > _LIMIT:
        raise ExecutionError('recovery intent exceeds size limit')
    with store._directory_handle(execution_id) as directory:
        _atomic_write(store.root / execution_id / 'recovery-intent.json', intent,
                      directory_fd=directory)


def _read(store, execution_id):
    try:
        with store._directory_handle(execution_id) as directory:
            raw = _safe_read(store.root / execution_id / 'recovery-intent.json',
                             directory_fd=directory)
        if len(raw) > _LIMIT:
            raise ExecutionError('recovery intent exceeds size limit')
        intent = json.loads(raw)
        if (not isinstance(intent, dict) or set(intent) != {'version', 'status', 'preview'}
                or type(intent['version']) is not int or intent['version'] != 1
                or intent['status'] not in {'prepared', 'approved', 'consumed'}
                or not isinstance(intent['preview'], dict)
                or set(intent['preview']) != _PREVIEW_FIELDS
                or type(intent['preview']['version']) is not int
                or intent['preview']['version'] != 1):
            raise ExecutionError('invalid recovery intent schema')
        return intent
    except (OSError, ValueError) as exc:
        raise ExecutionError('recovery intent unavailable or invalid') from exc


def prepare_recovery(store, execution_id: str, mode: str = 'recovery_only') -> dict:
    """Persist a concrete preview for separate explicit operator approval."""
    _operator()
    with store.worktree_locked(execution_id), store.locked(execution_id):
        preview = _snapshot(store, execution_id, mode, secrets.token_hex(16))
        _write(store, execution_id, dict(version=1, status='prepared', preview=preview))
        return preview


def approve_recovery(store, execution_id: str, preview: dict) -> str:
    """Approve the exact previously prepared preview, after operator review."""
    _operator()
    with store.worktree_locked(execution_id), store.locked(execution_id):
        intent = _read(store, execution_id)
        if intent['status'] != 'prepared' or preview != intent['preview']:
            raise ExecutionError('recovery preview approval mismatch')
        if _snapshot(store, execution_id, preview['mode'], preview['event_id']) != preview:
            raise ExecutionError('recovery state changed since preview')
        intent['status'] = 'approved'
        _write(store, execution_id, intent)
        return preview['event_id']


def validate_recovery(store, execution_id: str, event_id: str) -> dict:
    """Verify approved control evidence immediately before retry admission.

    The caller must retain both execution and worktree locks through admission.
    """
    _operator()
    with store.worktree_locked(execution_id), store.locked(execution_id):
        intent = _read(store, execution_id)
        preview = intent['preview']
        if intent['status'] != 'approved' or preview['event_id'] != event_id:
            raise ExecutionError('recovery lacks an approved unused event')
        if _snapshot(store, execution_id, preview['mode'], event_id) != preview:
            raise ExecutionError('recovery state changed since approval')
        return preview


def consume_recovery(store, execution_id: str, event_id: str) -> dict:
    """Consume before authorize_retry/admit while retaining both admission locks.

    Failure between consumption and launch requires a new explicit approval.
    """
    with store.worktree_locked(execution_id), store.locked(execution_id):
        preview = validate_recovery(store, execution_id, event_id)
        _write(store, execution_id, dict(version=1, status='consumed', preview=preview))
        return preview
