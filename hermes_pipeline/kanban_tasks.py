"""Kanban task registration for kanban-as-scheduler (TODO-10).

Uses raw `hermes kanban` CLI directly — not through HermesKanbanAdapter.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import PromptClient
from .outcomes import (
    OUTCOME_ALL_COMPLETE,
    OUTCOME_PHASE_COMPLETE,
    OUTCOME_PICKED_NONE,
)
from .phases import CLIENT_VOCABULARY, _render_phase_prompt, load_phases
from .state import _atomic_write_text

# Sentinel written after successful registration to record expected phases.
_EXPECTED_PHASES_FILE_SUFFIX = ".expected-phases.json"
_PENDING_TASK_CREATE_FILE = "pending-task-create.json"
_REGISTRATION_BARRIER_PHASE_KEY = "__registration_barrier__"
_REGISTRATION_BARRIER_INFRASTRUCTURE = "registration_barrier"

log = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"done", "failed", "archived"})

# A "blocked" kanban task is a GATE, not an error: it deliberately holds the
# project in-flight (blocked ∉ COMPLETION_STATUSES) until a human approves.
BLOCKED = "blocked"

# Statuses that count as "complete" for the purpose of determining whether
# a prior tick's work is done. Archived phases (from mid-registration
# cleanup) are excluded — they indicate the tick didn't finish cleanly.
COMPLETION_STATUSES = frozenset({"done", "failed"})

# Timeouts for subprocess calls
KANBAN_QUERY_TIMEOUT = 60       # kanban create (task registration)
HERMES_COMMAND_TIMEOUT = 10     # kanban list, archive (utility commands)
ERROR_MSG_MAX_LENGTH = 200      # max chars of stderr in error messages


def _build_json_header(
    *,
    tick_id: str,
    phase_key: str,
    todo_id: str,
    project_slug: str,
) -> str:
    """Build the JSON header line for a kanban task body."""
    return json.dumps(
        {
            "tick_id": tick_id,
            "phase_key": phase_key,
            "todo_id": todo_id,
            "project_slug": project_slug,
        },
        sort_keys=True,
    )


@dataclass(frozen=True)
class PreparedPhaseTask:
    phase_key: str
    name: str
    body: str
    turns: int
    gate: bool
    timeout: int = 1800


@dataclass(frozen=True)
class PendingTaskCreate:
    """A create whose remote task ID was not visible during recovery."""

    tenant: str
    tick_id: str
    phase_key: str
    known_task_ids: tuple[str, ...]


@dataclass(frozen=True)
class PendingTaskCleanup:
    """Task IDs whose child-first archive cleanup must be retried."""

    tenant: str
    tick_id: str
    task_ids: tuple[str, ...]


@dataclass(frozen=True)
class PendingBarrierCommit:
    """A durable phase chain whose registration barrier still needs commit."""

    tenant: str
    tick_id: str
    barrier_task_id: str
    cleanup_task_ids: tuple[str, ...]


def _external_client_delegation_block(
    prompt_client: PromptClient,
    timeout: int,
) -> str:
    """Return the dispatcher contract prepended to executable phase tasks."""
    if prompt_client == "codex":
        command = "codex exec --sandbox danger-full-access"
    elif prompt_client == "claude":
        command = "claude -p --permission-mode bypassPermissions"
    else:
        raise ValueError(
            f"prompt_client must be one of ('claude', 'codex'), got {prompt_client!r}"
        )
    agent_product = CLIENT_VOCABULARY[prompt_client]["agent_product"]
    return (
        "External client delegation:\n"
        "You are the Hermes dispatcher, not the implementation agent.\n"
        "Use the Hermes `ai-coding-agents` skill to invoke the selected "
        f"external client ({agent_product}). Build the external-agent prompt "
        "from the delimited block below and pass only that prompt to the "
        "external client.\n"
        f"Required external command: `{command}`\n"
        f"External agent timeout: {timeout} seconds.\n"
        "Launch the external command with Hermes tracked background execution, "
        "then monitor the background process until it exits or this deadline "
        "expires. Do not use a foreground terminal call because Hermes may "
        "replace this phase timeout with its shorter foreground cap.\n"
        "Do not implement this phase directly with Hermes tools.\n"
        "If the external client is unavailable, exits non-zero, or exceeds "
        "the deadline, block or fail this task with the exact reason. You "
        "must not inspect partial changes, and must not implement or commit "
        "the phase yourself.\n"
        "When completing the task, include result metadata with "
        "`external_agent_command`, `external_agent_timeout_seconds`, "
        "`external_agent_exit_code`, and any external session identifier.\n\n"
    )


def _external_agent_prompt_block(
    rendered_prompt: str, *, prompt_client: PromptClient
) -> str:
    """Wrap rendered phase work as the prompt Hermes should pass onward."""
    return (
        "BEGIN EXTERNAL AGENT PROMPT\n"
        f"{rendered_prompt.rstrip()}\n"
        "END EXTERNAL AGENT PROMPT\n"
    )


def _pending_task_create_marker(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".hermes" / "outcomes" / _PENDING_TASK_CREATE_FILE


def _pending_task_create_payload(pending: PendingTaskCreate) -> dict[str, object]:
    """Validate and serialize a pending-create marker's durable fields."""
    if not all(
        isinstance(value, str) and value
        for value in (pending.tenant, pending.tick_id, pending.phase_key)
    ):
        raise ValueError("pending task-create fields must be nonempty strings")
    if not all(
        isinstance(task_id, str)
        and re.fullmatch(r"t_[0-9a-f]{8}", task_id) is not None
        for task_id in pending.known_task_ids
    ):
        raise ValueError("pending task-create known task IDs must be valid Hermes IDs")
    return {
        "tenant": pending.tenant,
        "tick_id": pending.tick_id,
        "phase_key": pending.phase_key,
        "known_task_ids": list(pending.known_task_ids),
    }


def _persist_pending_task_create(
    project_dir: str | Path, pending: PendingTaskCreate
) -> None:
    """Atomically persist an uncertain task create for a later retry."""
    marker = _pending_task_create_marker(project_dir)
    payload = json.dumps(_pending_task_create_payload(pending), sort_keys=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(marker, payload)


def _pending_task_cleanup_payload(pending: PendingTaskCleanup) -> dict[str, object]:
    """Validate and serialize an incomplete cleanup for a later retry."""
    if not all(
        isinstance(value, str) and value for value in (pending.tenant, pending.tick_id)
    ):
        raise ValueError("pending task-cleanup fields must be nonempty strings")
    if not pending.task_ids or not all(
        isinstance(task_id, str)
        and re.fullmatch(r"t_[0-9a-f]{8}", task_id) is not None
        for task_id in pending.task_ids
    ):
        raise ValueError("pending task-cleanup IDs must be valid Hermes IDs")
    return {
        "tenant": pending.tenant,
        "tick_id": pending.tick_id,
        "cleanup_task_ids": list(pending.task_ids),
    }


def _persist_pending_task_cleanup(
    project_dir: str | Path, pending: PendingTaskCleanup
) -> None:
    """Atomically persist known task IDs whose cleanup was not confirmed."""
    marker = _pending_task_create_marker(project_dir)
    payload = json.dumps(_pending_task_cleanup_payload(pending), sort_keys=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(marker, payload)


def _pending_barrier_commit_payload(
    pending: PendingBarrierCommit,
) -> dict[str, object]:
    """Validate and serialize a registration commit awaiting confirmation."""
    if not all(
        isinstance(value, str) and value for value in (pending.tenant, pending.tick_id)
    ):
        raise ValueError("pending barrier-commit fields must be nonempty strings")
    task_ids = (pending.barrier_task_id, *pending.cleanup_task_ids)
    if not pending.cleanup_task_ids or not all(
        isinstance(task_id, str)
        and re.fullmatch(r"t_[0-9a-f]{8}", task_id) is not None
        for task_id in task_ids
    ):
        raise ValueError("pending barrier-commit IDs must be valid Hermes IDs")
    return {
        "tenant": pending.tenant,
        "tick_id": pending.tick_id,
        "barrier_task_id": pending.barrier_task_id,
        "cleanup_task_ids": list(pending.cleanup_task_ids),
    }


def _persist_pending_barrier_commit(
    project_dir: str | Path, pending: PendingBarrierCommit
) -> None:
    """Persist the commit intent before completing the remote barrier."""
    marker = _pending_task_create_marker(project_dir)
    payload = json.dumps(_pending_barrier_commit_payload(pending), sort_keys=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(marker, payload)


def _load_pending_task_state(
    project_dir: str | Path,
) -> PendingTaskCreate | PendingTaskCleanup | PendingBarrierCommit | None:
    """Load valid pending create or cleanup state, failing closed if malformed."""
    marker = _pending_task_create_marker(project_dir)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if set(payload) == {
        "tenant",
        "tick_id",
        "barrier_task_id",
        "cleanup_task_ids",
    }:
        tenant = payload["tenant"]
        tick_id = payload["tick_id"]
        barrier_task_id = payload["barrier_task_id"]
        task_ids = payload["cleanup_task_ids"]
        if not all(
            isinstance(value, str) and value
            for value in (tenant, tick_id, barrier_task_id)
        ) or not isinstance(task_ids, list):
            return None
        if not task_ids or not all(
            isinstance(task_id, str)
            and re.fullmatch(r"t_[0-9a-f]{8}", task_id) is not None
            for task_id in (barrier_task_id, *task_ids)
        ):
            return None
        return PendingBarrierCommit(
            tenant, tick_id, barrier_task_id, tuple(task_ids)
        )
    if set(payload) == {"tenant", "tick_id", "cleanup_task_ids"}:
        tenant = payload["tenant"]
        tick_id = payload["tick_id"]
        task_ids = payload["cleanup_task_ids"]
        if not all(
            isinstance(value, str) and value for value in (tenant, tick_id)
        ) or not isinstance(task_ids, list):
            return None
        if not task_ids or not all(
            isinstance(task_id, str)
            and re.fullmatch(r"t_[0-9a-f]{8}", task_id) is not None
            for task_id in task_ids
        ):
            return None
        return PendingTaskCleanup(tenant, tick_id, tuple(task_ids))
    if set(payload) != {
        "tenant",
        "tick_id",
        "phase_key",
        "known_task_ids",
    }:
        return None
    tenant = payload["tenant"]
    tick_id = payload["tick_id"]
    phase_key = payload["phase_key"]
    known_task_ids = payload["known_task_ids"]
    if not all(
        isinstance(value, str) and value for value in (tenant, tick_id, phase_key)
    ) or not isinstance(known_task_ids, list):
        return None
    if not all(
        isinstance(task_id, str)
        and re.fullmatch(r"t_[0-9a-f]{8}", task_id) is not None
        for task_id in known_task_ids
    ):
        return None
    return PendingTaskCreate(tenant, tick_id, phase_key, tuple(known_task_ids))


def _load_pending_task_create(project_dir: str | Path) -> PendingTaskCreate | None:
    """Load pending-create state, excluding cleanup-only markers."""
    pending = _load_pending_task_state(project_dir)
    return pending if isinstance(pending, PendingTaskCreate) else None


def _clear_pending_task_state(
    project_dir: str | Path,
    pending: PendingTaskCreate | PendingTaskCleanup | PendingBarrierCommit,
) -> bool:
    """Remove a pending marker only when it still describes this state."""
    if _load_pending_task_state(project_dir) != pending:
        return False
    marker = _pending_task_create_marker(project_dir)
    try:
        marker.unlink()
    except OSError:
        log.warning("failed to remove pending task-create marker: %s", marker)
        return False
    return True


def _clear_pending_task_create(
    project_dir: str | Path, pending: PendingTaskCreate
) -> bool:
    """Backward-compatible create-state-specific marker clear."""
    return _clear_pending_task_state(project_dir, pending)


def reconcile_pending_task_create(project_dir: str | Path) -> bool:
    """Resolve an uncertain create and confirm child-first archive cleanup."""
    pending = _load_pending_task_state(project_dir)
    if pending is None:
        return not _pending_task_create_marker(project_dir).exists()
    if isinstance(pending, PendingBarrierCommit):
        barrier_status = _task_status_in_snapshot(
            tenant=pending.tenant,
            task_id=pending.barrier_task_id,
        )
        if barrier_status == "done":
            return _clear_pending_task_state(project_dir, pending)
        if barrier_status in {"ready", "todo"}:
            try:
                _complete_registration_barrier(pending.barrier_task_id)
            except RuntimeError:
                return False
            return _clear_pending_task_state(project_dir, pending)
        if barrier_status in {"failed", "archived"}:
            cleanup = PendingTaskCleanup(
                pending.tenant,
                pending.tick_id,
                pending.cleanup_task_ids,
            )
            return _persist_and_archive_cleanup(project_dir, cleanup)
        return False
    if isinstance(pending, PendingTaskCleanup):
        cleanup = pending
    else:
        task_id = _find_task_id_in_snapshot(
            tenant=pending.tenant,
            tick_id=pending.tick_id,
            phase_key=pending.phase_key,
        )
        if task_id is None:
            return False
        cleanup = PendingTaskCleanup(
            tenant=pending.tenant,
            tick_id=pending.tick_id,
            task_ids=(task_id, *reversed(pending.known_task_ids)),
        )
        try:
            _persist_pending_task_cleanup(project_dir, cleanup)
        except OSError:
            log.warning(
                "failed to persist resolved child-first cleanup for tick %s",
                pending.tick_id,
            )
            return False

    if not _archive_tasks(list(cleanup.task_ids), tenant=cleanup.tenant):
        return False
    return _clear_pending_task_state(project_dir, cleanup)


def _parse_task_id(stdout: str) -> str | None:
    task_id = None
    try:
        task_data = json.loads(stdout)
        if isinstance(task_data, dict):
            task_id = task_data.get("id")
    except json.JSONDecodeError:
        pass
    if task_id is None:
        parts = stdout.strip().split()
        if len(parts) >= 2 and parts[0] == "Created":
            task_id = parts[1]
    if not isinstance(task_id, str) or re.fullmatch(r"t_[0-9a-f]{8}", task_id) is None:
        return None
    return task_id


def _list_task_snapshot(tenant: str) -> list[dict[str, object]] | None:
    """Return the current Hermes task snapshot, including archived tasks."""
    try:
        result = subprocess.run(
            [
                "hermes",
                "kanban",
                "list",
                "--tenant",
                tenant,
                "--archived",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            return None
        snapshot = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None

    if isinstance(snapshot, list):
        tasks = snapshot
    elif isinstance(snapshot, dict):
        tasks = snapshot.get("tasks", [])
    else:
        return None
    if not isinstance(tasks, list):
        return None
    return [task for task in tasks if isinstance(task, dict)]


def _task_status_in_snapshot(*, tenant: str, task_id: str) -> str | None:
    """Return a task's status from a complete snapshot, or None if uncertain."""
    tasks = _list_task_snapshot(tenant)
    if tasks is None:
        return None
    for task in tasks:
        if task.get("id") == task_id:
            status = task.get("status")
            return status if isinstance(status, str) else None
    return None


def _find_task_id_in_snapshot(
    *, tenant: str, tick_id: str, phase_key: str
) -> str | None:
    """Resolve a task after an inconclusive idempotent create retry."""
    tasks = _list_task_snapshot(tenant)
    if tasks is None:
        return None
    for task in tasks:
        try:
            header = json.loads(task.get("body", "").split("\n", 1)[0])
        except (AttributeError, json.JSONDecodeError):
            continue
        if not isinstance(header, dict):
            continue
        if (
            header.get("tick_id") == tick_id
            and header.get("phase_key") == phase_key
        ):
            return _parse_task_id(json.dumps({"id": task.get("id")}))
    return None


def _recover_uncertain_task_id(
    cmd: list[str], *, tenant: str, tick_id: str, phase_key: str
) -> str | None:
    """Repeat an idempotent create once to recover a remotely-created task ID."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=KANBAN_QUERY_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return _find_task_id_in_snapshot(
            tenant=tenant,
            tick_id=tick_id,
            phase_key=phase_key,
        )
    if result.returncode != 0:
        task_id = None
    else:
        task_id = _parse_task_id(result.stdout)
    return task_id or _find_task_id_in_snapshot(
        tenant=tenant,
        tick_id=tick_id,
        phase_key=phase_key,
    )


def _persist_and_archive_cleanup(
    project_dir: str | Path,
    cleanup: PendingTaskCleanup,
) -> bool:
    """Persist ordered cleanup before attempting it and clear only on proof."""
    try:
        _persist_pending_task_cleanup(project_dir, cleanup)
    except OSError:
        log.warning(
            "failed to persist child-first cleanup for tick %s",
            cleanup.tick_id,
        )
        return False
    if not _archive_tasks(list(cleanup.task_ids), tenant=cleanup.tenant):
        return False
    return _clear_pending_task_state(project_dir, cleanup)


def _recover_and_archive_uncertain_task(
    cmd: list[str],
    *,
    project_dir: str | Path,
    pending: PendingTaskCreate,
) -> bool:
    uncertain_task_id = _recover_uncertain_task_id(
        cmd,
        tenant=pending.tenant,
        tick_id=pending.tick_id,
        phase_key=pending.phase_key,
    )
    if uncertain_task_id is None:
        return False
    cleanup = PendingTaskCleanup(
        tenant=pending.tenant,
        tick_id=pending.tick_id,
        task_ids=(uncertain_task_id, *reversed(pending.known_task_ids)),
    )
    return _persist_and_archive_cleanup(project_dir, cleanup)


def _block_gate_task(task_id: str) -> None:
    """Write Hermes's sticky human-input block event for an unassigned gate."""
    try:
        result = subprocess.run(
            [
                "hermes",
                "kanban",
                "block",
                "--kind",
                "needs_input",
                task_id,
            ],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"failed to block kanban gate {task_id}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to block kanban gate {task_id}: "
            f"rc={result.returncode} "
            f"stderr={result.stderr[:ERROR_MSG_MAX_LENGTH]}"
        )


def _complete_registration_barrier(task_id: str) -> None:
    """Commit a durable phase registration by completing its barrier."""
    try:
        result = subprocess.run(
            ["hermes", "kanban", "complete", task_id],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(
            f"failed to complete registration barrier {task_id}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to complete registration barrier {task_id}: "
            f"rc={result.returncode} "
            f"stderr={result.stderr[:ERROR_MSG_MAX_LENGTH]}"
        )


def prepare_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    phases_path: str | Path | None = None,
    prompt_client: PromptClient = "claude",
) -> list[PreparedPhaseTask]:
    if not re.fullmatch(r"TODO-\d+", todo_id):
        raise ValueError(f"invalid todo_id format: {todo_id!r} (expected TODO-N)")
    phases = load_phases(phases_path)
    prepared: list[PreparedPhaseTask] = []
    for phase in phases:
        rendered_prompt = _render_phase_prompt(
            phase.prompt,
            todo_id=todo_id,
            tick_id=tick_id,
            project_slug=board_slug,
            prompt_client=prompt_client,
            template_source=f"{phases_path or 'gstack'}:{phase.phase_key}",
        )
        if phase.gate:
            body_prompt = rendered_prompt
        else:
            body_prompt = _external_agent_prompt_block(
                rendered_prompt, prompt_client=prompt_client
            )
        delegation = (
            ""
            if phase.gate
            else _external_client_delegation_block(
                prompt_client,
                timeout=phase.timeout,
            )
        )
        prepared.append(
            PreparedPhaseTask(
                phase_key=phase.phase_key,
                name=phase.name,
                body=(
                    _build_json_header(
                        tick_id=tick_id,
                        phase_key=phase.phase_key,
                        todo_id=todo_id,
                        project_slug=board_slug,
                    )
                    + "\n"
                    + delegation
                    + body_prompt
                ),
                turns=phase.turns,
                gate=phase.gate,
                timeout=phase.timeout,
            )
        )
    return prepared


def _registration_barrier_body(*, tick_id: str, project_slug: str) -> str:
    header = json.dumps(
        {
            "infrastructure": _REGISTRATION_BARRIER_INFRASTRUCTURE,
            "phase_key": _REGISTRATION_BARRIER_PHASE_KEY,
            "project_slug": project_slug,
            "tick_id": tick_id,
        },
        sort_keys=True,
    )
    return (
        f"{header}\n"
        "Registration infrastructure: completing this nonspawnable barrier "
        "commits the durable phase chain."
    )


def _cleanup_after_local_create_error(
    project_dir: str | Path,
    pending: PendingTaskCreate,
) -> bool:
    """Convert a conclusive local create failure to known-ID cleanup state."""
    cleanup_ids = tuple(reversed(pending.known_task_ids))
    if not cleanup_ids:
        return _clear_pending_task_state(project_dir, pending)
    cleanup = PendingTaskCleanup(
        tenant=pending.tenant,
        tick_id=pending.tick_id,
        task_ids=cleanup_ids,
    )
    return _persist_and_archive_cleanup(project_dir, cleanup)


def _run_durable_task_create(
    *,
    cmd: list[str],
    project_dir: str | Path,
    pending: PendingTaskCreate,
) -> tuple[str, PendingTaskCleanup]:
    """Run one idempotent create while preserving crash-recovery state."""
    try:
        _persist_pending_task_create(project_dir, pending)
    except OSError as exc:
        cleanup = PendingTaskCleanup(
            tenant=pending.tenant,
            tick_id=pending.tick_id,
            task_ids=tuple(reversed(pending.known_task_ids)),
        )
        if cleanup.task_ids:
            _persist_and_archive_cleanup(project_dir, cleanup)
        raise RuntimeError(
            f"failed to persist pending kanban task {pending.phase_key} "
            f"for tick {pending.tick_id}: {exc}"
        ) from exc

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=KANBAN_QUERY_TIMEOUT,
        )
    except OSError as exc:
        cleanup_succeeded = _cleanup_after_local_create_error(project_dir, pending)
        cleanup_detail = "" if cleanup_succeeded else "; cleanup remains pending"
        raise RuntimeError(
            f"failed to register kanban task {pending.phase_key} "
            f"for tick {pending.tick_id}: Hermes process failed: "
            f"{exc}{cleanup_detail}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        cleanup_succeeded = _recover_and_archive_uncertain_task(
            cmd,
            project_dir=project_dir,
            pending=pending,
        )
        cleanup_detail = "" if cleanup_succeeded else "; recovery remains pending"
        raise RuntimeError(
            f"failed to register kanban task {pending.phase_key} "
            f"for tick {pending.tick_id}: Hermes process failed: "
            f"{exc}{cleanup_detail}"
        ) from exc

    if result.returncode != 0:
        cleanup_succeeded = _recover_and_archive_uncertain_task(
            cmd,
            project_dir=project_dir,
            pending=pending,
        )
        cleanup_detail = "" if cleanup_succeeded else "; recovery remains pending"
        log.error(
            "failed to register prepared kanban task %s for tick %s: rc=%d stderr=%s",
            pending.phase_key,
            pending.tick_id,
            result.returncode,
            result.stderr[:ERROR_MSG_MAX_LENGTH],
        )
        raise RuntimeError(
            f"failed to register kanban task {pending.phase_key} "
            f"for tick {pending.tick_id}: rc={result.returncode} "
            f"stderr={result.stderr[:ERROR_MSG_MAX_LENGTH]}"
            f"{cleanup_detail}"
        )

    task_id = _parse_task_id(result.stdout)
    if task_id is None:
        cleanup_succeeded = _recover_and_archive_uncertain_task(
            cmd,
            project_dir=project_dir,
            pending=pending,
        )
        cleanup_detail = "" if cleanup_succeeded else "; recovery remains pending"
        idempotency_key = f"{pending.tick_id}:{pending.phase_key}"
        raise RuntimeError(
            f"{pending.phase_key}: failed to parse valid task ID; "
            f"inspect Hermes task with idempotency key {idempotency_key}: "
            f"{result.stdout[:ERROR_MSG_MAX_LENGTH]}{cleanup_detail}"
        )

    cleanup = PendingTaskCleanup(
        tenant=pending.tenant,
        tick_id=pending.tick_id,
        task_ids=(task_id, *reversed(pending.known_task_ids)),
    )
    try:
        _persist_pending_task_cleanup(project_dir, cleanup)
    except OSError as exc:
        raise RuntimeError(
            f"failed to persist ordered cleanup after registering "
            f"{pending.phase_key} for tick {pending.tick_id}: {exc}"
        ) from exc
    return task_id, cleanup


def create_prepared_todo_phases(
    *,
    prepared: list[PreparedPhaseTask],
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    assignee: str = "default",
) -> list[str]:
    """Create a nonspawnable registration barrier and its phase task chain.

    Executable tasks depend on the barrier, gates remain detached and receive a
    sticky Hermes block event, and barrier completion commits the registration
    only after the expected-phase sentinel is durable.

    Args:
        prepared: Fully rendered phase tasks, in registration order.
        tick_id: ULID tick ID.
        board_slug: Kanban board slug (project slug).
        project_dir: Project directory for --workspace.

    Returns:
        List of created task IDs in phase order.

    Raises:
        RuntimeError: If registration cannot commit or cleanup cannot be
            confirmed. Unconfirmed cleanup remains durable for reconciliation.
    """
    project_dir = Path(project_dir)
    created_task_ids: list[str] = []
    phase_task_ids: list[str] = []
    previous_dependency_id: str | None = None

    barrier_cmd = [
        "hermes",
        "kanban",
        "create",
        "--tenant",
        board_slug,
        f"Registration barrier: {tick_id}",
        "--body",
        _registration_barrier_body(tick_id=tick_id, project_slug=board_slug),
        "--workspace",
        f"dir:{project_dir}",
        "--idempotency-key",
        f"{tick_id}:{_REGISTRATION_BARRIER_PHASE_KEY}",
        "--assignee",
        "-",
        "--json",
    ]
    barrier_id, cleanup = _run_durable_task_create(
        cmd=barrier_cmd,
        project_dir=project_dir,
        pending=PendingTaskCreate(
            tenant=board_slug,
            tick_id=tick_id,
            phase_key=_REGISTRATION_BARRIER_PHASE_KEY,
            known_task_ids=(),
        ),
    )
    created_task_ids.append(barrier_id)

    for phase in prepared:
        is_gate = phase.gate
        cmd = [
            "hermes",
            "kanban",
            "create",
            "--tenant",
            board_slug,
            phase.name,
            "--body",
            phase.body,
            "--workspace",
            f"dir:{project_dir}",
            "--idempotency-key",
            f"{tick_id}:{phase.phase_key}",
            "--assignee",
            "-" if is_gate else assignee,
            "--json",
        ]
        cmd.extend(["--parent", previous_dependency_id or barrier_id])
        if not is_gate:
            cmd.extend(
                [
                    "--max-runtime",
                    str(phase.timeout),
                    "--goal",
                    "--goal-max-turns",
                    str(phase.turns),
                ]
            )

        log.info(
            "registering prepared kanban task: phase=%s tick=%s",
            phase.phase_key,
            tick_id,
        )
        task_id, cleanup = _run_durable_task_create(
            cmd=cmd,
            project_dir=project_dir,
            pending=PendingTaskCreate(
                tenant=board_slug,
                tick_id=tick_id,
                phase_key=phase.phase_key,
                known_task_ids=tuple(created_task_ids),
            ),
        )
        created_task_ids.append(task_id)
        phase_task_ids.append(task_id)

        if is_gate:
            try:
                _block_gate_task(task_id)
            except Exception as exc:
                cleanup_succeeded = _persist_and_archive_cleanup(
                    project_dir,
                    cleanup,
                )
                cleanup_detail = (
                    "" if cleanup_succeeded else "; cleanup remains pending"
                )
                raise RuntimeError(
                    f"failed to apply sticky block to gate {phase.phase_key} "
                    f"for tick {tick_id}: {exc}{cleanup_detail}"
                ) from exc
        previous_dependency_id = task_id

    try:
        _persist_expected_phases(prepared, project_dir=project_dir)
    except Exception as exc:
        cleanup_succeeded = _persist_and_archive_cleanup(project_dir, cleanup)
        cleanup_detail = "" if cleanup_succeeded else "; cleanup remains pending"
        raise RuntimeError(
            f"failed to commit durable kanban chain for tick {tick_id}: "
            f"{exc}{cleanup_detail}"
        ) from exc

    commit_pending = PendingBarrierCommit(
        tenant=board_slug,
        tick_id=tick_id,
        barrier_task_id=barrier_id,
        cleanup_task_ids=cleanup.task_ids,
    )
    try:
        _persist_pending_barrier_commit(project_dir, commit_pending)
    except OSError as exc:
        cleanup_succeeded = _persist_and_archive_cleanup(project_dir, cleanup)
        cleanup_detail = "" if cleanup_succeeded else "; cleanup remains pending"
        raise RuntimeError(
            f"failed to persist barrier commit state for tick {tick_id}: "
            f"{exc}{cleanup_detail}"
        ) from exc

    try:
        _complete_registration_barrier(barrier_id)
    except Exception as exc:
        raise RuntimeError(
            f"failed to commit durable kanban chain for tick {tick_id}: "
            f"{exc}; barrier commit remains pending"
        ) from exc

    if not _clear_pending_task_state(project_dir, commit_pending):
        raise RuntimeError(
            f"committed durable kanban chain for tick {tick_id}, but failed "
            "to clear barrier commit state; reconciliation remains pending"
        )

    return phase_task_ids


def register_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
    assignee: str = "default",
    prompt_client: PromptClient = "claude",
) -> list[str]:
    """Prepare and register phases as backward-compatible kanban tasks."""
    prepared = prepare_todo_phases(
        todo_id=todo_id,
        tick_id=tick_id,
        board_slug=board_slug,
        phases_path=phases_path,
        prompt_client=prompt_client,
    )
    return create_prepared_todo_phases(
        prepared=prepared,
        tick_id=tick_id,
        board_slug=board_slug,
        project_dir=project_dir,
        assignee=assignee,
    )


def _persist_expected_phases(
    phases: list,
    *,
    project_dir: Path | str | None = None,
) -> None:
    """Write expected phase keys to a sentinel file for crash recovery.

    Called after successful registration so all_phases_complete can verify
    all expected phases are present (guards against partial registration).

    Args:
        phases: List of phase objects.
        project_dir: If given, write to <project_dir>/.hermes/outcomes/.
            Defaults to .hermes/outcomes/ for backward compatibility.
    """
    phase_keys = [p.phase_key for p in phases]
    if project_dir is not None:
        outcomes_dir = Path(project_dir) / ".hermes" / "outcomes"
    else:
        outcomes_dir = Path(".hermes") / "outcomes"
    try:
        outcomes_dir.mkdir(parents=True, exist_ok=True)
        # Overwrite previous — only the latest registration matters.
        sentinel = outcomes_dir / "expected-phases.json"
        _atomic_write_text(sentinel, json.dumps(phase_keys, sort_keys=False))
    except OSError as exc:
        raise RuntimeError("failed to persist expected phases sentinel") from exc


def _archive_tasks(task_ids: list[str], *, tenant: str | None = None) -> bool:
    """Archive task IDs in order, confirming each child before its parent."""
    if not task_ids:
        return True

    if tenant is None:
        command_succeeded = True
        for task_id in task_ids:
            try:
                result = subprocess.run(
                    ["hermes", "kanban", "archive", task_id],
                    capture_output=True,
                    text=True,
                    timeout=HERMES_COMMAND_TIMEOUT,
                    check=False,
                )
                if result.returncode != 0:
                    command_succeeded = False
                    log.warning(
                        "failed to archive task %s: rc=%d stderr=%s",
                        task_id,
                        result.returncode,
                        result.stderr[:ERROR_MSG_MAX_LENGTH],
                    )
            except Exception as exc:
                command_succeeded = False
                log.warning("failed to archive task %s: %s", task_id, exc)
        return command_succeeded

    snapshot = _list_task_snapshot(tenant)
    statuses = (
        {
            task["id"]: task.get("status")
            for task in snapshot
            if isinstance(task.get("id"), str)
        }
        if snapshot is not None
        else {}
    )
    for task_id in task_ids:
        if statuses.get(task_id) == "archived":
            continue
        try:
            result = subprocess.run(
                ["hermes", "kanban", "archive", task_id],
                capture_output=True,
                text=True,
                timeout=HERMES_COMMAND_TIMEOUT,
                check=False,
            )
            if result.returncode != 0:
                log.warning(
                    "failed to archive task %s: rc=%d stderr=%s",
                    task_id,
                    result.returncode,
                    result.stderr[:ERROR_MSG_MAX_LENGTH],
                )
        except Exception as exc:
            log.warning("failed to archive task %s: %s", task_id, exc)

        snapshot = _list_task_snapshot(tenant)
        if snapshot is None:
            return False
        statuses = {
            task["id"]: task.get("status")
            for task in snapshot
            if isinstance(task.get("id"), str)
        }
        if statuses.get(task_id) != "archived":
            return False
        log.info("confirmed archived kanban task %s", task_id)
    return True


def complete_todo_kanban_task(tenant: str, task_id: str) -> bool:
    """Complete a kanban task via `hermes kanban complete` (best-effort).

    `hermes kanban complete` has no --tenant flag — tenant is accepted for
    call-site symmetry with other kanban_tasks functions but unused.

    Returns True on success, False otherwise, so callers can distinguish
    failure from success instead of assuming this always worked.
    """
    try:
        result = subprocess.run(
            ["hermes", "kanban", "complete", task_id],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            log.warning(
                "failed to complete kanban task %s: rc=%d stderr=%s",
                task_id,
                result.returncode,
                result.stderr[:ERROR_MSG_MAX_LENGTH],
            )
            return False
        log.info("completed kanban task %s", task_id)
        return True
    except Exception as e:
        log.warning("failed to complete task %s: %s", task_id, e)
        return False


def get_todo_kanban_status(tenant: str, tick_id: str) -> dict[str, str]:
    """Query kanban for all tasks of a tick, return {phase_key: status}.

    Args:
        tenant: Tenant (project slug) to filter by.
        tick_id: ULID tick ID to filter tasks by.

    Returns:
        Dict mapping phase_key to status for tasks matching the tick_id.
        Empty dict if no tasks found or CLI fails.
    """
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--tenant", tenant, "--json"],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            return {}
        snapshot = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        log.warning("kanban list failed for tenant=%s", tenant)
        return {}

    # hermes kanban list --json returns a list; older versions returned
    # {"tasks": [...]} — handle both.
    if isinstance(snapshot, list):
        tasks = snapshot
    else:
        tasks = snapshot.get("tasks", [])

    status_map: dict[str, str] = {}
    for task in tasks:
        body = task.get("body", "")
        first_line = body.split("\n")[0]
        try:
            header = json.loads(first_line)
            if header.get("tick_id") != tick_id:
                continue
            phase_key = header.get("phase_key")
            if phase_key:
                status_map[phase_key] = task.get("status", "unknown")
        except (json.JSONDecodeError, IndexError):
            pass

    return status_map


@dataclass(frozen=True)
class KanbanTaskInfo:
    """One kanban task, resolved by phase_key for a single tick."""
    task_id: str
    phase_key: str
    status: str
    todo_id: str


def get_todo_kanban_tasks(tenant: str, tick_id: str) -> dict[str, KanbanTaskInfo]:
    """Query kanban for all tasks of a tick, return {phase_key: KanbanTaskInfo}.

    Like get_todo_kanban_status but preserves the task id and todo id so
    callers can complete the gate task and match it to a ship sidecar.
    Returns an empty dict if no tasks match or the CLI fails.
    """
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--tenant", tenant, "--json"],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            return {}
        snapshot = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        log.warning("kanban list failed for tenant=%s", tenant)
        return {}

    tasks = snapshot if isinstance(snapshot, list) else snapshot.get("tasks", [])

    out: dict[str, KanbanTaskInfo] = {}
    for task in tasks:
        body = task.get("body", "")
        first_line = body.split("\n")[0]
        try:
            header = json.loads(first_line)
        except (json.JSONDecodeError, IndexError):
            continue
        if header.get("tick_id") != tick_id:
            continue
        phase_key = header.get("phase_key")
        if not phase_key:
            continue
        out[phase_key] = KanbanTaskInfo(
            task_id=task.get("id", ""),
            phase_key=phase_key,
            status=task.get("status", "unknown"),
            todo_id=header.get("todo_id", ""),
        )
    return out


def all_phases_complete(
    tenant: str,
    tick_id: str,
    *,
    state_dir: str | Path | None = None,
) -> bool:
    """Check if all kanban tasks for a tick are in completion statuses.

    Completion statuses: done, failed. Archived phases (from mid-registration
    cleanup) are excluded — they indicate the tick didn't finish cleanly,
    so we hold the lock until the operator intervenes or the stale lock
    is reclaimed.

    Args:
        tenant: Tenant (project slug) to filter by.
        tick_id: ULID tick ID.
        state_dir: State directory — used to check for the picked=None
            sentinel when no kanban tasks exist.

    Returns:
        True if every task for the tick is in a completion status.
        False if any task is still in-flight, archived, or if the CLI fails
        (conservative: don't release lock on failure).
    """
    status_map = get_todo_kanban_status(tenant, tick_id)

    if not status_map:
        # No tasks found — could be:
        # (a) picked=None, so no phases were registered.
        # (b) tick_id persisted but crash before/during registration —
        #     tick_started sentinel present, no kanban tasks.
        # (c) first tick, hasn't registered yet (shouldn't reach here).
        if state_dir is not None:
            outcomes_dir = Path(state_dir) / "outcomes"
            sentinel = outcomes_dir / f"{tick_id}-phases.json"
            if sentinel.exists():
                try:
                    lines = sentinel.read_text().strip().split("\n")
                    for line in lines:
                        data = json.loads(line)
                        outcome = data.get("outcome")
                        if outcome == OUTCOME_PICKED_NONE:
                            return True  # Prior tick completed, no work
                        # tick_started sentinel alone means crash before/during
                        # registration — NOT complete. Treat as stall so the
                        # circuit breaker can detect it. Do NOT return True.
                except (json.JSONDecodeError, OSError):
                    pass
        # Conservative: return False so we don't accidentally release.
        # In the tick flow, the check is only done when a prior tick
        # had a picked TODO, so empty here means still in-flight.
        return False

    for phase_key, status in status_map.items():
        if status not in COMPLETION_STATUSES:
            log.debug(
                "phase %s for tick %s is still %s (not in completion status %s)",
                phase_key, tick_id, status, sorted(COMPLETION_STATUSES),
            )
            return False

    # Guard against partial registration: if we have an expected-phases
    # sentinel, verify all expected phases are in the status map.
    try:
        state_dir_path = Path(state_dir) if state_dir else Path(".hermes")
        outcomes_dir = state_dir_path / "outcomes"
        expected_file = outcomes_dir / "expected-phases.json"
        if expected_file.exists():
            expected_keys = json.loads(expected_file.read_text())
            for key in expected_keys:
                if key not in status_map:
                    # Plan-gate exception: when rejected, the gate task is
                    # archived (no longer in the kanban list). A rejection
                    # sidecar on disk is the authoritative signal — treat it
                    # as a completion (failed) status so the tick advances.
                    log.warning(
                        "expected phase %s not found in status map for tick %s "
                        "(partial registration suspected)",
                        key, tick_id,
                    )
                    return False
    except (json.JSONDecodeError, OSError):
        # If we can't read the sentinel, proceed without the check.
        pass

    return True


def observe_outcomes(
    *,
    state_dir: Path | str,
    tick_id: str,
    status_map: dict[str, str],
) -> None:
    """Write phase outcomes to JSONL sidecar based on kanban task status.

    Direction 2 — Kanban -> Decision Store: reads the kanban status map
    and appends outcome entries to .hermes/outcomes/<tick_id>-phases.json.

    High-watermark: reads existing outcomes to avoid re-writing phases that
    were already observed.

    Args:
        state_dir: State directory (e.g., Path(".hermes")).
        tick_id: ULID tick ID for the outcome file.
        status_map: Dict mapping phase_key to kanban status.
    """
    state_dir = Path(state_dir)
    outcomes_dir = state_dir / "outcomes"
    outcomes_dir.mkdir(parents=True, exist_ok=True)

    phases_file = outcomes_dir / f"{tick_id}-phases.json"

    # Read existing outcomes (high-watermark to avoid duplicates)
    existing = set()
    if phases_file.exists():
        content = phases_file.read_text().strip()
        if content:
            for line in content.split("\n"):
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    key = entry.get("phase_key", "")
                    if key:
                        existing.add(key)
                    # Track outcome-level sentinels (e.g. all_phases_complete)
                    outcome = entry.get("outcome", "")
                    if outcome:
                        existing.add(outcome)

    new_outcomes: list[str] = []

    for phase_key, status in status_map.items():
        if status == "done":
            if phase_key not in existing:
                new_outcomes.append(
                    json.dumps(
                        {
                            "outcome": OUTCOME_PHASE_COMPLETE,
                            "phase_key": phase_key,
                        },
                        sort_keys=True,
                    )
                )
        elif status == "failed":
            if phase_key not in existing:
                new_outcomes.append(
                    json.dumps(
                        {
                            "outcome": f"failed_at_phase_{phase_key}",
                            "detail": {"kanban_status": "failed"},
                        },
                        sort_keys=True,
                    )
                )
        elif status == "archived":
            if phase_key not in existing:
                new_outcomes.append(
                    json.dumps(
                        {
                            "outcome": "failed_at_phase_" + phase_key,
                            "detail": {"kanban_status": "archived"},
                        },
                        sort_keys=True,
                    )
                )
        # running, ready, created — no outcome line

    # Check if all tasks are in completion statuses (done/failed, not archived)
    all_complete = (
        len(status_map) > 0
        and all(s in COMPLETION_STATUSES for s in status_map.values())
    )
    if all_complete and OUTCOME_ALL_COMPLETE not in existing:
        new_outcomes.append(
            json.dumps(
                {
                    "outcome": OUTCOME_ALL_COMPLETE,
                },
                sort_keys=True,
            )
        )

    if new_outcomes:
        fd = os.open(str(phases_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for line in new_outcomes:
                os.write(fd, (line + "\n").encode())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        log.info(
            "observed %d outcomes for tick %s", len(new_outcomes), tick_id
        )
