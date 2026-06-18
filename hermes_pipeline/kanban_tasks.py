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
from pathlib import Path

from .outcomes import (
    OUTCOME_ALL_COMPLETE,
    OUTCOME_FAILED_TO_SPAWN,
    OUTCOME_PHASE_COMPLETE,
    OUTCOME_PICKED_NONE,
    OUTCOME_TICK_STARTED,
)
from .phases import load_phases, _render_phase_prompt

log = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"done", "failed", "archived"})

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


def register_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
) -> list[str]:
    """Register phases as kanban tasks with --parent dependency chain.

    Reads phases.yaml, creates kanban tasks in order, and links each task
    to its predecessor via --parent. Uses --idempotency-key for dedup.

    Args:
        todo_id: TODO ID (e.g., "TODO-10").
        tick_id: ULID tick ID.
        board_slug: Kanban board slug (project slug).
        project_dir: Project directory for --workspace.
        phases_path: Optional path to phases.yaml. Defaults to repo default.

    Returns:
        List of created task IDs in phase order.

    Raises:
        RuntimeError: If task creation fails — already-created tasks are
            archived before raising.
    """
    project_dir = Path(project_dir)
    # Validate todo_id format before use in subprocess calls
    if not re.match(r'^TODO-\d+$', todo_id):
        raise ValueError(f"invalid todo_id format: {todo_id!r} (expected TODO-N)")
    phases = load_phases(phases_path)

    task_ids: list[str] = []

    for phase_idx, phase in enumerate(phases):
        # Build task body: JSON header + rendered phase prompt
        header = _build_json_header(
            tick_id=tick_id,
            phase_key=phase.phase_key,
            todo_id=todo_id,
            project_slug=board_slug,
        )
        body = header + "\n" + _render_phase_prompt(
            phase.prompt,
            todo_id=todo_id,
            tick_id=tick_id,
            project_slug=board_slug,
        )

        # Build command — title is positional, use --tenant for namespacing,
        # --json for structured task ID output.
        cmd = [
            "hermes",
            "kanban",
            "create",
            "--tenant", board_slug,
            phase.name,
            "--body", body,
            "--workspace", f"dir:{project_dir}",
            "--idempotency-key", f"{tick_id}:{phase.phase_key}",
            "--json",
        ]

        # Add --parent for phases after the first
        if phase_idx > 0:
            cmd.extend(["--parent", task_ids[phase_idx - 1]])

        # Add goal mode flags
        cmd.extend(["--goal", "--goal-max-turns", str(phase.turns)])

        log.info(
            "registering kanban task: phase=%s todo=%s tick=%s",
            phase.phase_key,
            todo_id,
            tick_id,
        )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=KANBAN_QUERY_TIMEOUT)
        if result.returncode != 0:
            # Mid-registration failure: archive already-created tasks
            log.error(
                "failed to register kanban task %s for %s: rc=%d stderr=%s",
                phase.phase_key,
                todo_id,
                result.returncode,
                result.stderr[:ERROR_MSG_MAX_LENGTH],
            )
            _archive_tasks(task_ids)
            raise RuntimeError(
                f"failed to register kanban task {phase.phase_key} "
                f"for {todo_id}: rc={result.returncode} stderr={result.stderr[:ERROR_MSG_MAX_LENGTH]}"
            )

        # Parse task ID from JSON output (--json returns {"id": "t_xxx"})
        try:
            task_data = json.loads(result.stdout)
            task_id = task_data["id"]
        except (json.JSONDecodeError, KeyError):
            # Fallback: old CLI returns "Created t_xxx  (ready, assignee=-)"
            line = result.stdout.strip()
            if line.startswith("Created"):
                task_id = line.split()[1]
            else:
                raise RuntimeError(f"failed to parse task ID from: {result.stdout[:ERROR_MSG_MAX_LENGTH]}")
        task_ids.append(task_id)
        log.info("registered kanban task: task_id=%s phase=%s", task_id, phase.phase_key)

    return task_ids


def _archive_tasks(task_ids: list[str]) -> None:
    """Archive a list of kanban task IDs (best-effort)."""
    for task_id in task_ids:
        try:
            subprocess.run(
                ["hermes", "kanban", "archive", task_id],
                capture_output=True,
                text=True,
                timeout=HERMES_COMMAND_TIMEOUT,
                check=False,
            )
            log.info("archived kanban task %s", task_id)
        except Exception as e:
            log.warning("failed to archive task %s: %s", task_id, e)


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
                        if outcome == OUTCOME_TICK_STARTED:
                            return True  # Crash before/during registration;
                                        # no tasks to wait for.
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
