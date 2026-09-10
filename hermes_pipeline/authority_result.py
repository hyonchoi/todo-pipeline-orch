"""Bind downstream worker claims to durable supervisor acceptance.

Historical consumers deliberately validate immutable commit topology instead of
requiring the implementation's head to remain the current review/finish head.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .agent_execution import ExecutionError, ExecutionStore, LockUnconfirmed, _safe_read
from .result_contract import (
    MAX_METADATA_BYTES,
    ResultContractError,
    parse_worker_result,
    verify_optional_single_commit,
    verify_worker_git_topology,
)

_MARKER = 'supervisor-required.json'
_MARKER_CONTENT = {'version': 1, 'authority': 'agent-executions'}


class RunAuthorityBusy(RuntimeError):
    """A live owner holds the worktree; polling should wait without accepting."""


def mark_supervised_run(state_dir: Path, tick_id: str) -> None:
    """Remember supervisor enrollment of an older registration before dispatch."""
    from .run_registration import _open_run_directory, _write_durable_at

    _, directory = _open_run_directory(state_dir, tick_id)
    try:
        _write_durable_at(directory, _MARKER, json.dumps(_MARKER_CONTENT).encode())
    finally:
        os.close(directory)



def _supervisor_required(registration, state_dir: Path, tick_id: str, phases) -> bool:
    from ._agent_supervisor import execution_id

    required = getattr(registration, 'supervised_execution', False)
    marker = state_dir / 'runs' / tick_id / _MARKER
    try:
        marker_payload = json.loads(_safe_read(marker))
    except FileNotFoundError:
        pass
    else:
        if (not isinstance(marker_payload, dict) or type(marker_payload.get('version')) is not int
                or marker_payload != _MARKER_CONTENT):
            raise ExecutionError('invalid supervisor enrollment')
        required = True
    root = state_dir / 'agent-executions'
    # A damaged enrolled execution is not proof of a legacy run.
    return required or any(
        (root / execution_id(tick_id, phase)).exists()
        or (root / execution_id(tick_id, phase)).is_symlink() for phase in phases
    )


@contextmanager
def locked_run_authority(*, registration, state_dir: Path, tick_id: str):
    """Serialize prerequisite reads and transitions with supervisor admission.

    Use precisely the worktree lock namespace held by ExecutionStore.admit,
    recovery approval, and supervise, before taking any execution lock. Legacy
    un-enrolled runs retain their original behavior without creating locks.
    """
    from .phases import IMPLEMENTATION_KEY

    try:
        if not _supervisor_required(registration, state_dir, tick_id,
                                    (IMPLEMENTATION_KEY, 'review:0', 'finish')):
            yield
            return
        if state_dir.resolve() != (registration.repository / '.hermes').resolve():
            raise ExecutionError('registration_root_mismatch')
        store = ExecutionStore(state_dir / 'agent-executions')
        lock_id = 'worktree-' + hashlib.sha256(os.fsencode(registration.worktree.resolve())).hexdigest()
        with store.locked(lock_id):
            yield
    except LockUnconfirmed as exc:
        cause = exc.__cause__
        if isinstance(cause, OSError) and cause.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise RunAuthorityBusy from exc
        raise ResultContractError('supervisor_result_unconfirmed') from exc
    except (ExecutionError, OSError, ValueError, KeyError, TypeError) as exc:
        raise ResultContractError('supervisor_result_unconfirmed') from exc


def require_accepted_review(*, registration, state_dir: Path, tick_id: str,
                            accepted_head: str) -> None:
    """Re-prove current predecessor authority before a finish transition.

    The caller holds locked_run_authority through this check and delivery.
    An accepted-head marker cannot authorize a failed or newer upstream retry.
    """
    from ._agent_supervisor import execution_id
    from .phases import IMPLEMENTATION_KEY

    try:
        if not _supervisor_required(registration, state_dir, tick_id,
                                    (IMPLEMENTATION_KEY, 'review:0')):
            return
        store = ExecutionStore(state_dir / 'agent-executions')
        parent = registration.base_sha
        for phase in (IMPLEMENTATION_KEY, 'review:0'):
            identity = execution_id(tick_id, phase)
            record = store.load(identity)
            attempt = record['attempts'][-1]
            contract = record['registration']['result_contract']
            with store._directory_handle(identity) as directory:
                raw = json.loads(_safe_read(Path(f"result-{attempt['generation']}.json"), directory_fd=directory))
            result = parse_worker_result(
                {'runs': [{'status': 'completed', 'metadata': {'tpo_result': raw}}]},
                tick_id=tick_id, todo_id=registration.todo_id, step_key=phase,
                acceptance_criteria=tuple(contract['acceptance']),
                allow_no_changes=not bool(contract['expected_commits']),
            )
            with require_authorized_result(registration=registration, state_dir=state_dir,
                                           tick_id=tick_id, step_key=phase, result=result):
                if result.git.expected_parent_sha != parent:
                    raise ExecutionError('supervisor prerequisite chain mismatch')
                parent = result.git.resulting_head_sha
        if parent != accepted_head:
            raise ExecutionError('supervisor accepted head mismatch')
    except (ExecutionError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ResultContractError('supervisor_result_unconfirmed') from exc


@contextmanager
def require_authorized_result(*, registration, state_dir: Path, tick_id: str,
                              step_key: str, result) -> None:
    """Fail closed for modern runs; retain explicitly old un-enrolled runs."""
    from ._agent_supervisor import execution_id, validate_registration
    from .agent_checkpoint import ProgressJournal

    identity = execution_id(tick_id, step_key)
    root = state_dir / 'agent-executions'
    try:
        required = _supervisor_required(registration, state_dir, tick_id, (step_key,))
        if not required:
            yield
            return
        if state_dir.resolve() != (registration.repository / '.hermes').resolve():
            raise ExecutionError('registration_root_mismatch')
        store = ExecutionStore(root)
        # Loading first prevents a missing record from creating an empty lock
        # directory. Hold ownership through the generation/result/receipt read.
        store.load(identity)
        with store.locked(identity):
            validate_registration(store, identity)
            record = store.load(identity)
            pinned = record['registration']
            contract = pinned['result_contract']
            if (pinned['registration_id'] != tick_id or pinned['phase'] != step_key
                    or pinned['plan_identity'] != registration.plan_hash
                    or pinned['worktree'] != str(registration.worktree)
                    or contract['todo_id'] != registration.todo_id
                    or contract['result_kind'] != 'worker'):
                raise ExecutionError('supervisor identity mismatch')
            attempt = record['attempts'][-1] if record['attempts'] else None
            if (attempt is None or attempt['status'] != 'exited'
                    or attempt['exit_code'] != 0 or attempt['exit_signal'] is not None
                    or attempt['cleanup'] != 'confirmed'):
                raise ExecutionError('supervisor completion not authorized')
            generation = attempt['generation']
            with store._directory_handle(identity) as directory:
                encoded = _safe_read(Path(f'result-{generation}.json'), directory_fd=directory)
            if len(encoded) > MAX_METADATA_BYTES:
                raise ExecutionError('promoted result size limit')
            raw = json.loads(encoded)
            promoted = parse_worker_result(
                {'runs': [{'status': 'completed', 'metadata': {'tpo_result': raw}}]},
                tick_id=tick_id, todo_id=registration.todo_id, step_key=step_key,
                acceptance_criteria=tuple(contract['acceptance']),
                allow_no_changes=not bool(contract['expected_commits']),
            )
            if promoted != result:
                raise ExecutionError('supervisor result mismatch')
            journal = ProgressJournal(store, identity)._load()
            if pinned['manifest'] is not None:
                accepted = journal['accepted']
                if (len(accepted) != len(pinned['manifest']['tasks'])
                        or not accepted or accepted[-1]['commit'] != result.git.resulting_head_sha
                        or any(entry['generation'] > generation for entry in accepted)):
                    raise ExecutionError('checkpoint evidence incomplete')
            if contract['expected_commits']:
                verify_worker_git_topology(
                    registration.worktree, promoted.git,
                    expected_parent_sha=contract['base_sha'],
                    expected_commits=contract['expected_commits'],
                )
            else:
                verify_optional_single_commit(
                    registration.worktree, promoted.git,
                    expected_parent_sha=contract['base_sha'], require_current=False,
                )
            yield
    except (ExecutionError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ResultContractError('supervisor_result_unconfirmed') from exc
