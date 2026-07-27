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
    r"^(?:>[ \t]+-[ \t]+)?NEXT_TODO_ID:[ \t]*([1-9][0-9]*)[ \t]*$"
)
SECTION_HEADINGS = ("## Metadata", "## Entry Schema", "## Entries")


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


def _line_without_ending(line: str) -> str:
    return line.rstrip("\r\n")


def _section_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    headings: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        stripped = _line_without_ending(line)
        if stripped in SECTION_HEADINGS:
            headings.append((stripped, index))
    spans: dict[str, tuple[int, int]] = {}
    for position, (heading, index) in enumerate(headings):
        end = headings[position + 1][1] if position + 1 < len(headings) else len(lines)
        spans[heading] = (index + 1, end)
    return spans


def _canonical_section_spans(lines: list[str]) -> dict[str, tuple[int, int]] | None:
    heading_positions = [
        _line_without_ending(line)
        for line in lines
        if _line_without_ending(line) in SECTION_HEADINGS
    ]
    if heading_positions != list(SECTION_HEADINGS):
        return None
    return _section_spans(lines)


def _read_tracked_next_todo_id(todos_text: str) -> int | None:
    lines = todos_text.splitlines(keepends=True)
    spans = _canonical_section_spans(lines)
    if spans is None:
        return None
    metadata_span = spans.get("## Metadata")
    if metadata_span is None:
        return None
    metadata_start, metadata_end = metadata_span
    all_metadata_indexes = [
        index
        for index, line in enumerate(lines)
        if NEXT_TODO_ID_METADATA_RE.fullmatch(_line_without_ending(line))
    ]
    if len(all_metadata_indexes) != 1:
        return None
    metadata_index = all_metadata_indexes[0]
    if not (metadata_start <= metadata_index < metadata_end):
        return None
    match = NEXT_TODO_ID_RE.fullmatch(_line_without_ending(lines[metadata_index]))
    if match is None:
        return None
    return int(match.group(1))


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
    todos_lines = todos_content.splitlines(keepends=True)
    sections = _canonical_section_spans(todos_lines)
    if sections is None:
        active_todos_content = todos_content
    else:
        entries_start, entries_end = sections["## Entries"]
        active_todos_content = "".join(todos_lines[entries_start:entries_end])
    scanned_ids = [
        int(m)
        for m in TODO_ID_RE.findall(active_todos_content)
        + TODO_ID_RE.findall(archive_content)
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
