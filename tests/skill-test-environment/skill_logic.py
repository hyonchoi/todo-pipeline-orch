"""Pure-Python implementation of todos-manager skill structural logic.

Serves as both test oracle and golden-file generator.
"""

import re
from pathlib import Path
from typing import Optional


def scan_ids(text: str) -> set[int]:
    """Return all TODO-N IDs found in markdown text."""
    return {int(m) for m in re.findall(r"TODO-(\d+)", text)}


def compute_next_id(todos_path: Path, archive_path: Path) -> int:
    """Compute next sequential ID from TODOS.md and TODOS-archive.md."""
    all_ids: set[int] = set()
    if todos_path.exists():
        all_ids |= scan_ids(todos_path.read_text())
    if archive_path.exists():
        all_ids |= scan_ids(archive_path.read_text())
    if not all_ids:
        return 1
    return max(all_ids) + 1


COUNTER_FILE = ".hermes/todo_id_counter"


def read_counter_cache(project_dir: Path) -> Optional[int]:
    """Read the counter cache file. Returns None if not found."""
    counter = project_dir / COUNTER_FILE
    if not counter.exists():
        return None
    try:
        return int(counter.read_text().strip())
    except ValueError:
        return None


def counter_matches_scan(project_dir: Path) -> bool:
    """Check if counter cache matches max scanned ID across both files."""
    todos = project_dir / "TODOS.md"
    archive = project_dir / "TODOS-archive.md"
    all_ids: set[int] = set()
    if todos.exists():
        all_ids |= scan_ids(todos.read_text())
    if archive.exists():
        all_ids |= scan_ids(archive.read_text())
    if not all_ids:
        return read_counter_cache(project_dir) in (None, 0)
    max_id = max(all_ids)
    cached = read_counter_cache(project_dir)
    return cached == max_id
