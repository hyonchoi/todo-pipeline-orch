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
        all_ids |= scan_ids(todos_path.read_text(encoding="utf-8"))
    if archive_path.exists():
        all_ids |= scan_ids(archive_path.read_text(encoding="utf-8"))
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


def parse_entries(text: str) -> list[dict]:
    """Parse all TODO entries from TODOS.md markdown text.

    Returns a list of dicts with keys: id, status, title, summary, fields.
    """
    lines = text.split("\n")
    entries: list[dict] = []
    current: Optional[dict] = None

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
    """Extract raw markdown text blocks for each entry.

    Returns a list of strings, each containing the header line and sub-bullets
    for one entry.
    """
    lines = text.split("\n")
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

    # Reconstruct TODOS.md header + remaining entries
    # Find the first actual entry (line starting with "- "), skipping blockquote lines
    first_entry_pos = -1
    for line in todos_text.split("\n"):
        if ENTRY_HEADER_RE.match(line):
            break
        first_entry_pos += len(line) + 1  # +1 for the newline
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

