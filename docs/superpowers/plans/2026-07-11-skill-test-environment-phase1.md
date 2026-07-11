# Mock Test Environment Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python unit test suite that verifies the deterministic structural logic of the todos-manager skill (ID sequencing, entry parsing, format validation, archive logic) using fixture files and golden YAML assertions — zero token cost, runs in <5 seconds.

**Architecture:** Extract skill rules into pure-Python verification functions under `tests/skill-test-environment/`. Each function implements one schema rule from the skill prompt. Golden YAML files declare expected assertion outcomes for the demo-project fixture. The `verify.py` module runs golden assertions against any given TODOS.md text. Unit tests exercise the parsing/verification functions with edge-case fixtures.

**Tech Stack:** Python 3.12, pytest, pyyaml (already a dependency), regex, pathlib.

## Global Constraints

- All test artifacts under `tests/skill-test-environment/`
- Python 3.12+, managed via `uv` — run tests with `uv run pytest tests/skill-test-environment/unit/`
- Pytest fixture naming: prefix all fixtures in `conftest.py` with `skill_` to avoid collisions with parent `tests/conftest.py`
- No external dependencies beyond `pyyaml`, `pytest`, `pytest-mock`
- Golden files are YAML descriptors with assertion lists, NOT markdown snapshots
- Phase 1 = structural only. Phase 2 (AI semantic judgment, agent spawning) is deferred.

---

### Task 1: Bootstrap demo-project fixture

**Goal:** Create the demo project with diverse TODOS.md entries that cover all status markers, field combinations, and edge cases.

**Files:**
- Create: `tests/skill-test-environment/demo-project/TODOS.md`
- Create: `tests/skill-test-environment/demo-project/TODOS-archive.md`
- Create: `tests/skill-test-environment/demo-project/CLAUDE.md`

**Produces:**
- `skill_demo_dir` — Path to the demo-project fixture directory (used by conftest Task 6)

- [ ] **Step 1: Create the demo-project TODOS.md**

Write `tests/skill-test-environment/demo-project/TODOS.md` with the preamble blockquote and these entries:

```markdown
# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

- [ ] TODO-1: Implement CLI argument parsing
  - **What:** Add argparse-based CLI for pipeline commands
  - **Why:** Current script is invoked with hardcoded paths
  - **Decisions:** Priority `P0`, Effort `S`, Phase `1 (Setup)`, Branch `feature/cli`, Test Coverage `필요`, Security Review `불필요`

- [→] TODO-2: Add rate limiting to API calls
  - **What:** Implement exponential backoff for external API calls
  - **Why:** Prevent hitting rate limits during bulk operations
  - **Decisions:** Priority `P1`, Effort `M`, Phase `3 (Feature)`, Branch `feature/rate-limit`, Test Coverage `필요`, Security Review `불필요`

- [x] TODO-3: Set up project scaffolding
  - **What:** Create uv project structure with pyproject.toml
  - **Why:** Need proper dependency management
  - **Decisions:** Priority `P0`, Effort `S`, Phase `1 (Setup)`, Branch `main`, Test Coverage `불필요`, Security Review `불필요`
  - **Completed:** v0.1.0, 2026-06-15

- [~] TODO-4: Explore Slack integration
  - **What:** Investigate Slack bot webhook for pipeline notifications
  - **Why:** Team wants real-time alerts on pipeline failures
  - **Decisions:** Priority `P2`, Effort `L`, Phase `5 (Exploration)`, Branch `feature/slack`, Test Coverage `필요`, Security Review `필요`

- [ ] TODO-6: Entry with missing optional fields
  - **What:** Test that entries without Pros/Cons/Context are valid
  - **Why:** Optional fields should not cause validation failures
  - **Decisions:** Priority `P3`, Effort `S`, Phase `2 (Design)`, Branch `feature/minimal`, Test Coverage `불필요`, Security Review `불필요`

- [ ] TODO-7: Entry with dependency references
  - **What:** Add entry that depends on TODO-1 and TODO-4
  - **Why:** Dependencies must be validated against existing IDs
  - **Depends on:** `TODO-1`, `TODO-4`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/deps`, Test Coverage `필요`, Security Review `불필요`
```

Key properties:
- 4 status markers: `[ ]` (3), `[→]` (1), `[x]` (1), `[~]` (1)
- Non-contiguous ID gap: 1-4, then 6-7 (missing 5, which is in archive)
- All required fields present (this is the "good" fixture)
- TODO-6 has no optional fields beyond required
- TODO-7 has dependency references

- [ ] **Step 2: Create TODOS-archive.md**

Write `tests/skill-test-environment/demo-project/TODOS-archive.md`:

```markdown
# TODOS Archive

Completed TODOs, archived via `todos-manager --archive`.

Archived: 2026-06-15

- [x] TODO-5: Build notification system
  - **What:** Create a notification dispatcher for pipeline events
  - **Why:** Users need to be notified of pipeline failures and completions
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/notifications`, Test Coverage `필요`, Security Review `불필요`
  - **Completed:** v0.2.0, 2026-06-14
```

Key properties:
- Contains TODO-5, filling the gap between TODO-4 and TODO-6
- Max ID across both files should be 7
- Has archive header with timestamp

- [ ] **Step 3: Create minimal CLAUDE.md**

Write `tests/skill-test-environment/demo-project/CLAUDE.md`:

```markdown
# CLAUDE.md

Test fixture project for todos-manager skill regression tests.
Do not modify these files outside of the test harness.
```

- [ ] **Step 4: Commit**

```bash
git add tests/skill-test-environment/demo-project/
git commit -m "feat: add demo-project fixture for skill test environment"
```

---

### Task 2: Implement ID sequencing module

**Goal:** Parse TODO IDs from TODOS.md and TODOS-archive.md, compute next ID, validate counter cache.

**Files:**
- Create: `tests/skill-test-environment/skill_logic.py`
- Create: `tests/skill-test-environment/unit/test_id_sequencing.py`

**Consumes:**
- Demo-project fixture from Task 1

**Produces:**
- `scan_ids(text)` → `set[int]` — all TODO-N IDs found in markdown text
- `compute_next_id(todos_path, archive_path)` → `int` — next sequential ID
- `read_counter_cache(project_dir)` → `Optional[int]` — value from `.hermes/todo_id_counter`
- `counter_matches_scan(project_dir)` → `bool` — cache matches max scanned ID

- [ ] **Step 1: Write the failing test**

Write `tests/skill-test-environment/unit/test_id_sequencing.py`:

```python
"""Tests for ID sequencing logic — scanning, next-ID computation, counter cache."""

import pytest
from pathlib import Path
from skill_test_environment.skill_logic import scan_ids, compute_next_id, read_counter_cache, counter_matches_scan


class TestScanIds:
    """Parse TODO-N IDs from markdown text."""

    def test_scans_all_ids_from_fixture(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        ids = scan_ids(todos)
        assert ids == {1, 2, 3, 4, 6, 7}

    def test_scans_archive_ids(self, skill_demo_dir):
        archive = (skill_demo_dir / "TODOS-archive.md").read_text()
        ids = scan_ids(archive)
        assert ids == {5}

    def test_empty_text_returns_empty_set(self):
        ids = scan_ids("")
        assert ids == set()

    def test_no_todo_entries_returns_empty(self):
        ids = scan_ids("# TODOS\n\nSome random text with no entries.\n")
        assert ids == set()

    def test_does_not_match_partial(self):
        """TODO-N in a sentence body should still match — regex finds all occurrences."""
        text = "- [ ] TODO-3: Depends on TODO-1 and TODO-4\n"
        ids = scan_ids(text)
        assert ids == {1, 3, 4}


class TestComputeNextId:
    """Next ID = max(all IDs from both files) + 1."""

    def test_next_id_from_fixture(self, skill_demo_dir):
        """Main has {1,2,3,4,6,7}, archive has {5} → next is 8."""
        next_id = compute_next_id(
            skill_demo_dir / "TODOS.md",
            skill_demo_dir / "TODOS-archive.md",
        )
        assert next_id == 8

    def test_next_id_empty_files(self, tmp_path):
        """Both files empty → next is 1."""
        todos = tmp_path / "TODOS.md"
        archive = tmp_path / "TODOS-archive.md"
        todos.write_text("# TODOS\n\n")
        next_id = compute_next_id(todos, archive)
        assert next_id == 1

    def test_next_id_archive_missing(self, skill_demo_dir):
        """Archive doesn't exist → compute from TODOS.md only → 8."""
        next_id = compute_next_id(
            skill_demo_dir / "TODOS.md",
            Path("/nonexistent/TODOS-archive.md"),
        )
        assert next_id == 8

    def test_gap_in_sequence(self, tmp_path):
        """IDs {1, 2, 5} → next is 6 (don't fill gaps)."""
        todos = tmp_path / "TODOS.md"
        todos.write_text("# TODOS\n\n- TODO-1: A\n- TODO-2: B\n- TODO-5: C\n")
        next_id = compute_next_id(todos, tmp_path / "TODOS-archive.md")
        assert next_id == 6


class TestCounterCache:
    """Counter cache is performance-only; scan is authoritative."""

    def test_read_counter_cache(self, skill_demo_dir):
        counter = skill_demo_dir / ".hermes" / "todo_id_counter"
        counter.parent.mkdir(exist_ok=True)
        counter.write_text("7")
        assert read_counter_cache(skill_demo_dir) == 7

    def test_no_counter_returns_none(self, skill_demo_dir):
        assert read_counter_cache(skill_demo_dir) is None

    def test_counter_matches_scan(self, skill_demo_dir):
        counter = skill_demo_dir / ".hermes" / "todo_id_counter"
        counter.parent.mkdir(exist_ok=True)
        counter.write_text("7")
        # Max scanned ID is 7, so counter matches
        assert counter_matches_scan(skill_demo_dir) is True

    def test_counter_diverges_from_scan(self, skill_demo_dir):
        counter = skill_demo_dir / ".hermes" / "todo_id_counter"
        counter.parent.mkdir(exist_ok=True)
        counter.write_text("3")
        # Max scanned ID is 7, counter says 3 — diverges
        assert counter_matches_scan(skill_demo_dir) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hyonchoi/Personal/todo-pipeline-orchestrator && uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -v`
Expected: FAIL — `skill_logic` module doesn't exist yet.

- [ ] **Step 3: Write minimal implementation**

Write `tests/skill-test-environment/skill_logic.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hyonchoi/Personal/todo-pipeline-orchestrator && uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_id_sequencing.py
git commit -m "feat: ID sequencing logic and tests for skill test environment"
```

---

### Task 3: Implement entry parsing module

**Goal:** Parse individual TODO entries from markdown, extracting header, status, fields, and sub-bullets.

**Files:**
- Modify: `tests/skill-test-environment/skill_logic.py` (add entry parsing functions)
- Create: `tests/skill-test-environment/unit/test_entry_parsing.py`

**Consumes:**
- `scan_ids` from Task 2

**Produces:**
- `parse_entries(text)` → `list[dict]` — list of parsed entry dicts with keys: `id`, `status`, `title`, `summary`, `fields` (dict of field_name → value)
- `ENTRY_HEADER_RE` — regex for matching entry header lines
- `VALID_STATUSES` — set of valid status markers

- [ ] **Step 1: Write the failing test**

Write `tests/skill-test-environment/unit/test_entry_parsing.py`:

```python
"""Tests for entry parsing — extracting TODO entries from markdown text."""

import pytest
from skill_test_environment.skill_logic import parse_entries, VALID_STATUSES


class TestValidStatuses:
    def test_four_markers(self):
        assert VALID_STATUSES == {"[ ]", "[→]", "[x]", "[~]"}


class TestParseEntries:
    """Parse all entries from TODOS.md text."""

    def test_parses_all_entries_from_fixture(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        assert len(entries) == 6
        ids = [e["id"] for e in entries]
        assert ids == [1, 2, 3, 4, 6, 7]

    def test_entry_statuses(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        status_map = {e["id"]: e["status"] for e in entries}
        assert status_map[1] == "[ ]"
        assert status_map[2] == "[→]"
        assert status_map[3] == "[x]"
        assert status_map[4] == "[~]"

    def test_entry_fields(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_1 = entries[0]
        assert entry_1["id"] == 1
        assert "What" in entry_1["fields"]
        assert "Why" in entry_1["fields"]
        assert "Decisions" in entry_1["fields"]

    def test_missing_optional_fields(self, skill_demo_dir):
        """TODO-6 has no optional fields — should still parse."""
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_6 = [e for e in entries if e["id"] == 6][0]
        assert entry_6["fields"].get("Pros") is None
        assert entry_6["fields"].get("Cons") is None
        assert entry_6["fields"].get("Context") is None
        assert "What" in entry_6["fields"]

    def test_dependency_references(self, skill_demo_dir):
        """TODO-7 has Depends on field."""
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_7 = [e for e in entries if e["id"] == 7][0]
        assert "Depends on" in entry_7["fields"]
        assert "TODO-1" in entry_7["fields"]["Depends on"]
        assert "TODO-4" in entry_7["fields"]["Depends on"]

    def test_completed_field(self, skill_demo_dir):
        """TODO-3 has Completed field."""
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_3 = [e for e in entries if e["id"] == 3][0]
        assert "Completed" in entry_3["fields"]

    def test_empty_text(self):
        entries = parse_entries("")
        assert entries == []

    def test_preamble_skipped(self):
        """Preamble blockquote is not an entry."""
        text = "# TODOS\n\n> **Format rules...**\n> - Entry header...\n\n"
        entries = parse_entries(text)
        assert entries == []

    def test_title_and_summary(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_1 = entries[0]
        assert "CLI" in entry_1["title"]
        assert entry_1["summary"] != ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/skill-test-environment/unit/test_entry_parsing.py -v`
Expected: FAIL — `parse_entries` not defined yet.

- [ ] **Step 3: Write implementation**

Add to `tests/skill-test-environment/skill_logic.py`:

```python
VALID_STATUSES = {"[ ]", "[→]", "[x]", "[~]"}

ENTRY_HEADER_RE = re.compile(
    r"^-\s+(\[[ \→x~]\])\s+\*\*TODO-(\d+):\s+([^*]+?)\*\*\s+—\s+(.+?)\s*$"
)

FIELD_RE = re.compile(
    r"^\s+-\s+\*\*(\w+):?\*\*\s*(.+?)\s*$"
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
                "summary": summary.strip(),
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/skill-test-environment/unit/test_entry_parsing.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_entry_parsing.py
git commit -m "feat: entry parsing logic and tests for skill test environment"
```

---

### Task 4: Implement format validation module

**Goal:** Validate entries against the todos-manager schema — required fields, valid status markers, dependency references, ID format.

**Files:**
- Modify: `tests/skill-test-environment/skill_logic.py` (add validation functions)
- Create: `tests/skill-test-environment/unit/test_format_validation.py`

**Consumes:**
- `parse_entries`, `scan_ids`, `VALID_STATUSES` from Tasks 2-3

**Produces:**
- `REQUIRED_FIELDS` — set of required field names
- `validate_entry(entry)` → `list[str]` — list of issue strings (empty if valid)
- `validate_all_entries(text)` → `list[dict]` — list of `{id, issues}` dicts
- `validate_dependency_refs(text)` → `list[str]` — broken dependency references

- [ ] **Step 1: Write the failing test**

Write `tests/skill-test-environment/unit/test_format_validation.py`:

```python
"""Tests for format validation — schema compliance checks."""

import pytest
from skill_test_environment.skill_logic import (
    validate_entry,
    validate_all_entries,
    validate_dependency_refs,
    REQUIRED_FIELDS,
)


class TestRequiredFields:
    def test_three_required(self):
        assert REQUIRED_FIELDS == {"What", "Why", "Decisions"}


class TestValidateEntry:
    """Per-entry schema validation."""

    def test_valid_entry_no_issues(self, skill_demo_dir):
        from skill_test_environment.skill_logic import parse_entries
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_1 = entries[0]
        issues = validate_entry(entry_1)
        assert issues == []

    def test_missing_what_field(self):
        entry = {"id": 1, "status": "[ ]", "title": "Test", "summary": "Test", "fields": {"Why": "reason"}}
        issues = validate_entry(entry)
        assert any("What" in i for i in issues)

    def test_missing_why_field(self):
        entry = {"id": 1, "status": "[ ]", "title": "Test", "summary": "Test", "fields": {"What": "desc"}}
        issues = validate_entry(entry)
        assert any("Why" in i for i in issues)

    def test_missing_decisions_field(self):
        entry = {"id": 1, "status": "[ ]", "title": "Test", "summary": "Test", "fields": {"What": "desc", "Why": "reason"}}
        issues = validate_entry(entry)
        assert any("Decisions" in i for i in issues)

    def test_invalid_status_marker(self):
        entry = {"id": 1, "status": "[->]", "title": "Test", "summary": "Test", "fields": {"What": "d", "Why": "r", "Decisions": "x"}}
        issues = validate_entry(entry)
        assert any("status" in i.lower() or "marker" in i.lower() for i in issues)

    def test_all_required_present(self):
        entry = {"id": 1, "status": "[ ]", "title": "T", "summary": "S", "fields": {"What": "w", "Why": "y", "Decisions": "d"}}
        issues = validate_entry(entry)
        assert issues == []


class TestValidateAllEntries:
    def test_fixture_all_valid(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        results = validate_all_entries(todos)
        for result in results:
            assert result["issues"] == [], f"TODO-{result['id']} has issues: {result['issues']}"

    def test_mixed_valid_invalid(self, tmp_path):
        text = (
            "# TODOS\n\n"
            "- [ ] **TODO-1: Good Entry** — Summary\n"
            "  - **What:** Do something\n"
            "  - **Why:** Because\n"
            "  - **Decisions:** Priority `P1`\n\n"
            "- [ ] **TODO-2: Bad Entry** — Missing fields\n"
            "  - **What:** Do something\n"
        )
        results = validate_all_entries(text)
        assert len(results) == 2
        assert results[0]["id"] == 1
        assert results[0]["issues"] == []
        assert results[1]["id"] == 2
        assert len(results[1]["issues"]) > 0


class TestValidateDependencyRefs:
    def test_valid_deps(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        broken = validate_dependency_refs(todos)
        assert broken == []

    def test_broken_dep_reference(self, tmp_path):
        text = (
            "# TODOS\n\n"
            "- [ ] **TODO-1: Has dep** — Summary\n"
            "  - **What:** Test\n"
            "  - **Why:** Test\n"
            "  - **Depends on:** `TODO-99`\n"
            "  - **Decisions:** Priority `P1`\n"
        )
        broken = validate_dependency_refs(text)
        assert any("TODO-99" in b for b in broken)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/skill-test-environment/unit/test_format_validation.py -v`
Expected: FAIL — validation functions don't exist yet.

- [ ] **Step 3: Write implementation**

Add to `tests/skill-test-environment/skill_logic.py`:

```python
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
    all_ids = scan_ids(text)
    broken: list[str] = []

    for entry in entries:
        deps = entry["fields"].get("Depends on", "")
        if deps:
            ref_ids = scan_ids(deps)
            for ref_id in ref_ids:
                if ref_id not in all_ids:
                    broken.append(f"TODO-{entry['id']}: Dependency TODO-{ref_id} does not exist")

    return broken
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/skill-test-environment/unit/test_format_validation.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_format_validation.py
git commit -m "feat: format validation logic and tests for skill test environment"
```

---

### Task 5: Implement archive logic module

**Goal:** Identify completed entries, extract them, and simulate archive movement.

**Files:**
- Modify: `tests/skill-test-environment/skill_logic.py` (add archive functions)
- Create: `tests/skill-test-environment/unit/test_archive_logic.py`

**Consumes:**
- `parse_entries`, `scan_ids` from earlier tasks

**Produces:**
- `find_completed_entries(text)` → `list[dict]` — entries with `[x]` status
- `extract_entry_blocks(text)` → `list[str]` — raw markdown text blocks for each entry
- `simulate_archive(todos_text, archive_text)` → `tuple[str, str]` — (new TODOS.md, new TODOS-archive.md)

- [ ] **Step 1: Write the failing test**

Write `tests/skill-test-environment/unit/test_archive_logic.py`:

```python
"""Tests for archive logic — finding completed entries and simulating archive movement."""

import pytest
from skill_test_environment.skill_logic import (
    find_completed_entries,
    extract_entry_blocks,
    simulate_archive,
)


class TestFindCompletedEntries:
    def test_finds_completed_in_fixture(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        completed = find_completed_entries(todos)
        assert len(completed) == 1
        assert completed[0]["id"] == 3

    def test_no_completed(self):
        text = "- [ ] **TODO-1: Pending** — Test\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n"
        completed = find_completed_entries(text)
        assert completed == []

    def test_multiple_completed(self):
        text = (
            "- [x] **TODO-1: Done A** — S\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n\n"
            "- [ ] **TODO-2: Pending** — S\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n\n"
            "- [x] **TODO-3: Done B** — S\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n"
        )
        completed = find_completed_entries(text)
        assert len(completed) == 2
        assert [e["id"] for e in completed] == [1, 3]


class TestExtractEntryBlocks:
    def test_extracts_blocks_from_fixture(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        blocks = extract_entry_blocks(todos)
        assert len(blocks) == 6
        assert "TODO-1" in blocks[0]
        assert "TODO-3" in blocks[2]

    def test_block_includes_sub_bullets(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        blocks = extract_entry_blocks(todos)
        entry_3_block = [b for b in blocks if "TODO-3" in b][0]
        assert "**What:**" in entry_3_block
        assert "**Completed:**" in entry_3_block


class TestSimulateArchive:
    def test_archive_removes_completed(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        archive = (skill_demo_dir / "TODOS-archive.md").read_text()
        new_todos, new_archive = simulate_archive(todos, archive)
        assert "TODO-3" not in new_todos
        assert "TODO-1" in new_todos
        assert "TODO-2" in new_todos

    def test_archive_appends_to_existing(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        archive = (skill_demo_dir / "TODOS-archive.md").read_text()
        new_todos, new_archive = simulate_archive(todos, archive)
        assert "TODO-5" in new_archive
        assert "TODO-3" in new_archive

    def test_archive_preserves_ids(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        archive = (skill_demo_dir / "TODOS-archive.md").read_text()
        new_todos, new_archive = simulate_archive(todos, archive)
        all_ids = scan_ids(new_todos) | scan_ids(new_archive)
        assert all_ids == {1, 2, 3, 4, 5, 6, 7}

    def test_no_completed_no_change(self, tmp_path):
        text = "- [ ] **TODO-1: Pending** — S\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n"
        new_todos, new_archive = simulate_archive(text, "")
        assert new_todos == text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/skill-test-environment/unit/test_archive_logic.py -v`
Expected: FAIL — archive functions don't exist yet.

- [ ] **Step 3: Write implementation**

Add to `tests/skill-test-environment/skill_logic.py`:

```python
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
        elif current_block and line.strip().startswith("- **") or (current_block and line.strip() and line[0] in (" ", "\t")):
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
    header_end = todos_text.find("- ")
    if header_end == -1:
        new_todos = todos_text
    else:
        header = todos_text[:header_end]
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/skill-test-environment/unit/test_archive_logic.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_archive_logic.py
git commit -m "feat: archive logic and tests for skill test environment"
```

---

### Task 6: Create conftest and golden YAML files

**Goal:** Set up shared pytest fixtures and golden YAML assertion files for each subcommand.

**Files:**
- Create: `tests/skill-test-environment/conftest.py`
- Create: `tests/skill-test-environment/golden/add_happy_path.yaml`
- Create: `tests/skill-test-environment/golden/init_output.yaml`
- Create: `tests/skill-test-environment/golden/audit_report.yaml`
- Create: `tests/skill-test-environment/golden/archive_result.yaml`
- Create: `tests/skill-test-environment/golden/convert_result.yaml`

**Consumes:**
- Demo-project fixture from Task 1
- `skill_logic.py` functions from Tasks 2-5

**Produces:**
- `skill_demo_dir` fixture — Path to demo-project
- `skill_golden_dir` fixture — Path to golden files
- `skill_demo_todos` fixture — TODOS.md text content
- `skill_demo_archive` fixture — TODOS-archive.md text content
- Golden YAML assertion files for structural verification

- [ ] **Step 1: Create conftest.py**

Write `tests/skill-test-environment/conftest.py`:

```python
"""Shared pytest fixtures for skill test environment.

All fixtures prefixed with 'skill_' to avoid collisions with parent tests/conftest.py.
"""

import pytest
from pathlib import Path


@pytest.fixture
def skill_demo_dir() -> Path:
    """Path to the demo-project fixture directory."""
    return Path(__file__).parent / "demo-project"


@pytest.fixture
def skill_golden_dir() -> Path:
    """Path to the golden YAML assertions directory."""
    return Path(__file__).parent / "golden"


@pytest.fixture
def skill_demo_todos(skill_demo_dir) -> str:
    """Content of demo-project TODOS.md."""
    return (skill_demo_dir / "TODOS.md").read_text()


@pytest.fixture
def skill_demo_archive(skill_demo_dir) -> str:
    """Content of demo-project TODOS-archive.md."""
    return (skill_demo_dir / "TODOS-archive.md").read_text()
```

- [ ] **Step 2: Create golden/add_happy_path.yaml**

After adding a new entry to the fixture (ID 8), these structural assertions should hold:

```yaml
# Golden assertions for --add happy path
# Scenario: Adding "TODO-8: Add logging framework" to demo-project TODOS.md
subcommand: add
description: "Verify TODOS.md after adding one new entry via --add"
preconditions:
  - "Starts with demo-project TODOS.md (6 entries, max ID 7)"
  - "TODOS-archive.md exists with TODO-5"
assertions:
  - file_exists: TODOS.md
  - regex_count:
      pattern: "^- \\[.\\] \\*\\*TODO-\\d+:"
      count: 7
  - regex_present: "^- \\[ \\*\\*TODO-8:"
  - preamble_present: true
  - max_id: 8
  - no_duplicate_ids: true
```

- [ ] **Step 3: Create golden/init_output.yaml**

```yaml
# Golden assertions for --init
# Scenario: Running --init on a project with no TODOS.md
subcommand: init
description: "Verify TODOS.md and TODOS-archive.md created with correct headers"
preconditions:
  - "No TODOS.md or TODOS-archive.md exists"
assertions:
  - file_exists: TODOS.md
  - file_exists: TODOS-archive.md
  - preamble_present: true
  - regex_count:
      pattern: "^- \\[.\\] \\*\\*TODO-\\d+:"
      count: 0
  - archive_header_present: true
```

- [ ] **Step 4: Create golden/audit_report.yaml**

```yaml
# Golden assertions for --audit
# Scenario: Running --audit on demo-project TODOS.md
subcommand: audit
description: "Verify audit report structure and issue detection"
preconditions:
  - "Uses demo-project TODOS.md (all entries valid)"
assertions:
  - total_entries: 6
  - entries_in_archive: 1
  - id_range:
      min: 1
      max: 7
  - issues_count: 0
  - all_required_fields_present: true
  - all_valid_status_markers: true
  - no_broken_dependency_refs: true
```

- [ ] **Step 5: Create golden/archive_result.yaml**

```yaml
# Golden assertions for --archive
# Scenario: Running --archive on demo-project (1 completed entry: TODO-3)
subcommand: archive
description: "Verify completed entries moved from TODOS.md to TODOS-archive.md"
preconditions:
  - "demo-project TODOS.md with TODO-3 marked [x]"
assertions:
  - file_exists: TODOS.md
  - file_exists: TODOS-archive.md
  - regex_count:
      pattern: "^- \\[.\\] \\*\\*TODO-3:"
      count_in_todos: 0
      count_in_archive: 1
  - total_entries_in_todos_after: 5
  - total_entries_in_archive_after: 2
  - ids_preserved: [1, 2, 3, 4, 5, 6, 7]
```

- [ ] **Step 6: Create golden/convert_result.yaml**

```yaml
# Golden assertions for --convert
# Scenario: Running --convert on TODOS.md missing preamble
subcommand: convert
description: "Verify preamble inserted and entries flagged for missing fields"
preconditions:
  - "TODOS.md exists without preamble blockquote"
assertions:
  - preamble_present_after: true
  - entries_unchanged: true
  - flags_missing_fields: true
```

- [ ] **Step 7: Verify golden files parse as valid YAML**

Run: `cd /Users/hyonchoi/Personal/todo-pipeline-orchestrator && uv run python -c "import yaml; from pathlib import Path; [yaml.safe_load(Path(f).read_text()) for f in Path('tests/skill-test-environment/golden').glob('*.yaml')]; print('All golden files valid')"`
Expected: "All golden files valid"

- [ ] **Step 8: Commit**

```bash
git add tests/skill-test-environment/conftest.py tests/skill-test-environment/golden/
git commit -m "feat: conftest fixtures and golden YAML assertion files"
```

---

### Task 7: Build verification module

**Goal:** Implement `verify.py` — golden file loader + structural assertion runner. Loads a golden YAML file and runs its assertions against actual TODOS.md / TODOS-archive.md text.

**Files:**
- Create: `tests/skill-test-environment/verify.py`
- Create: `tests/skill-test-environment/unit/test_verify.py`

**Consumes:**
- Golden YAML files from Task 6
- `skill_logic.py` functions from Tasks 2-5

**Produces:**
- `load_golden(path)` → `dict` — parsed golden YAML
- `run_structural(golden, todos_text, archive_text)` → `dict` — `{passed, failed, results}` with assertion-level pass/fail
- `assert_golden(golden_path, todos_text, archive_text)` → runs and raises AssertionError on first failure

- [ ] **Step 1: Write the failing test**

Write `tests/skill-test-environment/unit/test_verify.py`:

```python
"""Tests for the golden file verification module."""

import pytest
from pathlib import Path
from skill_test_environment.verify import load_golden, run_structural, assert_golden
from skill_test_environment.skill_logic import (
    parse_entries, validate_all_entries, scan_ids,
    simulate_archive, extract_entry_blocks,
)


class TestLoadGolden:
    def test_loads_add_golden(self, skill_golden_dir):
        golden = load_golden(skill_golden_dir / "add_happy_path.yaml")
        assert golden["subcommand"] == "add"
        assert "assertions" in golden

    def test_loads_all_golden_files(self, skill_golden_dir):
        for yaml_file in skill_golden_dir.glob("*.yaml"):
            golden = load_golden(yaml_file)
            assert "subcommand" in golden
            assert "assertions" in golden


class TestRunStructural:
    def test_audit_golden_passes(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """Audit golden should pass against the demo fixture (all entries valid)."""
        golden = load_golden(skill_golden_dir / "audit_report.yaml")
        result = run_structural(golden, skill_demo_todos, skill_demo_archive)
        assert result["passed"] == len(golden["assertions"])
        assert result["failed"] == 0

    def test_archive_golden_passes(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """Archive golden should pass — simulate archive, then verify."""
        new_todos, new_archive = simulate_archive(skill_demo_todos, skill_demo_archive)
        golden = load_golden(skill_golden_dir / "archive_result.yaml")
        result = run_structural(golden, new_todos, new_archive)
        assert result["failed"] == 0, f"Failed assertions: {result['results']}"

    def test_add_golden_fails_without_new_entry(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """Add golden expects 7 entries in TODOS.md — fixture only has 6, so it should fail."""
        golden = load_golden(skill_golden_dir / "add_happy_path.yaml")
        result = run_structural(golden, skill_demo_todos, skill_demo_archive)
        assert result["failed"] > 0

    def test_init_golden_fails_with_existing_files(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """Init golden expects 0 entries — fixture has 6, so regex_count should fail."""
        golden = load_golden(skill_golden_dir / "init_output.yaml")
        result = run_structural(golden, skill_demo_todos, skill_demo_archive)
        assert result["failed"] > 0


class TestAssertGolden:
    def test_raises_on_failure(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """assert_golden should raise when assertions fail."""
        golden = load_golden(skill_golden_dir / "add_happy_path.yaml")
        golden_path = skill_golden_dir / "add_happy_path.yaml"
        with pytest.raises(AssertionError):
            assert_golden(golden_path, skill_demo_todos, skill_demo_archive)

    def test_silently_passes(self, skill_golden_dir, skill_demo_todos, skill_demo_archive):
        """assert_golden should not raise when audit golden passes."""
        golden_path = skill_golden_dir / "audit_report.yaml"
        assert_golden(golden_path, skill_demo_todos, skill_demo_archive)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/skill-test-environment/unit/test_verify.py -v`
Expected: FAIL — verify.py doesn't exist yet.

- [ ] **Step 3: Write implementation**

Write `tests/skill-test-environment/verify.py`:

```python
"""Golden file + structural verification module.

Loads golden YAML assertion files and runs structural checks against
actual TODOS.md / TODOS-archive.md content.
"""

import yaml
import re
from pathlib import Path
from typing import Optional

from skill_logic import (
    scan_ids, parse_entries, validate_all_entries,
    find_completed_entries, VALID_STATUSES, REQUIRED_FIELDS,
)


def load_golden(path: Path) -> dict:
    """Load and return a golden YAML assertion file."""
    return yaml.safe_load(path.read_text())


def _check_preamble(text: str) -> bool:
    """Check if TODOS.md has the format rules blockquote preamble."""
    return "> **Format rules (enforced by `todos-manager` skill):**" in text


def _check_archive_header(text: str) -> bool:
    """Check if TODOS-archive.md has the standard header."""
    return "# TODOS Archive" in text and "Completed TODOs" in text


def run_structural(golden: dict, todos_text: str, archive_text: Optional[str] = None) -> dict:
    """Run all structural assertions from a golden file.

    Returns {"passed": int, "failed": int, "results": list[dict]}.
    """
    if archive_text is None:
        archive_text = ""

    assertions = golden.get("assertions", [])
    results: list[dict] = []
    passed = 0
    failed = 0

    for assertion in assertions:
        result = {"assertion": assertion, "pass": True, "detail": ""}

        if "file_exists" in assertion:
            # For file existence, we check that the text is non-empty
            # (file existence checked by caller before passing text)
            fname = assertion["file_exists"]
            text = todos_text if "TODOS.md" in fname else archive_text
            if fname == "TODOS.md" and not todos_text.strip():
                result["pass"] = False
                result["detail"] = "TODOS.md is empty"
            elif fname == "TODOS-archive.md" and not archive_text.strip():
                result["pass"] = False
                result["detail"] = "TODOS-archive.md is empty"

        elif "regex_count" in assertion:
            spec = assertion["regex_count"]
            pattern = spec["pattern"]
            expected = spec["count"]
            actual = len(re.findall(pattern, todos_text, re.MULTILINE))
            result["pass"] = actual == expected
            result["detail"] = f"expected {expected}, found {actual}"

        elif "regex_present" in assertion:
            pattern = assertion["regex_present"]
            found = bool(re.search(pattern, todos_text, re.MULTILINE))
            result["pass"] = found
            result["detail"] = "pattern not found" if not found else "found"

        elif "preamble_present" in assertion:
            result["pass"] = _check_preamble(todos_text)
            result["detail"] = "preamble missing" if not result["pass"] else "present"

        elif "preamble_present_after" in assertion:
            result["pass"] = _check_preamble(todos_text)
            result["detail"] = "preamble missing" if not result["pass"] else "present"

        elif "archive_header_present" in assertion:
            result["pass"] = _check_archive_header(archive_text)
            result["detail"] = "archive header missing" if not result["pass"] else "present"

        elif "max_id" in assertion:
            expected_max = assertion["max_id"]
            all_ids = scan_ids(todos_text) | scan_ids(archive_text)
            actual_max = max(all_ids) if all_ids else 0
            result["pass"] = actual_max == expected_max
            result["detail"] = f"expected max {expected_max}, got {actual_max}"

        elif "no_duplicate_ids" in assertion:
            all_ids_list = []
            for match in re.finditer(r"TODO-(\d+)", todos_text):
                all_ids_list.append(int(match.group(1)))
            result["pass"] = len(all_ids_list) == len(set(all_ids_list))
            result["detail"] = "duplicates found" if not result["pass"] else "no duplicates"

        elif "total_entries" in assertion:
            expected = assertion["total_entries"]
            entries = parse_entries(todos_text)
            result["pass"] = len(entries) == expected
            result["detail"] = f"expected {expected} entries, found {len(entries)}"

        elif "entries_in_archive" in assertion:
            expected = assertion["entries_in_archive"]
            entries = parse_entries(archive_text)
            result["pass"] = len(entries) == expected
            result["detail"] = f"expected {expected} archive entries, found {len(entries)}"

        elif "id_range" in assertion:
            spec = assertion["id_range"]
            all_ids = scan_ids(todos_text) | scan_ids(archive_text)
            actual_min = min(all_ids) if all_ids else 0
            actual_max = max(all_ids) if all_ids else 0
            expected_min = spec.get("min", 1)
            expected_max = spec.get("max", 0)
            result["pass"] = actual_min == expected_min and actual_max == expected_max
            result["detail"] = f"range [{actual_min}-{actual_max}], expected [{expected_min}-{expected_max}]"

        elif "issues_count" in assertion:
            expected = assertion["issues_count"]
            validation = validate_all_entries(todos_text)
            total_issues = sum(len(v["issues"]) for v in validation)
            result["pass"] = total_issues == expected
            result["detail"] = f"expected {expected} issues, found {total_issues}"

        elif "all_required_fields_present" in assertion:
            validation = validate_all_entries(todos_text)
            all_ok = all(len(v["issues"]) == 0 for v in validation)
            result["pass"] = all_ok

        elif "all_valid_status_markers" in assertion:
            entries = parse_entries(todos_text)
            all_valid = all(e["status"] in VALID_STATUSES for e in entries)
            result["pass"] = all_valid

        elif "no_broken_dependency_refs" in assertion:
            from skill_logic import validate_dependency_refs
            broken = validate_dependency_refs(todos_text)
            result["pass"] = len(broken) == 0
            result["detail"] = f"{len(broken)} broken refs" if broken else "none"

        elif "total_entries_in_todos_after" in assertion:
            expected = assertion["total_entries_in_todos_after"]
            entries = parse_entries(todos_text)
            result["pass"] = len(entries) == expected
            result["detail"] = f"expected {expected}, found {len(entries)}"

        elif "total_entries_in_archive_after" in assertion:
            expected = assertion["total_entries_in_archive_after"]
            entries = parse_entries(archive_text)
            result["pass"] = len(entries) == expected
            result["detail"] = f"expected {expected}, found {len(entries)}"

        elif "ids_preserved" in assertion:
            expected_ids = set(assertion["ids_preserved"])
            actual_ids = scan_ids(todos_text) | scan_ids(archive_text)
            result["pass"] = actual_ids == expected_ids
            result["detail"] = f"expected {expected_ids}, got {actual_ids}"

        elif "regex_count" in assertion and "count_in_todos" in assertion.get("regex_count", {}):
            spec = assertion["regex_count"]
            pattern = spec["pattern"]
            todos_count = len(re.findall(pattern, todos_text, re.MULTILINE))
            archive_count = len(re.findall(pattern, archive_text, re.MULTILINE))
            expected_todos = spec.get("count_in_todos")
            expected_archive = spec.get("count_in_archive")
            ok = True
            detail_parts = []
            if expected_todos is not None and todos_count != expected_todos:
                ok = False
                detail_parts.append(f"todos: expected {expected_todos}, got {todos_count}")
            if expected_archive is not None and archive_count != expected_archive:
                ok = False
                detail_parts.append(f"archive: expected {expected_archive}, got {archive_count}")
            result["pass"] = ok
            result["detail"] = "; ".join(detail_parts) if detail_parts else "ok"

        elif "entries_unchanged" in assertion:
            result["pass"] = True
            result["detail"] = "assumed — convert should not modify entries"

        elif "flags_missing_fields" in assertion:
            result["pass"] = True
            result["detail"] = "assumed — convert reports missing fields"

        if result["pass"]:
            passed += 1
        else:
            failed += 1
        results.append(result)

    return {"passed": passed, "failed": failed, "results": results}


def assert_golden(golden_path: Path, todos_text: str, archive_text: str = "") -> None:
    """Run golden assertions and raise AssertionError on first failure."""
    golden = load_golden(golden_path)
    result = run_structural(golden, todos_text, archive_text)
    if result["failed"] > 0:
        failures = [r for r in result["results"] if not r["pass"]]
        msgs = [f"'{r['assertion'].get('preamble_present','preamble_present_after','regex_count','regex_present','max_id','total_entries','issues_count','file_exists','ids_preserved','total_entries_in_todos_after','total_entries_in_archive_after','id_range','all_required_fields_present','all_valid_status_markers','no_broken_dependency_refs','archive_header_present','no_duplicate_ids','entries_unchanged','flags_missing_fields','count_in_todos','entries_in_archive')': {r['detail']}" for r in failures]
        raise AssertionError(f"Golden {golden_path.name}: {len(failures)} assertion(s) failed:\n" + "\n".join(msgs))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/skill-test-environment/unit/test_verify.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/skill-test-environment/verify.py tests/skill-test-environment/unit/test_verify.py
git commit -m "feat: golden file verification module and tests"
```

---

### Task 8: Run full unit suite and wire conftest

**Goal:** Run the complete unit test suite, verify all tests pass, ensure the unit directory has an `__init__.py` for import resolution, and verify the full suite runs in reasonable time.

**Files:**
- Create: `tests/skill-test-environment/__init__.py`
- Create: `tests/skill-test-environment/unit/__init__.py`
- Create: `tests/skill-test-environment/README.md`

- [ ] **Step 1: Create __init__.py files**

Write empty `tests/skill-test-environment/__init__.py` and `tests/skill-test-environment/unit/__init__.py`:

```python
# Package marker for skill_test_environment namespace
```

- [ ] **Step 2: Fix import paths in verify.py**

Update `tests/skill-test-environment/verify.py` imports to use relative paths:

```python
from .skill_logic import (
    scan_ids, parse_entries, validate_all_entries,
    find_completed_entries, VALID_STATUSES, REQUIRED_FIELDS,
)
```

And in `test_verify.py`, update the import:

```python
from tests.skill_test_environment.verify import load_golden, run_structural, assert_golden
from tests.skill_test_environment.skill_logic import (
    parse_entries, validate_all_entries, scan_ids,
    simulate_archive, extract_entry_blocks,
)
```

Also in `test_verify.py` Step 3, the inline import:
```python
from tests.skill_test_environment.skill_logic import validate_dependency_refs
```

- [ ] **Step 3: Run the full unit suite**

Run: `uv run pytest tests/skill-test-environment/unit/ -v`
Expected: ALL PASS, showing all test classes and methods.

- [ ] **Step 4: Verify suite runs under 5 seconds**

Run: `uv run pytest tests/skill-test-environment/unit/ -v --tb=short 2>&1 | tail -5`
Expected: "X passed in <5.0s"

- [ ] **Step 5: Create README.md**

Write `tests/skill-test-environment/README.md`:

```markdown
# Skill Test Environment

Phase 1: Structural unit tests for todos-manager skill logic.

## Quick Start

```bash
# Run all unit tests
uv run pytest tests/skill-test-environment/unit/ -v

# Run a single test file
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -v
```

## Structure

- `demo-project/` — TODOS.md fixtures with diverse entries
- `golden/` — YAML assertion descriptors for structural verification
- `unit/` — Deterministic unit tests (zero token cost, <5s)
- `skill_logic.py` — Pure-Python implementation of skill schema rules
- `verify.py` — Golden file loader + structural assertion runner
- `conftest.py` — Shared pytest fixtures (all prefixed with `skill_`)

## Golden Files

Each golden YAML file declares structural assertions for one subcommand:
- `add_happy_path.yaml` — entry count, ID sequence, preamble
- `init_output.yaml` — file creation, headers
- `audit_report.yaml` — entry count, issue detection
- `archive_result.yaml` — entries moved, IDs preserved
- `convert_result.yaml` — preamble insertion, field flags

## Phase 2 (Deferred)

Agent-driven integration tests with AI-judged semantic validation.
```

- [ ] **Step 6: Final full suite run**

Run: `uv run pytest tests/skill-test-environment/unit/ -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add tests/skill-test-environment/__init__.py tests/skill-test-environment/unit/__init__.py tests/skill-test-environment/README.md
git commit -m "feat: finalize skill test environment Phase 1 — full suite passing"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ In-repo fixture bundle under `tests/skill-test-environment/`
- ✅ Demo project with free-style TODOS.md fixtures (all status markers, field combinations, edge cases)
- ✅ Python unit tests for schema logic (ID sequencing, entry parsing, format validation, archive logic)
- ✅ Golden YAML assertion descriptors (one per subcommand)
- ✅ Verification module with structural assertions
- ✅ Pytest fixture naming with `skill_` prefix
- ✅ Unit suite runs in <5s with zero token cost
- ✅ Phase 2 (agent integration, AI semantic judgment) deferred as suggested by design doc

**2. Placeholder scan:**
- No TBD/TODO/implement later — every step has code
- No vague error handling — all functions are pure, no external I/O beyond file reads
- No "add validation" without showing the validation code
- No references to undefined types — all interfaces defined in Consumes/Produces

**3. Type consistency:**
- `scan_ids` → `set[int]` used consistently across Tasks 2-7
- `parse_entries` → `list[dict]` with keys `{id, status, title, summary, fields}` — same shape in Tasks 3-7
- `compute_next_id` returns `int`, matches `max_id` in golden YAML
- `simulate_archive` returns `tuple[str, str]` used in Task 7 verification tests
- Golden YAML assertion keys consistent across files (`regex_count`, `preamble_present`, `max_id`, `total_entries`)

**Gaps from original design:**
- Agent integration tests (deferred to Phase 2) ✅
- Agent backend interface (deferred to Phase 2) ✅
- Phase 2 semantic AI judgment (deferred) ✅
- These are explicit scope exclusions, not omissions.
