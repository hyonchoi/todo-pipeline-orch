"""Counter recovery — scan TODOS.md for max TODO-N and initialize .hermes/todo_id_counter."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

COUNTER_FILE = ".hermes/todo_id_counter"
TODO_ID_RE = re.compile(r"\bTODO-(\d+)\b")
NEXT_TODO_ID_METADATA_RE = re.compile(
    r"^(?:>[ \t]+-[ \t]+)?NEXT_TODO_ID:[^\r\n]*\r?$", re.MULTILINE
)
NEXT_TODO_ID_RE = re.compile(
    r"^(?:>[ \t]+-[ \t]+)?NEXT_TODO_ID:[ \t]*([1-9][0-9]*)[ \t]*\r?$",
    re.MULTILINE,
)


def _preamble_line_indexes(lines: list[str]) -> range:
    """Return the contiguous blockquote preamble immediately after ``# TODOS``."""
    heading = next(
        (index for index, line in enumerate(lines) if line.rstrip("\r\n") == "# TODOS"),
        None,
    )
    if heading is None:
        return range(0)

    start = heading + 1
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and NEXT_TODO_ID_METADATA_RE.fullmatch(lines[start].rstrip("\r\n")):
        start += 1
        while start < len(lines) and not lines[start].strip():
            start += 1
    if start == len(lines) or not lines[start].startswith(">"):
        return range(0)

    end = start
    while end < len(lines):
        line = lines[end]
        if line.startswith(">") or not line.strip():
            end += 1
            continue
        break
    return range(start, end)


def _read_tracked_next_todo_id(todos_text: str) -> int | None:
    lines = todos_text.splitlines(keepends=True)
    metadata_lines = []
    matches = []
    for line in lines:
        line = line.rstrip("\r\n")
        if NEXT_TODO_ID_METADATA_RE.fullmatch(line):
            metadata_lines.append(line)
        match = NEXT_TODO_ID_RE.fullmatch(line)
        if match is not None:
            matches.append(match.group(1))
    if len(metadata_lines) != 1 or len(matches) != 1:
        return None
    return int(matches[0])


def recover_counter(project_dir: Path) -> int:
    """Scan TODOS.md for TODO-N entries and initialize/update the counter file.

    Reads project_dir / "TODOS.md" and uses its tracked NEXT_TODO_ID when
    available, writing NEXT_TODO_ID - 1 to project_dir / ".hermes" /
    "todo_id_counter". Legacy files without valid tracked state fall back to
    the maximum N in TODO-N patterns and preserve a higher existing counter.

    During legacy recovery only, if the counter file exists and has a higher
    value than the scanned max (e.g., completed TODOs were removed), the
    existing counter is preserved. This prevents ID resurrection.

    Args:
        project_dir: Path to the project root (containing TODOS.md).

    Returns:
        The counter value after recovery.

    Raises:
        FileNotFoundError: If TODOS.md doesn't exist in the project directory.
    """
    todos_path = project_dir / "TODOS.md"
    if not todos_path.exists():
        raise FileNotFoundError(f"TODOS.md not found in {project_dir}")

    counter_path = project_dir / COUNTER_FILE
    todos_content = todos_path.read_text()
    archive_path = project_dir / "TODOS-archive.md"
    archive_content = (
        archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
    )
    scanned_ids = [
        int(m)
        for m in TODO_ID_RE.findall(todos_content) + TODO_ID_RE.findall(archive_content)
    ]
    scanned_max = max(scanned_ids) if scanned_ids else 0
    scanned_next = scanned_max + 1
    tracked_next = _read_tracked_next_todo_id(todos_content)
    if tracked_next == scanned_next:
        result = tracked_next - 1
    else:
        # Legacy files without tracked state use the scan and never decrease
        # an existing counter, preventing ID resurrection.
        existing_value = 0
        if counter_path.exists():
            try:
                existing_value = int(counter_path.read_text().strip())
            except (ValueError, OSError):
                # Corrupt or unreadable counter — treat as 0
                existing_value = 0
        result = max(existing_value, scanned_max)

    # Write the counter file atomically (create .hermes/ if needed)
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file + rename, so a crash mid-write leaves a partial
    # file that the reader treats as 0 rather than a corrupted counter.
    fd, tmp_path = tempfile.mkstemp(dir=counter_path.parent, prefix=".todo_id_counter.")
    try:
        os.write(fd, str(result).encode())
        os.close(fd)
        os.replace(tmp_path, str(counter_path))
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return result
