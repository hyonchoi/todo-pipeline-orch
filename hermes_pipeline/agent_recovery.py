"""Private, explicit operator recovery approvals bound to a Git work snapshot.

Approvals are created by an operator (CLI, refused under worker environment)
or by the pipeline tick (auto_approve_resume). Verification and consumption
run inside the supervisor and do not consult the environment. Records are
not isolated from same-user clients (see docs/howto-agent-supervisor.md
storage section). The generation cap bounds any retry loop.
"""

import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path

from .agent_checkpoint import ProgressJournal
from .agent_execution import (
    TERMINAL,
    ExecutionError,
    LockUnconfirmed,
    _atomic_write,
    _safe_read,
    execution_logger,
)
from .agent_git import run_git


class RecoveryStateChanged(ExecutionError):
    """The worktree or record no longer matches the approved recovery preview."""


_LIMIT = 256 * 1024
_PREVIEW_FIELDS = {'version', 'execution_id', 'event_id', 'generation', 'mode',
                   'registration_sha256', 'head', 'branch', 'worktree',
                   'work_sha256', 'context'}
_EVENT_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')

MAX_GENERATIONS = 3
MAX_REISSUES = 3
RECOVERY_REASONS = frozenset({
    "recovery_approved",
    "recovery_already_approved",
    "recovery_no_attempt",
    "recovery_outcome_ineligible",
    "recovery_cleanup_unconfirmed",
    "recovery_generation_exhausted",
    "recovery_evidence_legacy",
    "recovery_evidence_invalid",
    "recovery_worktree_unsafe",
    "recovery_worktree_busy",
    "recovery_state_changed",
    "recovery_approval_failed",
    "recovery_busy",
    "recovery_operator_intent_pending",
})
_APPROVERS = frozenset({"operator", "tick"})


def _operator():
    if any(name in os.environ for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID')):
        raise ExecutionError('operator recovery is unavailable in worker context')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _git(tree, *arguments):
    try:
        result = run_git(tree, arguments, check=True,
                                capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutionError('recovery Git validation failed') from exc
    return result.stdout


def _verdict(reason, generation, *, event_id=None, mode=None):
    """Return a standardized recovery verdict dict."""
    if reason not in RECOVERY_REASONS:
        raise ValueError(f"reason {reason!r} not in RECOVERY_REASONS")
    return {
        'approved': reason in {"recovery_approved", "recovery_already_approved"},
        'event_id': event_id,
        'reason': reason,
        'generation': generation,
        'mode': mode,
    }


def _operator_form(intent):
    """Extract the 3-key operator form: {version, status, preview}."""
    return {
        'version': intent['version'],
        'status': intent['status'],
        'preview': intent['preview'],
    }


def _read_optional(store, execution_id):
    """Read intent, returning None only if missing, raising if malformed."""
    try:
        with store._directory_handle(execution_id) as directory:
            try:
                raw = _safe_read(store.root / execution_id / 'recovery-intent.json',
                                 directory_fd=directory)
            except FileNotFoundError:
                return None
        if len(raw) > _LIMIT:
            raise ExecutionError('recovery intent exceeds size limit')

        try:
            intent = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ExecutionError('invalid recovery intent schema') from exc

        if not isinstance(intent, dict):
            raise ExecutionError('invalid recovery intent schema')

        valid_keys_new = {'version', 'status', 'approver', 'preview'}
        valid_keys_with_reissues = {'version', 'status', 'approver', 'preview', 'reissues'}
        valid_keys_legacy = {'version', 'status', 'preview'}
        actual_keys = set(intent)

        is_legacy = actual_keys == valid_keys_legacy
        is_new = actual_keys == valid_keys_new
        is_with_reissues = actual_keys == valid_keys_with_reissues

        if not (is_legacy or is_new or is_with_reissues):
            raise ExecutionError('invalid recovery intent schema')

        if type(intent['version']) is not int or intent['version'] != 1:
            raise ExecutionError('invalid recovery intent schema')

        valid_statuses = {'prepared', 'approved', 'consumed', 'invalidated'}
        if intent['status'] not in valid_statuses:
            raise ExecutionError('invalid recovery intent schema')

        if not isinstance(intent['preview'], dict) or set(intent['preview']) != _PREVIEW_FIELDS:
            raise ExecutionError('invalid recovery intent schema')

        if (type(intent['preview']['version']) is not int
                or intent['preview']['version'] != 1):
            raise ExecutionError('invalid recovery intent schema')

        preview = intent['preview']
        if not _EVENT_ID_PATTERN.match(preview['event_id']):
            raise ExecutionError('invalid recovery intent schema')
        if type(preview['generation']) is not int or preview['generation'] < 1:
            raise ExecutionError('invalid recovery intent schema')
        if preview['mode'] not in {'resume', 'recovery_only'}:
            raise ExecutionError('invalid recovery intent schema')
        if preview['execution_id'] != execution_id:
            raise ExecutionError('invalid recovery intent schema')

        if is_new or is_with_reissues:
            if intent['approver'] not in _APPROVERS:
                raise ExecutionError('invalid recovery intent schema')
            intent.setdefault('reissues', 0)
            if type(intent['reissues']) is not int or intent['reissues'] < 0:
                raise ExecutionError('invalid recovery intent schema')
        else:
            intent['approver'] = 'operator'
            intent['reissues'] = 0

        return intent
    except ExecutionError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise ExecutionError('recovery intent unavailable or invalid') from exc


def _snapshot(store, execution_id, mode, event_id, context=None):
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

    if context is None:
        context = ProgressJournal(store, execution_id).recovery_context(attempt['generation'])

    if mode == 'resume' and context.get('legacy_evidence_absent'):
        raise ExecutionError('legacy evidence permits recovery_only validation only')
    context = {**context, 'operator_mode': mode}
    if mode == 'recovery_only':
        context['instruction'] = ('Verification and result collection only: do not repeat implementation. '
                                  + context['instruction'])
    digest = hashlib.sha256()
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
        intent = _read_optional(store, execution_id)
        if intent is None or intent['status'] != 'prepared' or preview != intent['preview']:
            raise ExecutionError('recovery preview approval mismatch')
        if _snapshot(store, execution_id, preview['mode'], preview['event_id']) != preview:
            raise ExecutionError('recovery state changed since preview')
        intent['status'] = 'approved'
        _write(store, execution_id, _operator_form(intent))
        return preview['event_id']


def validate_recovery(store, execution_id: str, event_id: str) -> dict:
    """Verify approved control evidence immediately before retry admission.

    Verification and consumption run inside the supervisor on behalf of a
    recorded approval; the worker-spawned daemon inherits HERMES_KANBAN_TASK
    and cannot approve, so _operator() is not called here.
    The caller must retain both execution and worktree locks through admission.
    """
    with store.worktree_locked(execution_id), store.locked(execution_id):
        intent = _read_optional(store, execution_id)
        if intent is None or intent['status'] != 'approved' or intent['preview']['event_id'] != event_id:
            raise ExecutionError('recovery lacks an approved unused event')
        preview = intent['preview']
        if _snapshot(store, execution_id, preview['mode'], event_id) != preview:
            raise RecoveryStateChanged('recovery state changed since approval')
        return preview


def consume_recovery(store, execution_id: str, event_id: str) -> dict:
    """Consume before authorize_retry/admit while retaining both admission locks.

    Failure between consumption and launch requires a new explicit approval.
    """
    with store.worktree_locked(execution_id), store.locked(execution_id):
        intent = _read_optional(store, execution_id)
        preview = validate_recovery(store, execution_id, event_id)
        if intent['approver'] == 'operator':
            _write(store, execution_id, _operator_form({
                'version': 1, 'status': 'consumed', 'preview': preview
            }))
        else:
            _write(store, execution_id, dict(version=1, status='consumed', approver=intent['approver'],
                                            preview=preview, reissues=intent['reissues']))
        return preview


def _retract_stale_tick_approval(store, execution_id, generation):
    """Retract stale tick approval if it exists for this generation."""
    try:
        intent = _read_optional(store, execution_id)
        if (intent and intent['status'] == 'approved' and
            intent.get('approver') == 'tick' and
            intent['preview']['generation'] == generation):
            intent['status'] = 'invalidated'
            _write(store, execution_id, intent)
    except ExecutionError:
        pass


def auto_approve_resume(store, execution_id, *, max_generations=MAX_GENERATIONS,
                        mode='resume', worktree_lock_held=False) -> dict:
    """Automatically approve recovery on behalf of tick-based recovery dispatcher.

    Returns a dict with:
    - approved: bool
    - event_id: str | None (set when approved)
    - reason: str (one of RECOVERY_REASONS)
    - generation: int
    - mode: str | None

    Never raises for policy outcomes. Takes store.worktree_locked(id) unless
    worktree_lock_held is True, in which case takes store.locked(id).
    """
    logger = execution_logger(store, execution_id)
    generation = 0

    with contextlib.ExitStack() as stack:
        try:
            if not worktree_lock_held:
                lock_id = store.worktree_lock_id(execution_id)
                stack.enter_context(store.locked(lock_id))
            stack.enter_context(store.locked(execution_id))

            record = store.load(execution_id)

            if not record['attempts']:
                reason = 'recovery_no_attempt'
                logger.info("auto_approve_resume %s generation=0", reason)
                return _verdict(reason, 0)

            attempt = record['attempts'][-1]
            generation = attempt['generation']

            try:
                store.assert_worktree_peers_resolved(execution_id)
            except ExecutionError:
                reason = 'recovery_worktree_busy'
                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                return _verdict(reason, generation)

            if attempt['status'] not in {'timed_out', 'interrupted'}:
                reason = 'recovery_outcome_ineligible'
                _retract_stale_tick_approval(store, execution_id, generation)
                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                return _verdict(reason, generation)

            if attempt['cleanup'] != 'confirmed':
                reason = 'recovery_cleanup_unconfirmed'
                _retract_stale_tick_approval(store, execution_id, generation)
                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                return _verdict(reason, generation)

            if generation >= max_generations:
                reason = 'recovery_generation_exhausted'
                _retract_stale_tick_approval(store, execution_id, generation)
                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                return _verdict(reason, generation)

            existing_intent = _read_optional(store, execution_id)
            if existing_intent is not None:
                if existing_intent.get('approver') == 'operator' and existing_intent['status'] in {'prepared', 'approved'}:
                    reason = 'recovery_operator_intent_pending'
                    logger.info("auto_approve_resume %s generation=%s", reason, generation)
                    return _verdict(reason, generation)

                if (existing_intent['status'] == 'approved' and
                    existing_intent.get('approver') == 'tick' and
                    existing_intent['preview']['generation'] == generation):

                    event_id = existing_intent['preview']['event_id']
                    event_in_attempts = any(a.get('recovery_event') == event_id
                                           for a in record['attempts'])

                    if not event_in_attempts:
                        try:
                            new_snapshot = _snapshot(store, execution_id, mode, event_id)
                            if new_snapshot == existing_intent['preview']:
                                reason = 'recovery_already_approved'
                                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                                return _verdict(reason, generation, event_id=event_id, mode=mode)
                        except ExecutionError:
                            pass

            try:
                ProgressJournal(store, execution_id)._load()
            except FileNotFoundError:
                pass
            except ExecutionError:
                reason = 'recovery_evidence_invalid'
                _retract_stale_tick_approval(store, execution_id, generation)
                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                return _verdict(reason, generation)

            try:
                context = ProgressJournal(store, execution_id).recovery_context(generation)
            except ExecutionError:
                reason = 'recovery_worktree_unsafe'
                _retract_stale_tick_approval(store, execution_id, generation)
                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                return _verdict(reason, generation)

            if context.get('legacy_evidence_absent'):
                reason = 'recovery_evidence_legacy'
                _retract_stale_tick_approval(store, execution_id, generation)
                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                return _verdict(reason, generation)

            try:
                event_id = secrets.token_hex(16)
                preview = _snapshot(store, execution_id, mode, event_id, context=context)
            except ExecutionError:
                reason = 'recovery_worktree_unsafe'
                _retract_stale_tick_approval(store, execution_id, generation)
                logger.info("auto_approve_resume %s generation=%s", reason, generation)
                return _verdict(reason, generation)

            reissues = 0
            if existing_intent and existing_intent.get('approver') == 'tick':
                if existing_intent['preview']['generation'] == generation:
                    old_reissues = existing_intent.get('reissues', 0)
                    reissues = old_reissues + 1
                    if reissues > MAX_REISSUES:
                        _retract_stale_tick_approval(store, execution_id, generation)
                        reason = 'recovery_state_changed'
                        logger.info("auto_approve_resume %s generation=%s", reason, generation)
                        return _verdict(reason, generation)

            intent = {
                'version': 1,
                'status': 'approved',
                'approver': 'tick',
                'preview': preview,
                'reissues': reissues,
            }
            _write(store, execution_id, intent)
            reason = 'recovery_approved'
            logger.info("auto_approve_resume %s generation=%s", reason, generation)
            return _verdict(reason, generation, event_id=event_id, mode=mode)

        except (LockUnconfirmed, ExecutionError, OSError) as exc:
            if isinstance(exc, LockUnconfirmed) and isinstance(exc.__cause__, OSError):
                if exc.__cause__.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    reason = 'recovery_busy'
                    logger.info("auto_approve_resume %s generation=%s", reason, generation)
                    return _verdict(reason, generation)
            reason = 'recovery_approval_failed'
            logger.info("auto_approve_resume %s generation=%s", reason, generation)
            return _verdict(reason, generation)


def pending_auto_recovery(store, execution_id, *, max_generations=MAX_GENERATIONS) -> str | None:
    """Return event_id if an approved auto-recovery is pending, None otherwise.

    Pending means: intent exists with status 'approved', approver 'tick',
    preview generation matches last attempt generation, generation < max_generations,
    event_id not yet consumed (not in any attempt's recovery_event), last attempt
    is terminal with confirmed cleanup.

    A missing intent yields None; a malformed intent raises ExecutionError.
    """
    with store.locked(execution_id):
        intent = _read_optional(store, execution_id)

        if intent is None or intent['status'] != 'approved' or intent.get('approver') != 'tick':
            return None

        record = store.load(execution_id)
        if not record['attempts']:
            return None

        last_attempt = record['attempts'][-1]

        if last_attempt['status'] not in {'timed_out', 'interrupted'}:
            return None

        if last_attempt['cleanup'] != 'confirmed':
            return None

        generation = last_attempt['generation']
        if generation >= max_generations:
            return None

        if intent['preview']['generation'] != generation:
            return None

        event_id = intent['preview']['event_id']
        if any(a.get('recovery_event') == event_id for a in record['attempts']):
            return None

        return event_id


def invalidate_recovery(store, execution_id, event_id) -> bool:
    """Mark an approved auto-recovery as invalidated.

    If the intent has status 'approved', approver 'tick', and the event_id matches,
    rewrites with status 'invalidated', preserving approver and preview.
    Returns True if changed, False otherwise.
    """
    with store.locked(execution_id):
        intent = _read_optional(store, execution_id)

        if (intent is not None and intent['status'] == 'approved' and
            intent.get('approver') == 'tick' and intent['preview']['event_id'] == event_id):
            intent['status'] = 'invalidated'
            _write(store, execution_id, intent)
            logger = execution_logger(store, execution_id)
            logger.info("invalidate_recovery %s", event_id)
            return True

        return False


def recovery_state(store, execution_id) -> dict | None:
    """Return recovery state info if intent exists, None otherwise.

    Returns: {"state": status, "approver": ..., "generation": ..., "reissues": ...}

    A missing intent yields None; a malformed intent raises ExecutionError.
    This is a lockless operation; a daemon may hold the lock simultaneously.
    """
    intent = _read_optional(store, execution_id)
    if intent is None:
        return None
    return {
        'state': intent['status'],
        'approver': intent.get('approver', 'operator'),
        'generation': intent['preview']['generation'],
        'reissues': intent.get('reissues', 0),
    }
