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

from .outcomes import (
    OUTCOME_ALL_COMPLETE,
    OUTCOME_FAILED_TO_SPAWN,
    OUTCOME_PHASE_COMPLETE,
    OUTCOME_PICKED_NONE,
    OUTCOME_TICK_STARTED,
)
from .phases import load_phases, _render_phase_prompt

# Sentinel written after successful registration to record expected phases.
_EXPECTED_PHASES_FILE_SUFFIX = ".expected-phases.json"

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


def register_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
    assignee: str = "default",
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
            "--assignee", assignee,
            "--json",
        ]

        # Add --parent for phases after the first
        if phase_idx > 0:
            cmd.extend(["--parent", task_ids[phase_idx - 1]])

        # Gate phases are pure markers: created blocked, never dispatched to
        # an agent. Everything else runs as a goal-mode kanban task.
        if getattr(phase, "gate", False):
            cmd.extend(["--initial-status", BLOCKED])
        else:
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

    # Persist expected phase keys so all_phases_complete can verify
    # completeness (guards against partial registration on crash).
    _persist_expected_phases(phases, project_dir=project_dir)

    return task_ids


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
    try:
        phase_keys = [p.phase_key for p in phases]
        if project_dir is not None:
            outcomes_dir = Path(project_dir) / ".hermes" / "outcomes"
        else:
            outcomes_dir = Path(".hermes") / "outcomes"
        outcomes_dir.mkdir(parents=True, exist_ok=True)
        # Overwrite previous — only the latest registration matters.
        sentinel = outcomes_dir / "expected-phases.json"
        sentinel.write_text(json.dumps(phase_keys, sort_keys=False))
    except OSError:
        # Best-effort — don't fail registration if we can't write the sentinel.
        log.warning("failed to persist expected phases sentinel")


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
