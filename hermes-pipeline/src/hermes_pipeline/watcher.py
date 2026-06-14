"""Lane F.1: Auto-tick discovery + per-project isolation.

Implements:
  TF.1: watcher.py — auto tick discovery + per-project isolation (T4, T11)
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional

from .selection import select_for_project
from .state import State
from .slack import notify
from .logging_setup import new_tick_id, set_tick_id

log = logging.getLogger(__name__)


def discover_projects(projects_dir: Path | str) -> list[Path]:
    """
    Discover all projects with TODOS.md in projects_dir.

    Args:
        projects_dir: Base directory containing projects.

    Returns:
        List of project paths that contain TODOS.md files.
    """
    projects_dir = Path(projects_dir)
    projects = []
    if not projects_dir.exists():
        return projects

    for project_dir in projects_dir.iterdir():
        if project_dir.is_dir():
            todos_md = project_dir / "TODOS.md"
            if todos_md.exists():
                projects.append(project_dir)

    return sorted(projects)


def _compute_hash(content: str) -> str:
    """Compute SHA256 hash of TODOS.md content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def auto_tick(
    projects_dir: Path | str,
    lock_dir: Path | str,
    state_dir: Path | str,
    on_selected: Callable[[str, int], None],
    notify_fn: Callable[[str], None],
    slack_channel: str,
) -> None:
    """
    Run one tick of auto-discovery: iterate all projects, detect changes,
    and select eligible TODOs.

    Args:
        projects_dir: Base directory containing projects.
        lock_dir: Directory for .lock files.
        state_dir: Base state directory (~/.hermes).
        on_selected: Callback(project_name, todo_id) when a TODO is selected.
        notify_fn: Callback(message) to notify of issues.
        slack_channel: Slack channel for notifications.

    Workflow:
      1. Generate new tick_id and set it for correlation.
      2. For each project:
         a. Check if locked (skip if so).
         b. Read TODOS.md and compute hash.
         c. Compare hash to saved hash; skip if no change.
         d. Parse TODOS.md, detect cycles, notify changes.
         e. Call select_for_project() for eligible TODO.
         f. If selected: call on_selected(project, todo_id).
         g. Isolate parse errors per project (log + notify, don't block others).
      3. On error: catch and notify slack_channel.
      4. Clear tick_id at end.
    """
    tick_id = new_tick_id()
    set_tick_id(tick_id)

    try:
        projects = discover_projects(projects_dir)
        log.info(f"auto_tick: discovered {len(projects)} projects")

        projects_dir = Path(projects_dir)
        lock_dir = Path(lock_dir)
        state_dir = Path(state_dir)

        # Prepare checkpoint and ready-for-review directories
        checkpoint_dir = state_dir / "pipeline_checkpoints"
        ready_dir = state_dir / "ready_for_review"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        ready_dir.mkdir(parents=True, exist_ok=True)

        for project_dir in projects:
            project_name = project_dir.name
            todos_md = project_dir / "TODOS.md"

            try:
                # Create State object for this project
                state = State(
                    project=project_name,
                    lock_dir=lock_dir,
                    checkpoint_dir=checkpoint_dir,
                    ready_dir=ready_dir,
                )

                # Check if locked
                if state.is_locked():
                    log.debug(f"project {project_name}: locked, skipping")
                    continue

                # Read TODOS.md
                if not todos_md.exists():
                    log.debug(f"project {project_name}: no TODOS.md, skipping")
                    continue

                content = todos_md.read_text(encoding="utf-8")
                current_hash = _compute_hash(content)
                saved_hash = state.get_saved_hash()

                # Check for changes
                if saved_hash == current_hash:
                    log.debug(
                        f"project {project_name}: no changes "
                        f"(hash={current_hash[:8]}...), skipping"
                    )
                    continue

                log.info(
                    f"project {project_name}: TODOS.md changed, selecting eligible TODO"
                )

                # Save the new hash
                state.save_hash(current_hash)

                # Select eligible TODO
                selected_todo, parse_error = select_for_project(todos_md, state_dir)

                if parse_error:
                    msg = f"project {project_name}: parse error: {parse_error}"
                    log.error(msg)
                    notify_fn(msg)
                    if slack_channel:
                        notify(slack_channel, msg)
                    continue

                if selected_todo:
                    log.info(
                        f"project {project_name}: selected TODO-{selected_todo.todo_id} "
                        f"({selected_todo.title})"
                    )
                    on_selected(project_name, selected_todo.todo_id)
                else:
                    log.debug(
                        f"project {project_name}: no eligible TODOs available"
                    )

            except Exception as e:
                msg = f"project {project_name}: unexpected error: {e}"
                log.error(msg, exc_info=True)
                notify_fn(msg)
                if slack_channel:
                    notify(slack_channel, msg)
                continue

    except Exception as e:
        msg = f"auto_tick failed: {e}"
        log.error(msg, exc_info=True)
        notify_fn(msg)
        if slack_channel:
            notify(slack_channel, msg)

    finally:
        set_tick_id(None)


def run_phase(*, todo_id, tick_id, phase_key, project_slug, **kw):
    """Thin shim — delegates to phases.run so regression tests stay green."""
    from .phases import run as phases_run

    return phases_run(
        state_dir=kw.get("state_dir"),
        todo_id=f"TODO-{todo_id}" if isinstance(todo_id, int) else todo_id,
        tick_id=tick_id,
        phase_key=phase_key,
        project_slug=project_slug,
        **{k: v for k, v in kw.items() if k != "state_dir"},
    )
