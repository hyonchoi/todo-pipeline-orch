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
from .phases import _render_phase_prompt, load_phases
from .state import _atomic_write_text

# Sentinel written after successful registration to record expected phases.
_EXPECTED_PHASES_FILE_SUFFIX = ".expected-phases.json"
_PENDING_TASK_CREATE_FILE = "pending-task-create.json"

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


@dataclass(frozen=True)
class PendingTaskCreate:
    """A create whose remote task ID was not visible during recovery."""

    tenant: str
    tick_id: str
    phase_key: str
    known_task_ids: tuple[str, ...]


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


def _load_pending_task_create(project_dir: str | Path) -> PendingTaskCreate | None:
    """Load a valid pending-create marker, failing closed for malformed data."""
    marker = _pending_task_create_marker(project_dir)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
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


def _clear_pending_task_create(
    project_dir: str | Path, pending: PendingTaskCreate
) -> bool:
    """Remove a pending marker only when it still describes this create."""
    if _load_pending_task_create(project_dir) != pending:
        return False
    marker = _pending_task_create_marker(project_dir)
    try:
        marker.unlink()
    except OSError:
        log.warning("failed to remove pending task-create marker: %s", marker)
        return False
    return True


def reconcile_pending_task_create(project_dir: str | Path) -> bool:
    """Archive a delayed idempotent create once it becomes visible."""
    pending = _load_pending_task_create(project_dir)
    if pending is None:
        return False
    task_id = _find_task_id_in_snapshot(
        tenant=pending.tenant,
        tick_id=pending.tick_id,
        phase_key=pending.phase_key,
    )
    if task_id is None:
        return False
    if not _archive_tasks([*pending.known_task_ids, task_id]):
        return False
    marker = _pending_task_create_marker(project_dir)
    try:
        marker.unlink()
    except OSError:
        log.warning("failed to remove pending task-create marker: %s", marker)
        return False
    return True


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


def _find_task_id_in_snapshot(
    *, tenant: str, tick_id: str, phase_key: str
) -> str | None:
    """Resolve a task after an inconclusive idempotent create retry."""
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--tenant", tenant, "--json"],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
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


def _recover_and_archive_uncertain_task(
    cmd: list[str],
    task_ids: list[str],
    *,
    tenant: str,
    tick_id: str,
    phase_key: str,
) -> bool:
    uncertain_task_id = _recover_uncertain_task_id(
        cmd,
        tenant=tenant,
        tick_id=tick_id,
        phase_key=phase_key,
    )
    cleanup_ids = [
        *task_ids,
        *([uncertain_task_id] if uncertain_task_id is not None else []),
    ]
    cleanup_succeeded = _archive_tasks(cleanup_ids)
    return uncertain_task_id is not None and cleanup_succeeded


def _promote_task(task_id: str) -> None:
    """Activate a blocked task after its complete chain is durable."""
    try:
        result = subprocess.run(
            ["hermes", "kanban", "promote", task_id],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"failed to promote kanban task {task_id}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to promote kanban task {task_id}: "
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
    return [
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
                + _render_phase_prompt(
                    phase.prompt,
                    todo_id=todo_id,
                    tick_id=tick_id,
                    project_slug=board_slug,
                    prompt_client=prompt_client,
                    template_source=f"{phases_path or 'gstack'}:{phase.phase_key}",
                )
            ),
            turns=phase.turns,
            gate=phase.gate,
        )
        for phase in phases
    ]


def create_prepared_todo_phases(
    *,
    prepared: list[PreparedPhaseTask],
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    assignee: str = "default",
) -> list[str]:
    """Create already-prepared phase tasks with a --parent dependency chain.

    Creates kanban tasks in order and links each executable task to its
    predecessor via --parent. Uses --idempotency-key for dedup.

    Args:
        prepared: Fully rendered phase tasks, in registration order.
        tick_id: ULID tick ID.
        board_slug: Kanban board slug (project slug).
        project_dir: Project directory for --workspace.

    Returns:
        List of created task IDs in phase order.

    Raises:
        RuntimeError: If task creation fails — already-created tasks are
            archived before raising.
    """
    project_dir = Path(project_dir)

    task_ids: list[str] = []

    for phase_idx, phase in enumerate(prepared):
        # Build command — title is positional, use --tenant for namespacing,
        # --json for structured task ID output.
        is_gate = phase.gate
        cmd = [
            "hermes",
            "kanban",
            "create",
            "--tenant", board_slug,
            phase.name,
            "--body", phase.body,
            "--workspace", f"dir:{project_dir}",
            "--idempotency-key", f"{tick_id}:{phase.phase_key}",
            "--json",
        ]
        if is_gate:
            cmd.extend(["--assignee", "-"])
        else:
            cmd.extend(["--assignee", assignee])
        cmd.extend(["--initial-status", BLOCKED])

        # Add --parent for executable phases after the first. Gate phases must
        # remain manually blocked; Hermes unblocks parented children when their
        # parent completes, which would bypass the human review gate.
        if phase_idx > 0 and not is_gate:
            cmd.extend(["--parent", task_ids[phase_idx - 1]])

        # Gate phases are pure markers, never dispatched to an agent.
        # Executable phases retain goal mode while registration holds them
        # blocked until the complete chain is durable.
        if not is_gate:
            cmd.extend(["--goal", "--goal-max-turns", str(phase.turns)])

        log.info(
            "registering prepared kanban task: phase=%s tick=%s",
            phase.phase_key,
            tick_id,
        )

        pending = PendingTaskCreate(
            tenant=board_slug,
            tick_id=tick_id,
            phase_key=phase.phase_key,
            known_task_ids=tuple(task_ids),
        )
        try:
            _persist_pending_task_create(project_dir, pending)
        except OSError as exc:
            raise RuntimeError(
                f"failed to persist pending kanban task {phase.phase_key} "
                f"for tick {tick_id}: {exc}"
            ) from exc

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=KANBAN_QUERY_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            if isinstance(exc, subprocess.TimeoutExpired):
                cleanup_succeeded = _recover_and_archive_uncertain_task(
                    cmd,
                    task_ids,
                    tenant=board_slug,
                    tick_id=tick_id,
                    phase_key=phase.phase_key,
                )
            else:
                cleanup_succeeded = _archive_tasks(task_ids)
            if cleanup_succeeded:
                _clear_pending_task_create(project_dir, pending)
            raise RuntimeError(
                f"failed to register kanban task {phase.phase_key} "
                f"for tick {tick_id}: Hermes process failed: {exc}"
            ) from exc
        if result.returncode != 0:
            # A nonzero response can still follow a successful remote mutation.
            cleanup_succeeded = _recover_and_archive_uncertain_task(
                cmd,
                task_ids,
                tenant=board_slug,
                tick_id=tick_id,
                phase_key=phase.phase_key,
            )
            if cleanup_succeeded:
                _clear_pending_task_create(project_dir, pending)
            log.error(
                "failed to register prepared kanban task %s for tick %s: rc=%d stderr=%s",
                phase.phase_key,
                tick_id,
                result.returncode,
                result.stderr[:ERROR_MSG_MAX_LENGTH],
            )
            raise RuntimeError(
                f"failed to register kanban task {phase.phase_key} "
                f"for tick {tick_id}: rc={result.returncode} "
                f"stderr={result.stderr[:ERROR_MSG_MAX_LENGTH]}"
            )

        # Parse task ID from JSON output (--json returns {"id": "t_xxx"}).
        # Older Hermes versions print "Created t_xxx (...)". Validate either
        # form before it can become the next phase's --parent argument.
        task_id = _parse_task_id(result.stdout)
        if task_id is None:
            cleanup_succeeded = _recover_and_archive_uncertain_task(
                cmd,
                task_ids,
                tenant=board_slug,
                tick_id=tick_id,
                phase_key=phase.phase_key,
            )
            if cleanup_succeeded:
                _clear_pending_task_create(project_dir, pending)
            idempotency_key = f"{tick_id}:{phase.phase_key}"
            raise RuntimeError(
                f"{phase.phase_key}: failed to parse valid task ID; "
                f"inspect Hermes task with idempotency key {idempotency_key}: "
                f"{result.stdout[:ERROR_MSG_MAX_LENGTH]}"
            )
        task_ids.append(task_id)
        if not _clear_pending_task_create(project_dir, pending):
            raise RuntimeError(
                f"failed to clear pending kanban task {phase.phase_key} "
                f"for tick {tick_id}"
            )
        log.info("registered kanban task: task_id=%s phase=%s", task_id, phase.phase_key)

    try:
        # Persist expected phase keys before making any executable task runnable.
        _persist_expected_phases(prepared, project_dir=project_dir)
        first_executable = next(
            task_id
            for task_id, phase in zip(task_ids, prepared, strict=True)
            if not phase.gate
        )
        _promote_task(first_executable)
    except Exception as exc:
        _archive_tasks(task_ids)
        raise RuntimeError(
            f"failed to activate durable kanban chain for tick {tick_id}: {exc}"
        ) from exc

    return task_ids


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


def _archive_tasks(task_ids: list[str]) -> bool:
    """Archive task IDs, returning whether every archive was confirmed."""
    archived_all = True
    for task_id in task_ids:
        try:
            result = subprocess.run(
                ["hermes", "kanban", "archive", task_id],
                capture_output=True,
                text=True,
                timeout=HERMES_COMMAND_TIMEOUT,
                check=False,
            )
            if result.returncode == 0:
                log.info("archived kanban task %s", task_id)
            else:
                archived_all = False
                log.warning(
                    "failed to archive task %s: rc=%d stderr=%s",
                    task_id,
                    result.returncode,
                    result.stderr[:ERROR_MSG_MAX_LENGTH],
                )
        except Exception as exc:
            archived_all = False
            log.warning("failed to archive task %s: %s", task_id, exc)
    return archived_all


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
