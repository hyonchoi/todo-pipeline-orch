"""Pure-Python implementation of todos-manager skill structural logic.

Serves as both test oracle and golden-file generator.
"""

import fcntl
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


def scan_ids(text: str) -> set[int]:
    """Return all TODO-N IDs found in markdown text."""
    return {int(m) for m in re.findall(r"TODO-(\d+)", text)}


def compute_next_id(todos_path: Path, archive_path: Path) -> int:
    """Compute next sequential ID from TODOS.md and TODOS-archive.md."""
    all_ids: set[int] = set()
    if todos_path.exists():
        all_ids |= scan_ids(todos_path.read_text(encoding="utf-8"))
    if archive_path.exists():
        all_ids |= scan_ids(archive_path.read_text(encoding="utf-8"))
    if not all_ids:
        return 1
    return max(all_ids) + 1


NEXT_TODO_ID_LINE_RE = re.compile(
    r"^(?:>[ \t]+-[ \t]+)?NEXT_TODO_ID:(?P<value>[^\r\n]*)$"
)

SECTION_HEADINGS = ("## Metadata", "## Entry Schema", "## Entries")


@dataclass(frozen=True)
class TodoDocumentSections:
    metadata: str
    schema: str
    entries: str
    diagnostics: list[str]
    newline: str
    has_canonical_layout: bool


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
    if start < len(lines) and NEXT_TODO_ID_LINE_RE.fullmatch(lines[start].rstrip("\r\n")):
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


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_without_ending(line: str) -> str:
    return line.rstrip("\r\n")


def _metadata_line_indexes(lines: list[str]) -> list[int]:
    return [
        index
        for index, line in enumerate(lines)
        if NEXT_TODO_ID_LINE_RE.fullmatch(_line_without_ending(line))
    ]


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


def parse_todos_document_sections(text: str) -> TodoDocumentSections:
    lines = text.splitlines(keepends=True)
    newline = _detect_newline(text)
    spans = _section_spans(lines)
    diagnostics: list[str] = []
    heading_positions = [
        _line_without_ending(line)
        for line in lines
        if _line_without_ending(line) in SECTION_HEADINGS
    ]
    has_canonical_layout = heading_positions == list(SECTION_HEADINGS)
    if not has_canonical_layout:
        diagnostics.append("TODOS.md must contain ## Metadata, ## Entry Schema, and ## Entries in that order")

    def section_text(heading: str) -> str:
        if heading not in spans:
            return ""
        start, end = spans[heading]
        chunk = lines[start:end]
        while chunk and not chunk[0].strip():
            chunk = chunk[1:]
        while chunk and not chunk[-1].strip():
            chunk = chunk[:-1]
        return "".join(chunk)

    metadata_indexes = _metadata_line_indexes(lines)
    metadata_span = spans.get("## Metadata")
    misplaced_indexes = []
    if metadata_span is not None:
        start, end = metadata_span
        misplaced_indexes = [
            index for index in metadata_indexes if not (start <= index < end)
        ]
    elif metadata_indexes:
        misplaced_indexes = metadata_indexes
    if misplaced_indexes:
        diagnostics.append("NEXT_TODO_ID appears outside ## Metadata")
    if len(metadata_indexes) > 1:
        diagnostics.append("NEXT_TODO_ID is duplicated")

    return TodoDocumentSections(
        metadata=section_text("## Metadata"),
        schema=section_text("## Entry Schema"),
        entries=section_text("## Entries"),
        diagnostics=diagnostics,
        newline=newline,
        has_canonical_layout=has_canonical_layout,
    )


def _repair_embedded_metadata(lines: list[str]) -> None:
    marker = "> **Format rules (enforced by `todos-manager` skill):**"
    for index in _preamble_line_indexes(lines):
        line = lines[index]
        stripped = line.rstrip("\r\n")
        if stripped.startswith("> **Format rules") and "NEXT_TODO_ID" in stripped:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = marker + ending


def read_next_todo_id(text: str) -> tuple[int | None, list[str]]:
    """Read and validate the tracked next TODO ID from the metadata section."""
    lines = text.splitlines(keepends=True)
    sections = parse_todos_document_sections(text)
    metadata_indexes = _metadata_line_indexes(lines)
    issues = list(sections.diagnostics)
    if not metadata_indexes:
        issues.append("NEXT_TODO_ID is missing from ## Metadata")
        return None, issues
    if len(metadata_indexes) > 1:
        return None, issues
    metadata_lines = sections.metadata.splitlines()
    metadata_matches = [
        line
        for line in metadata_lines
        if NEXT_TODO_ID_LINE_RE.fullmatch(line.rstrip("\r\n"))
    ]
    if len(metadata_matches) != 1 or issues:
        return None, issues
    match = NEXT_TODO_ID_LINE_RE.fullmatch(metadata_matches[0].rstrip("\r\n"))
    assert match is not None
    raw = match.group("value").strip(" \t")
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        return None, [f"NEXT_TODO_ID must be a positive base-10 integer, got {raw!r}"]
    return int(raw), []


def compute_scan_next_id(todos_path: Path, archive_path: Path) -> int:
    """Return the scan-derived next TODO ID."""
    return compute_next_id(todos_path, archive_path)


def _sectioned_schema_from_legacy(lines: list[str]) -> list[str]:
    preamble_indexes = set(_preamble_line_indexes(lines))
    return [
        line
        for index, line in enumerate(lines)
        if index in preamble_indexes
        and not NEXT_TODO_ID_LINE_RE.fullmatch(_line_without_ending(line))
    ]


def _sectioned_entries_from_legacy(lines: list[str]) -> list[str]:
    preamble_indexes = set(_preamble_line_indexes(lines))
    entry_lines: list[str] = []
    in_entries = False
    for index, line in enumerate(lines):
        stripped = _line_without_ending(line)
        if stripped == "# TODOS" or index in preamble_indexes:
            continue
        if NEXT_TODO_ID_LINE_RE.fullmatch(stripped):
            continue
        if ENTRY_HEADER_RE.match(stripped):
            in_entries = True
        if in_entries:
            entry_lines.append(line)
    while entry_lines and not entry_lines[0].strip():
        entry_lines = entry_lines[1:]
    while entry_lines and not entry_lines[-1].strip():
        entry_lines = entry_lines[:-1]
    return entry_lines


def _ensure_heading(lines: list[str], newline: str) -> list[str]:
    if any(_line_without_ending(line) == "# TODOS" for line in lines):
        return ["# TODOS" + newline]
    return ["# TODOS" + newline]


def replace_next_todo_id_line(text: str, next_id: int) -> str:
    """Replace tracked metadata and normalize the document to three sections."""
    newline = _detect_newline(text)
    lines = text.splitlines(keepends=True)
    _repair_embedded_metadata(lines)
    sections = parse_todos_document_sections("".join(lines))
    if sections.has_canonical_layout:
        schema_lines = sections.schema.splitlines(keepends=True)
        entries_lines = sections.entries.splitlines(keepends=True)
    else:
        schema_lines = _sectioned_schema_from_legacy(lines)
        entries_lines = _sectioned_entries_from_legacy(lines)

    def clean(lines_to_clean: list[str]) -> list[str]:
        return [
            line
            for line in lines_to_clean
            if not NEXT_TODO_ID_LINE_RE.fullmatch(_line_without_ending(line))
        ]

    schema_lines = clean(schema_lines)
    entries_lines = clean(entries_lines)
    output: list[str] = [
        "# TODOS" + newline,
        newline,
        "## Metadata" + newline,
        newline,
        f"NEXT_TODO_ID: {next_id}" + newline,
        newline,
        "## Entry Schema" + newline,
        newline,
    ]
    output.extend(schema_lines)
    if output[-1].strip():
        output.append(newline)
    output.extend([newline, "## Entries" + newline])
    if entries_lines:
        output.append(newline)
        output.extend(entries_lines)
        if not output[-1].endswith(("\n", "\r\n")):
            output.append(newline)
    return "".join(output)


@contextmanager
def _todo_lock(lock_path: Path) -> Iterator[None]:
    """Hold the sidecar lock used to serialize TODO updates."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _before_todo_lock() -> None:
    """Test synchronization point before an update attempts the sidecar lock."""


def _before_todos_replace() -> None:
    """Test synchronization point before committing an updated TODO file."""


def atomic_update_todos(todos_path: Path, transform: Callable[[str], str]) -> None:
    """Apply a TODO transform under an exclusive lock using atomic replacement."""
    lock_path = todos_path.with_suffix(todos_path.suffix + ".lock")
    _before_todo_lock()
    with _todo_lock(lock_path):
        original = todos_path.read_text(encoding="utf-8")
        updated = transform(original)
        fd, tmp_name = tempfile.mkstemp(dir=todos_path.parent, prefix=".TODOS.", text=True)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(updated)
                tmp.flush()
                os.fsync(tmp.fileno())
            _before_todos_replace()
            os.replace(tmp_path, todos_path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise


def assign_next_todo_id(
    project_dir: Path, entry_builder: Callable[[int], str]
) -> tuple[int, list[str]]:
    """Assign the next TODO ID and append its entry in one atomic update."""
    todos_path = project_dir / "TODOS.md"
    archive_path = project_dir / "TODOS-archive.md"
    messages: list[str] = []
    assigned = 1

    def transform(text: str) -> str:
        nonlocal assigned, messages
        tracked, issues = read_next_todo_id(text)
        scanned_next = compute_scan_next_id(todos_path, archive_path)
        tracked_is_stale = tracked is not None and tracked != scanned_next
        if tracked is None or issues or tracked_is_stale:
            assigned = scanned_next
            messages.append(f"add: corrected NEXT_TODO_ID from {tracked} to {scanned_next}")
        else:
            assigned = tracked
        messages.extend(issues)
        updated = replace_next_todo_id_line(text, assigned + 1)
        entry = entry_builder(assigned).rstrip()
        sections = parse_todos_document_sections(updated)
        if sections.has_canonical_layout:
            existing_entries = sections.entries.rstrip()
            combined_entries = f"{existing_entries}\n\n{entry}" if existing_entries else entry
            return _replace_entries_section(updated, combined_entries)
        return updated.rstrip() + "\n\n" + entry + "\n"

    atomic_update_todos(todos_path, transform)
    return assigned, messages


def reconcile_next_todo_id(project_dir: Path, mode: str) -> tuple[int, list[str]]:
    """Repair tracked metadata so it agrees with the scan-derived ID."""
    todos_path = project_dir / "TODOS.md"
    archive_path = project_dir / "TODOS-archive.md"
    messages: list[str] = []
    reconciled_id = 1

    def transform(text: str) -> str:
        nonlocal reconciled_id
        tracked, issues = read_next_todo_id(text)
        reconciled_id = compute_scan_next_id(todos_path, archive_path)
        if tracked == reconciled_id and not issues:
            return text
        messages.extend(issues)
        if tracked is None:
            messages.append(f"{mode}: inserted NEXT_TODO_ID: {reconciled_id}")
        else:
            messages.append(
                f"{mode}: corrected NEXT_TODO_ID from {tracked} to {reconciled_id}"
            )
        return replace_next_todo_id_line(text, reconciled_id)

    atomic_update_todos(todos_path, transform)
    return reconciled_id, messages


COUNTER_FILE = ".hermes/todo_id_counter"


def read_counter_cache(project_dir: Path) -> int | None:
    """Read the counter cache file. Returns None if not found."""
    counter = project_dir / COUNTER_FILE
    if not counter.exists():
        return None
    try:
        return int(counter.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def counter_matches_scan(project_dir: Path) -> bool:
    """Check if counter cache matches max scanned ID across both files."""
    todos = project_dir / "TODOS.md"
    archive = project_dir / "TODOS-archive.md"
    all_ids: set[int] = set()
    if todos.exists():
        all_ids |= scan_ids(todos.read_text(encoding="utf-8"))
    if archive.exists():
        all_ids |= scan_ids(archive.read_text(encoding="utf-8"))
    if not all_ids:
        return read_counter_cache(project_dir) in (None, 0)
    max_id = max(all_ids)
    cached = read_counter_cache(project_dir)
    return cached == max_id


VALID_STATUSES = {"[ ]", "[→]", "[x]", "[~]"}

ENTRY_HEADER_RE = re.compile(
    r"^-\s+(\[[ →x~]\])\s+(?:\*\*)?TODO-(\d+):\s+([^*]+?)(?:\*\*)?(?:\s+—\s+(.+?))?$"
)

FIELD_RE = re.compile(
    r"^\s+-\s+\*\*([^*:]+?)(?::)?\*\*\s*(.+?)(?:\s*)?$"
)


def _entries_text(text: str) -> str:
    sections = parse_todos_document_sections(text)
    return sections.entries if sections.has_canonical_layout else text


def parse_entries(text: str) -> list[dict]:
    """Parse TODO entries under ## Entries from TODOS.md markdown text.

    Returns a list of dicts with keys: id, status, title, summary, fields.
    """
    lines = _entries_text(text).split("\n")
    entries: list[dict] = []
    current: dict | None = None

    for line in lines:
        header_match = ENTRY_HEADER_RE.match(line)
        if header_match:
            if current:
                entries.append(current)
            status, id_str, title, summary = header_match.groups()
            current = {
                "id": int(id_str),
                "status": status,
                "title": title.strip(),
                "summary": (summary.strip() if summary else ""),
                "fields": {},
            }
            continue

        if current is not None:
            field_match = FIELD_RE.match(line)
            if field_match:
                field_name, field_value = field_match.groups()
                current["fields"][field_name] = field_value.strip()

    if current:
        entries.append(current)

    return entries


REQUIRED_FIELDS = {"What", "Why", "Decisions"}


def validate_entry(entry: dict) -> list[str]:
    """Validate a single parsed entry against schema. Returns list of issues."""
    issues: list[str] = []

    if entry.get("status") not in VALID_STATUSES:
        issues.append(f"TODO-{entry['id']}: Invalid status marker '{entry.get('status')}' — expected one of {VALID_STATUSES}")

    for field in REQUIRED_FIELDS:
        if field not in entry.get("fields", {}):
            issues.append(f"TODO-{entry['id']}: Missing required field **{field}:**")

    return issues


def validate_all_entries(text: str) -> list[dict]:
    """Validate all entries in TODOS.md text. Returns list of {id, issues} dicts."""
    entries = parse_entries(text)
    return [{"id": e["id"], "issues": validate_entry(e)} for e in entries]


def validate_dependency_refs(text: str) -> list[str]:
    """Find dependency references pointing to non-existent IDs."""
    entries = parse_entries(text)
    # Get all IDs from actual entry headers, not field content
    all_ids = {e["id"] for e in entries}
    broken: list[str] = []

    for entry in entries:
        deps = entry["fields"].get("Depends on", "")
        if deps:
            ref_ids = scan_ids(deps)
            for ref_id in ref_ids:
                if ref_id not in all_ids:
                    broken.append(f"TODO-{entry['id']}: Dependency TODO-{ref_id} does not exist")

    return broken


def find_completed_entries(text: str) -> list[dict]:
    """Find all [x] (done) entries in TODOS.md text."""
    entries = parse_entries(text)
    return [e for e in entries if e["status"] == "[x]"]


def extract_entry_blocks(text: str) -> list[str]:
    """Extract raw markdown blocks for entries under ## Entries.

    Returns a list of strings, each containing the header line and sub-bullets
    for one entry.
    """
    lines = _entries_text(text).split("\n")
    blocks: list[str] = []
    current_block: list[str] = []

    for line in lines:
        if ENTRY_HEADER_RE.match(line):
            if current_block:
                blocks.append("\n".join(current_block))
            current_block = [line]
        elif current_block and (line.strip().startswith("- **") or (line.strip() and line[0] in (" ", "\t"))):
            current_block.append(line)
        elif current_block and line.strip() == "":
            current_block.append(line)
        elif current_block:
            blocks.append("\n".join(current_block))
            current_block = []

    if current_block:
        blocks.append("\n".join(current_block))

    return blocks


def _replace_entries_section(text: str, entries_text: str) -> str:
    sections = parse_todos_document_sections(text)
    if not sections.has_canonical_layout:
        return text
    newline = sections.newline
    lines = text.splitlines(keepends=True)
    spans = _section_spans(lines)
    start, end = spans["## Entries"]
    replacement = [newline]
    if entries_text.strip():
        replacement.extend(entries_text.rstrip().splitlines(keepends=True))
        if not replacement[-1].endswith(("\n", "\r\n")):
            replacement.append(newline)
    return "".join(lines[:start] + replacement + lines[end:])


def simulate_archive(todos_text: str, archive_text: str) -> tuple[str, str]:
    """Simulate moving completed entries from TODOS.md to TODOS-archive.md.

    Returns (new_todos_text, new_archive_text).
    """
    completed = find_completed_entries(todos_text)
    if not completed:
        return todos_text, archive_text

    completed_ids = {e["id"] for e in completed}

    # Build new TODOS.md by removing completed entries
    blocks = extract_entry_blocks(todos_text)
    remaining_blocks = []
    archived_blocks = []

    for block in blocks:
        block_ids = scan_ids(block)
        if block_ids & completed_ids:
            archived_blocks.append(block)
        else:
            remaining_blocks.append(block)

    if parse_todos_document_sections(todos_text).has_canonical_layout:
        new_todos = _replace_entries_section(todos_text, "\n\n".join(remaining_blocks))
    else:
        first_entry_pos = -1
        for line in todos_text.split("\n"):
            if ENTRY_HEADER_RE.match(line):
                break
            first_entry_pos += len(line) + 1
        if first_entry_pos == -1 or first_entry_pos >= len(todos_text):
            new_todos = todos_text
        else:
            header = todos_text[:first_entry_pos]
            new_todos = header + "\n".join(remaining_blocks)

    # Append to archive
    if not archive_text.strip():
        archive_header = "# TODOS Archive\n\nCompleted TODOs, archived via `todos-manager --archive`.\n\n"
    else:
        archive_header = archive_text

    new_archive = archive_header
    if archived_blocks:
        new_archive += "\n".join(archived_blocks) + "\n"

    return new_todos, new_archive
