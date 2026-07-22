"""Build SelectionContext per tick. Owns stale-marker sweep."""
from __future__ import annotations
import json
import os
import subprocess
import time
from pathlib import Path
from .schema import SelectionContext
from . import store as _store

def _pid_alive(pid: int) -> bool:
    """True iff `pid` is a live process this user can see."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Other user owns it; conservatively assume alive — better to leave
        # the marker in place than re-pick a TODO that might still be running.
        return True
    except OSError:
        return True

def _extract_in_flight_ids(snapshot: dict) -> set[str]:
    """Extract TODO IDs with in-flight tasks from a kanban snapshot.

    Args:
        snapshot: Parsed JSON from `hermes kanban list --json`.

    Returns:
        Set of TODO IDs with tasks in created/ready/running status.
    """
    result_set = set()
    tasks = snapshot if isinstance(snapshot, list) else snapshot.get("tasks", [])
    for task in tasks:
        if task.get("status") not in ("created", "ready", "running"):
            continue
        body = task.get("body", "")
        first_line = body.split("\n")[0]
        try:
            header = json.loads(first_line)
            todo_id = header.get("todo_id")
            if todo_id:
                result_set.add(todo_id)
        except (json.JSONDecodeError, IndexError):
            pass  # Task without JSON header — skip
    return result_set

def _kanban_in_flight_ids(board_slug: str) -> set[str] | None:
    """Extract TODO IDs with in-flight kanban tasks.

    Queries `hermes kanban list --tenant <slug> --json` and parses the
    JSON header in each task's body. Returns None on CLI failure so the
    caller can fall back to file markers.

    Returns:
        Set of TODO IDs with tasks in created/ready/running status,
        or None if the kanban CLI is unavailable.
    """
    snapshot = _fetch_kanban_snapshot(board_slug)
    if snapshot is None:
        return None
    return _extract_in_flight_ids(snapshot)

def build_in_flight(
    state_dir: Path,
    *,
    max_phase_timeout_min: int,
    board_slug: str | None = None,
    snapshot: dict | None = None,
) -> list[str]:
    """Compute in-flight set from kanban state.

    Args:
        state_dir: State directory path (no longer used for fallback).
        max_phase_timeout_min: Max age in minutes before a lock is stale (no longer used).
        board_slug: Kanban board slug for kanban-aware lookup.
        snapshot: Pre-fetched kanban snapshot (from _fetch_kanban_snapshot).
            If provided, used instead of fetching from the CLI.

    Returns:
        Sorted list of TODO IDs currently in flight.
    """
    if snapshot is not None:
        kanban_in_flight = _extract_in_flight_ids(snapshot)
        return sorted(kanban_in_flight)
    if board_slug is not None:
        kanban_in_flight = _kanban_in_flight_ids(board_slug)
        if kanban_in_flight is not None:
            return sorted(kanban_in_flight)
    return []

def _fetch_kanban_snapshot(project_slug: str) -> dict | None:
    """Fetch kanban board state via `hermes kanban list --json`.

    Returns:
        Parsed JSON dict, or None on CLI failure.
    """
    try:
        r = subprocess.run(
            ["hermes", "kanban", "list", "--tenant", project_slug, "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return None

def _kanban_snapshot(project_slug: str) -> dict:
    """Capture current Kanban state via `hermes kanban list`.

    Returns a dict suitable for the selection prompt. On CLI failure,
    returns an error marker dict.
    """
    snapshot = _fetch_kanban_snapshot(project_slug)
    if snapshot is not None:
        return snapshot
    return {"columns": [], "_error": "kanban snapshot unavailable"}

def _recent_decisions(state_dir: Path, n: int) -> list[dict]:
    """Delegated to store.load_recent."""
    return _store.load_recent(state_dir, n=n)

def _read_rejection_counts(state_dir: Path) -> dict[str, int]:
    """Read rejection sidecars and return a mapping of tick_id → rejection_count.

    Used by the risk classifier to check if a project has rejection history.
    Returns an empty dict if the decisions directory doesn't exist.
    """
    from ..gates import REJECTION_SUFFIX

    d = state_dir / "decisions"
    if not d.exists():
        return {}
    counts = {}
    for p in d.iterdir():
        if p.suffix != ".json" or not p.name.endswith(REJECTION_SUFFIX):
            continue
        try:
            data = json.loads(p.read_text())
            tick = data.get("tick_id", "")
            if tick:
                counts[tick] = data.get("rejection_count", 0)
        except (json.JSONDecodeError, OSError):
            pass
    return counts


def build_context(
    *,
    tick_id: str,
    state_dir: Path,
    todos_path: Path,
    project_slug: str,
    max_phase_timeout_min: int,
    recent_n: int = 5,
) -> SelectionContext:
    """Assemble the full SelectionContext for a tick.

    Fetches the kanban snapshot once and reuses it for both in-flight
    detection and the kanban_snapshot field (avoids duplicate CLI calls).
    """
    snapshot = _fetch_kanban_snapshot(project_slug)
    return SelectionContext(
        todos_md=todos_path.read_text(),
        in_flight=build_in_flight(
            state_dir,
            max_phase_timeout_min=max_phase_timeout_min,
            board_slug=project_slug,
            snapshot=snapshot,
        ),
        recent_decisions=_recent_decisions(state_dir, recent_n),
        kanban_snapshot=snapshot if snapshot is not None else {"columns": [], "_error": "kanban snapshot unavailable"},
        project_slug=project_slug,
    )
