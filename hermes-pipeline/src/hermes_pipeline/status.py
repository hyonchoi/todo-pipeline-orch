"""Lane F.2: Pending-records table printer.

Implements:
  TF.2: status.py — pending-records table printer (T6/F2)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .state import State, ReadyForReview


@dataclass
class StatusRow:
    """A row in the pending-records status table."""

    project: str
    todo_id: int
    branch: str
    pr_url: str
    merge_status: str
    age: str


def _age(iso_timestamp: str) -> str:
    """
    Convert ISO 8601 timestamp to human-readable age.

    Args:
        iso_timestamp: ISO 8601 timestamp string (e.g., "2025-06-11T12:34:56Z").

    Returns:
        Human-readable age (e.g., "5m", "2h", "1d").
    """
    if not iso_timestamp:
        return "unknown"

    try:
        # Parse ISO 8601 timestamp
        if iso_timestamp.endswith("Z"):
            iso_timestamp = iso_timestamp[:-1] + "+00:00"
        created_dt = datetime.fromisoformat(iso_timestamp)
        now = datetime.now(timezone.utc)
        delta = now - created_dt

        total_seconds = int(delta.total_seconds())

        if total_seconds < 60:
            return f"{total_seconds}s"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"{minutes}m"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours}h"
        else:
            days = total_seconds // 86400
            return f"{days}d"
    except Exception:
        return "unknown"


def collect_pending(projects_dir: Path | str, lock_dir: Path | str) -> list[StatusRow]:
    """
    Scan all projects for ready_for_review pending records.

    Args:
        projects_dir: Base directory containing projects.
        lock_dir: Directory for lock files (also contains .todos_hash files).

    Returns:
        List of StatusRow objects, one per ready_for_review pending record.
    """
    rows = []
    projects_dir = Path(projects_dir)
    lock_dir = Path(lock_dir)

    # Discover projects with TODOS.md
    if not projects_dir.exists():
        return rows

    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue

        todos_md = project_dir / "TODOS.md"
        if not todos_md.exists():
            continue

        project_name = project_dir.name

        # Create State object to access ready-for-review records
        checkpoint_dir = lock_dir.parent / "pipeline_checkpoints"
        ready_dir = lock_dir.parent / "ready_for_review"

        state = State(
            project=project_name,
            lock_dir=lock_dir,
            checkpoint_dir=checkpoint_dir,
            ready_dir=ready_dir,
        )

        # Scan ready-for-review records for this project
        if not ready_dir.exists():
            continue

        for record_file in ready_dir.glob(f"{project_name}_*.json"):
            try:
                json_str = record_file.read_text(encoding="utf-8")
                rec = ReadyForReview.from_json(json_str)

                # Only include pending and failed records
                if rec.merge_status not in ("pending", "failed"):
                    continue

                age_str = _age(rec.created_at)
                row = StatusRow(
                    project=rec.project,
                    todo_id=rec.todo_id,
                    branch=rec.branch,
                    pr_url=rec.pr_url,
                    merge_status=rec.merge_status,
                    age=age_str,
                )
                rows.append(row)
            except Exception as e:
                # Log and skip malformed records
                continue

    # Sort by project, then by todo_id
    rows.sort(key=lambda r: (r.project, r.todo_id))
    return rows


def format_table(rows: list[StatusRow]) -> str:
    """
    Pretty-print pending records as a table.

    Args:
        rows: List of StatusRow objects.

    Returns:
        Formatted table string with headers.
    """
    if not rows:
        return "No pending records.\n"

    # Column widths
    col_project = max(len("PROJECT"), max(len(r.project) for r in rows))
    col_todo = len("TODO")
    col_branch = max(len("BRANCH"), max(len(r.branch) for r in rows))
    col_pr = max(len("PR"), max(len(r.pr_url[:30]) for r in rows))  # truncate PR URLs
    col_status = max(len("STATUS"), max(len(r.merge_status) for r in rows))
    col_age = max(len("AGE"), max(len(r.age) for r in rows))

    # Build header
    header = (
        f"{r'PROJECT':<{col_project}} | "
        f"{r'TODO':<{col_todo}} | "
        f"{r'BRANCH':<{col_branch}} | "
        f"{r'PR':<{col_pr}} | "
        f"{r'STATUS':<{col_status}} | "
        f"{r'AGE':<{col_age}}"
    )

    # Separator
    sep = "-" * len(header)

    # Rows
    lines = [header, sep]
    for row in rows:
        pr_display = row.pr_url[:30] + "..." if len(row.pr_url) > 30 else row.pr_url
        line = (
            f"{row.project:<{col_project}} | "
            f"{row.todo_id:<{col_todo}} | "
            f"{row.branch:<{col_branch}} | "
            f"{pr_display:<{col_pr}} | "
            f"{row.merge_status:<{col_status}} | "
            f"{row.age:<{col_age}}"
        )
        lines.append(line)

    return "\n".join(lines) + "\n"
