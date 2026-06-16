"""Kanban task registration for kanban-as-scheduler (TODO-10).

Uses raw `hermes kanban` CLI directly — not through HermesKanbanAdapter.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .phases import load_phases, _render_phase_prompt

log = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"done", "failed", "archived"})


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

        # Build command
        cmd = [
            "hermes",
            "kanban",
            "create",
            "--board", board_slug,
            "--title", phase.name,
            "--body", body,
            "--workspace", f"dir:{project_dir}",
            "--idempotency-key", f"{tick_id}:{phase.phase_key}",
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

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            # Mid-registration failure: archive already-created tasks
            log.error(
                "failed to register kanban task %s for %s: rc=%d stderr=%s",
                phase.phase_key,
                todo_id,
                result.returncode,
                result.stderr[:200],
            )
            _archive_tasks(task_ids)
            raise RuntimeError(
                f"failed to register kanban task {phase.phase_key} "
                f"for {todo_id}: rc={result.returncode} stderr={result.stderr[:200]}"
            )

        # Parse task ID from output (stdout contains the task ID)
        task_id = result.stdout.strip()
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
                timeout=10,
                check=False,
            )
            log.info("archived kanban task %s", task_id)
        except Exception as e:
            log.warning("failed to archive task %s: %s", task_id, e)
