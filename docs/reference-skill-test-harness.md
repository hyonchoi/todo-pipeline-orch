# Reference: Skill Test Harness API

Complete API documentation for the todos-manager skill test environment.

## Overview

The skill test environment (`tests/skill-test-environment/`) provides pure-Python implementations of todos-manager skill logic, golden-file assertion loaders, and pytest fixtures. It enables zero-token structural validation of TODOS.md and TODOS-archive.md without running the skill itself.

All fixtures are prefixed with `skill_` to avoid collisions with the parent `tests/conftest.py`.

## Module: `skill_logic.py`

Pure-Python implementations of skill schema rules. These functions serve as both the test oracle and the source of truth for TODOS.md structure.

### Constants

```python
COUNTER_FILE = ".hermes/todo_id_counter"
```
Path to the counter cache file, relative to a project directory.

```python
VALID_STATUSES = {"[ ]", "[→]", "[x]", "[~]"}
```
The four allowed status markers for TODO entries:
- `[ ]` — Open
- `[→]` — In progress
- `[x]` — Completed
- `[~]` — Deferred

```python
REQUIRED_FIELDS = {"What", "Why", "Decisions"}
```
The three mandatory entry fields. All other fields (Pros, Cons, Context, Depends on, Assumptions, Completed, Resolved design) are optional.

### ID Management

#### `scan_ids(text: str) -> set[int]`

Parse all TODO-N IDs from markdown text.

**Args:**
- `text` — Markdown text from TODOS.md or TODOS-archive.md

**Returns:** Set of integer IDs found (e.g., `{1, 2, 3, 4, 6, 7}`)

**Example:**
```python
from tests.skill_test_environment.skill_logic import scan_ids
ids = scan_ids("- [ ] TODO-1: First\n- [ ] TODO-3: Third")
assert ids == {1, 3}
```

#### `compute_next_id(todos_path: Path, archive_path: Path) -> int`

Compute the next sequential TODO ID from both TODOS.md and TODOS-archive.md.

**Args:**
- `todos_path` — Path to TODOS.md file
- `archive_path` — Path to TODOS-archive.md file

**Returns:** Next available ID (always `max(all IDs) + 1`, or `1` if no IDs found)

**Example:**
```python
from pathlib import Path
from tests.skill_test_environment.skill_logic import compute_next_id

next_id = compute_next_id(
    Path("project/TODOS.md"),
    Path("project/TODOS-archive.md")
)
# Returns 8 if max ID across both files is 7
```

#### `read_counter_cache(project_dir: Path) -> Optional[int]`

Read the counter cache value from `.hermes/todo_id_counter`.

**Args:**
- `project_dir` — Project directory

**Returns:** Cached counter value, or `None` if file doesn't exist or is malformed

#### `counter_matches_scan(project_dir: Path) -> bool`

Check if the cached counter matches the maximum scanned ID across both files.

**Args:**
- `project_dir` — Project directory

**Returns:** `True` if counter matches `max(scan_ids(TODOS.md + TODOS-archive.md))`

### Entry Parsing

#### `parse_entries(text: str) -> list[dict]`

Parse all TODO entries from TODOS.md markdown text.

**Args:**
- `text` — Markdown text

**Returns:** List of entry dictionaries with keys:
- `id` (int) — The TODO-N number
- `status` (str) — One of `VALID_STATUSES`
- `title` (str) — Entry title (up to the summary separator or end of line)
- `summary` (str) — Optional summary text after `—` separator
- `fields` (dict) — Parsed fields (e.g., `{"What": "...", "Why": "...", "Decisions": "..."}`)

**Entry Structure:**
```
- [x] **TODO-5: Build Feature** — Optional summary text
  - **What:** Task description
  - **Why:** Rationale
  - **Decisions:** Key decisions
  - **Depends on:** TODO-3, TODO-4
```

**Example:**
```python
from tests.skill_test_environment.skill_logic import parse_entries

text = """
- [ ] TODO-1: First Entry
  - **What:** Build something
  - **Why:** Because it's needed
  - **Decisions:** Go with option A
"""

entries = parse_entries(text)
assert len(entries) == 1
assert entries[0]["id"] == 1
assert entries[0]["title"] == "First Entry"
assert entries[0]["fields"]["What"] == "Build something"
```

### Entry Validation

#### `validate_entry(entry: dict) -> list[str]`

Validate a single parsed entry against schema rules.

**Args:**
- `entry` — Dictionary returned by `parse_entries()`

**Returns:** List of issue strings (empty if valid)

**Validation Rules:**
- Status marker must be in `VALID_STATUSES`
- Must have all three required fields: What, Why, Decisions

**Example:**
```python
from tests.skill_test_environment.skill_logic import validate_entry

entry = {
    "id": 1,
    "status": "[ ]",
    "title": "Task",
    "fields": {"What": "...", "Why": "..."}
    # Missing Decisions
}

issues = validate_entry(entry)
assert len(issues) == 1
assert "Missing required field **Decisions:**" in issues[0]
```

#### `validate_all_entries(text: str) -> list[dict]`

Validate all entries in TODOS.md text.

**Args:**
- `text` — Markdown text

**Returns:** List of `{"id": int, "issues": [str]}` dicts

**Example:**
```python
from tests.skill_test_environment.skill_logic import validate_all_entries

text = """- [ ] TODO-1: First
  - **What:** X
  - **Why:** Y
- [ ] TODO-2: Second
  - **What:** X
  # Missing Why and Decisions
"""

validation = validate_all_entries(text)
assert len(validation) == 2
assert len(validation[0]["issues"]) == 0
assert len(validation[1]["issues"]) > 0
```

#### `validate_dependency_refs(text: str) -> list[str]`

Find dependency references pointing to non-existent IDs.

**Args:**
- `text` — Markdown text

**Returns:** List of error strings (e.g., `"TODO-1: Dependency TODO-99 does not exist"`)

**Example:**
```python
from tests.skill_test_environment.skill_logic import validate_dependency_refs

text = """
- [ ] TODO-1: Task A
  - **Depends on:** TODO-2, TODO-3

- [ ] TODO-2: Task B
  - **Depends on:** TODO-5
"""

broken = validate_dependency_refs(text)
# Returns ["TODO-2: Dependency TODO-5 does not exist"]
```

### Archive Operations

#### `find_completed_entries(text: str) -> list[dict]`

Find all `[x]` (completed) entries in TODOS.md.

**Args:**
- `text` — Markdown text

**Returns:** List of entry dictionaries (same format as `parse_entries()`)

#### `extract_entry_blocks(text: str) -> list[str]`

Extract raw markdown text blocks for each entry.

**Args:**
- `text` — Markdown text from TODOS.md

**Returns:** List of markdown strings, each containing an entry header and its sub-bullets

**Example:**
```python
from tests.skill_test_environment.skill_logic import extract_entry_blocks

text = """- [ ] TODO-1: First
  - **What:** X
  - **Why:** Y
  - **Decisions:** Z

- [ ] TODO-2: Second
  - **What:** A
"""

blocks = extract_entry_blocks(text)
assert len(blocks) == 2
assert "TODO-1" in blocks[0]
assert "TODO-2" in blocks[1]
```

#### `simulate_archive(todos_text: str, archive_text: str) -> tuple[str, str]`

Simulate moving completed entries from TODOS.md to TODOS-archive.md.

**Args:**
- `todos_text` — Current TODOS.md content
- `archive_text` — Current TODOS-archive.md content

**Returns:** `(new_todos_text, new_archive_text)` — the files after archiving

**Behavior:**
- Finds all `[x]` entries in TODOS.md
- Removes them from TODOS.md
- Appends them to TODOS-archive.md (newest first)
- Preserves all other content (headers, preambles)

**Example:**
```python
from tests.skill_test_environment.skill_logic import simulate_archive

todos = """- [ ] TODO-1: Open
- [x] TODO-2: Done
"""
archive = ""

new_todos, new_archive = simulate_archive(todos, archive)
assert "TODO-2" not in new_todos
assert "TODO-2" in new_archive
assert "TODO-1" in new_todos
```

## Module: `verify.py`

Golden-file loaders and structural assertion runners.

### Functions

#### `load_golden(path: Path) -> dict`

Load a golden YAML assertion file.

**Args:**
- `path` — Path to a `.yaml` file in `golden/`

**Returns:** Parsed YAML dictionary

**Example:**
```python
from pathlib import Path
from tests.skill_test_environment.verify import load_golden

golden = load_golden(Path("tests/skill-test-environment/golden/add_happy_path.yaml"))
# Returns: {"subcommand": "add", "assertions": [...], ...}
```

#### `run_structural(golden: dict, todos_text: str, archive_text: Optional[str] = None) -> dict`

Run all structural assertions from a golden file against actual file content.

**Args:**
- `golden` — Dictionary returned by `load_golden()`
- `todos_text` — Actual TODOS.md content
- `archive_text` — Actual TODOS-archive.md content (defaults to empty string)

**Returns:** Result dictionary:
```python
{
    "passed": int,
    "failed": int,
    "results": [
        {
            "assertion": dict,  # Original assertion from golden file
            "pass": bool,
            "detail": str  # Explanation of result
        },
        ...
    ]
}
```

**Example:**
```python
from tests.skill_test_environment.verify import load_golden, run_structural

golden = load_golden(Path("golden/add_happy_path.yaml"))
result = run_structural(golden, todos_content, archive_content)

if result["failed"] > 0:
    print(f"Failed: {result['failed']} assertions")
    for r in result["results"]:
        if not r["pass"]:
            print(f"  - {r['detail']}")
```

#### `assert_golden(golden_path: Path, todos_text: str, archive_text: str = "") -> None`

Run golden assertions and raise `AssertionError` on first failure.

**Args:**
- `golden_path` — Path to golden YAML file
- `todos_text` — Actual TODOS.md content
- `archive_text` — Actual TODOS-archive.md content

**Raises:** `AssertionError` with detailed failure messages if any assertion fails

**Example:**
```python
from tests.skill_test_environment.verify import assert_golden

assert_golden(
    Path("golden/add_happy_path.yaml"),
    todos_content,
    archive_content
)
# Raises AssertionError if any assertion fails
```

### Supported Assertions

Golden YAML files declare assertions using one key per assertion. The `verify.py` module supports:

#### File Checks

- `file_exists: TODOS.md` — Content is non-empty
- `file_exists: TODOS-archive.md` — Content is non-empty

#### Regex Checks

- `regex_count: {pattern: "...", count: N}` — Exact count in TODOS.md
- `regex_count: {pattern: "...", count_in_todos: N, count_in_archive: M}` — Split count
- `regex_present: "..."` — Pattern found in TODOS.md

#### Entry Counts

- `total_entries: N` — N entries in TODOS.md
- `entries_in_archive: N` — N entries in TODOS-archive.md
- `total_entries_in_todos_after: N` — N entries in TODOS.md (synonym)
- `total_entries_in_archive_after: N` — N entries in TODOS-archive.md (synonym)

#### ID Checks

- `max_id: N` — Highest ID is N
- `no_duplicate_ids: true` — No ID appears in both files
- `id_range: {min: 1, max: 10}` — All IDs in range [min, max]
- `ids_preserved: [1, 2, 3, 7]` — Exactly these IDs exist

#### Structural Checks

- `preamble_present: true` — TODOS.md has format rules blockquote
- `archive_header_present: true` — TODOS-archive.md has "# TODOS Archive" header

#### Validation Checks

- `all_required_fields_present: true` — No missing What/Why/Decisions
- `all_valid_status_markers: true` — All entries use valid statuses
- `issues_count: N` — Exactly N validation issues detected
- `no_broken_dependency_refs: true` — No TODO-N refs point to non-existent IDs
- `flags_missing_fields: true` — At least one entry missing required fields

## Pytest Fixtures

All fixtures defined in `conftest.py`. Use them by adding parameters to test functions.

### `skill_demo_dir() -> Path`

Path to the demo-project fixture directory containing `TODOS.md`, `TODOS-archive.md`, and `CLAUDE.md`.

```python
def test_something(skill_demo_dir):
    todos_path = skill_demo_dir / "TODOS.md"
    content = todos_path.read_text()
```

### `skill_golden_dir() -> Path`

Path to the golden YAML assertions directory.

```python
def test_something(skill_golden_dir):
    golden_files = list(skill_golden_dir.glob("*.yaml"))
```

### `skill_demo_todos() -> str`

Content of demo-project TODOS.md as a string.

```python
def test_something(skill_demo_todos):
    from tests.skill_test_environment.skill_logic import parse_entries
    entries = parse_entries(skill_demo_todos)
```

### `skill_demo_archive() -> str`

Content of demo-project TODOS-archive.md as a string.

```python
def test_something(skill_demo_todos, skill_demo_archive):
    from tests.skill_test_environment.verify import assert_golden
    assert_golden(
        Path("golden/archive_result.yaml"),
        skill_demo_todos,
        skill_demo_archive
    )
```

## Demo Fixtures

The `demo-project/` subdirectory contains reference TODOS.md and TODOS-archive.md files used by all unit tests.

- `demo-project/TODOS.md` — 6 active entries with diverse field content
- `demo-project/TODOS-archive.md` — 1 archived entry (TODO-5)
- `demo-project/CLAUDE.md` — Minimal project instructions

## Golden Files

The `golden/` subdirectory contains YAML assertion descriptors for skill subcommands.

### `add_happy_path.yaml`

Scenario: Adding a new entry to demo-project TODOS.md results in 7 entries, next ID is 8.

**Preconditions:** Demo TODOS.md with max ID 7

**Assertions:**
- File exists
- 7 total entries (one added)
- Preamble present
- Max ID is 8
- No duplicate IDs

### `archive_result.yaml`

Scenario: Archive operation moves completed entries to TODOS-archive.md.

**Assertions:**
- Entries properly removed from TODOS.md
- Entries appended to TODOS-archive.md
- All IDs preserved
- No entries moved to archive except completed

### `audit_report.yaml`

Scenario: Audit detects validation issues in malformed entries.

**Assertions:**
- Entry counts correct
- Issues detected for missing fields
- No structural corruption

### `init_output.yaml`

Scenario: Initialization creates both files with proper headers.

**Assertions:**
- Both files exist
- Preamble in TODOS.md
- Archive header in TODOS-archive.md

## Usage Examples

### Run All Unit Tests

```bash
uv run pytest tests/skill-test-environment/unit/ -v
```

### Run a Single Test File

```bash
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -v
```

### Run a Specific Test

```bash
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py::TestScanIds::test_scans_all_ids_from_fixture -v
```

### Use in Your Own Test

```python
import pytest
from pathlib import Path
from tests.skill_test_environment.skill_logic import parse_entries, validate_all_entries
from tests.skill_test_environment.verify import assert_golden

def test_my_custom_logic(skill_demo_todos, skill_demo_archive):
    entries = parse_entries(skill_demo_todos)
    assert len(entries) == 6
    
    validation = validate_all_entries(skill_demo_todos)
    assert all(len(v["issues"]) == 0 for v in validation)
    
    assert_golden(
        Path("tests/skill-test-environment/golden/add_happy_path.yaml"),
        skill_demo_todos,
        skill_demo_archive
    )
```

## Design Notes

- **Zero token cost** — No skill invocation, no Hermes calls, no LLM interaction
- **Deterministic** — All functions are pure; same input → same output
- **Fixture-based** — Uses pytest fixtures with `skill_` prefix to avoid collisions
- **Golden-file assertions** — Structural rules encoded in YAML; logic in Python
- **Extensible** — Add new golden files for new scenarios; new assertion types in `verify.py`
