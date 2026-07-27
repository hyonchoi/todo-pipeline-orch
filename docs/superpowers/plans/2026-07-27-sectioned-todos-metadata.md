# Sectioned TODOS Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate todos-manager from accepting `NEXT_TODO_ID:` anywhere in `TODOS.md` to a canonical three-section grammar where metadata, schema documentation, and active entries have separate enforced ranges.

**Architecture:** Add one shared document-section parser to the deterministic oracle and route metadata reads, metadata replacement, entry parsing, validation, dependency checks, archive simulation, and add/audit reconciliation through it. Then update bundled todos-manager Markdown runtime docs, fixtures, golden files, root `TODOS.md`, and production `recover-counter` so the skill-test oracle and product CLI agree on the same grammar.

**Tech Stack:** Python 3.12, uv, pytest, Ruff, Markdown runtime docs under `hermes_pipeline/data/skills/todos-manager/`.

## Global Constraints

- Work in `/Users/hyonchoi/Personal/todo-pipeline-orchestrator/.worktrees/skill-installer-todo-id-tracking`.
- Use shell commands such as `rg`, `sed`, `cat`, `uv run pytest`, and `uv run ruff`; do not use built-in Grep/Glob/Read.
- `NEXT_TODO_ID` is valid only inside `## Metadata`.
- `## Entry Schema` is documentation only.
- TODO entries are valid only under `## Entries`.
- Duplicate or misplaced `NEXT_TODO_ID:` lines anywhere outside `## Metadata` are invalid.
- `--audit`, `--convert`, and pre-add reconciliation migrate old layouts into the canonical three-section shape.
- Migration must preserve existing TODO entry text byte-for-byte where practical.
- After migration, a second audit should be idempotent.
- Keep `tests/skill-test-environment/skill_logic.py` and bundled runtime docs under `hermes_pipeline/data/skills/todos-manager/` in lockstep.
- Keep `.hermes/todo_id_counter` as compatibility/cache state; do not remove it.
- Do not change TODO entry field schema beyond section placement.
- Do not reopen installer safety scope in this plan except where final verification must acknowledge existing unresolved uninstall defects.

---

## File Structure

- Modify `tests/skill-test-environment/skill_logic.py`: add `TodoDocumentSections`, `parse_todos_document_sections()`, section-aware metadata helpers, section-aware entry consumers, and section-preserving archive/add behavior.
- Modify `tests/skill-test-environment/unit/test_format_validation.py`: cover valid sectioned metadata, misplaced metadata, duplicate metadata, legacy migration, CRLF handling, and canonical replacement.
- Modify `tests/skill-test-environment/unit/test_entry_parsing.py`: prove entries are parsed only under `## Entries` and schema examples are ignored.
- Modify `tests/skill-test-environment/unit/test_archive_logic.py`: prove archive discovery and block extraction only operate under `## Entries`.
- Modify `tests/skill-test-environment/unit/test_id_sequencing.py`: prove add/reconcile insert under `## Entries`, migrate old layouts, and remain atomic.
- Modify `tests/test_recover_counter_cli.py`: prove production `recover-counter` accepts only valid `## Metadata` tracked state.
- Modify `hermes_pipeline/counter.py`: mirror the section-aware tracked metadata reader for the CLI compatibility cache.
- Modify `hermes_pipeline/data/skills/todos-manager/SKILL.md`: update all user-facing command algorithms to the three-section grammar.
- Modify `hermes_pipeline/data/skills/todos-manager/sections/schema.md`, `sections/id-assignment.md`, `sections/entry-boundary.md`, `sections/list.md`, `sections/revise.md`, `sections/convert-mode-b.md`, `sections/error-messages.md`, and `sections/acceptance-scenarios.md`: align runtime details with the new grammar.
- Modify `tests/skill-test-environment/demo-project/TODOS.md`, `TODOS.md`, and any golden YAML under `tests/skill-test-environment/golden/` whose expected output includes metadata placement or entry counts.
- Modify docs that mention metadata placement: `docs/howto-todos-manager.md`, `docs/reference-counter.md`, `docs/howto-debugging-and-recovery.md`, `docs/ARCHITECTURE.md`, and `docs/explanation-skill-test-harness-design.md`.

### Task 1: Shared Section Parser And Metadata Contract

**Files:**
- Modify: `tests/skill-test-environment/skill_logic.py`
- Modify: `tests/skill-test-environment/unit/test_format_validation.py`

**Interfaces:**
- Consumes: Existing `NEXT_TODO_ID_LINE_RE`, `read_next_todo_id(text: str) -> tuple[int | None, list[str]]`, `replace_next_todo_id_line(text: str, next_id: int) -> str`.
- Produces:
  - `TodoDocumentSections(metadata: str, schema: str, entries: str, diagnostics: list[str], newline: str, has_canonical_layout: bool)`
  - `parse_todos_document_sections(text: str) -> TodoDocumentSections`
  - `read_next_todo_id(text: str) -> tuple[int | None, list[str]]`, now valid only from `## Metadata`
  - `replace_next_todo_id_line(text: str, next_id: int) -> str`, now normalizes to canonical three-section layout

- [ ] **Step 1: Write failing parser and metadata tests**

Add this import to `tests/skill-test-environment/unit/test_format_validation.py`:

```python
from tests.skill_test_environment.skill_logic import parse_todos_document_sections
```

Add these tests to `TestTrackedNextTodoIdFormat`:

```python
    def test_parse_sections_returns_three_ranges(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n"
            "> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        sections = parse_todos_document_sections(text)

        assert sections.metadata == "NEXT_TODO_ID: 8\n"
        assert "> **Format rules:**" in sections.schema
        assert "TODO-7: Existing" in sections.entries
        assert sections.diagnostics == []
        assert sections.has_canonical_layout is True

    def test_read_next_todo_id_from_metadata_section(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
        )

        value, issues = read_next_todo_id(text)

        assert value == 8
        assert issues == []

    def test_read_next_todo_id_rejects_metadata_under_entries(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "NEXT_TODO_ID: 8\n"
        )

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("outside ## Metadata" in issue for issue in issues)

    def test_read_next_todo_id_rejects_metadata_under_schema(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "\n"
            "## Entry Schema\n\n"
            "NEXT_TODO_ID: 8\n"
            "> **Format rules:**\n\n"
            "## Entries\n"
        )

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("outside ## Metadata" in issue for issue in issues)

    def test_read_next_todo_id_rejects_duplicate_across_sections(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "NEXT_TODO_ID: 99\n"
        )

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("duplicated" in issue.lower() for issue in issues)

    def test_replace_next_todo_id_line_migrates_legacy_layout(self):
        text = (
            "# TODOS\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "> **Format rules:**\n"
            "> - Entry header: example\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        updated = replace_next_todo_id_line(text, 9)

        assert updated.startswith("# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 9\n\n")
        assert "\n## Entry Schema\n\n> **Format rules:**" in updated
        assert "\n## Entries\n\n- [ ] **TODO-7: Existing**" in updated
        assert updated.count("NEXT_TODO_ID:") == 1

    def test_replace_next_todo_id_line_preserves_crlf(self):
        text = (
            "# TODOS\r\n\r\n"
            "## Metadata\r\n\r\n"
            "NEXT_TODO_ID: 8\r\n\r\n"
            "## Entry Schema\r\n\r\n"
            "> **Format rules:**\r\n\r\n"
            "## Entries\r\n"
        )

        updated = replace_next_todo_id_line(text, 9)

        assert "NEXT_TODO_ID: 9\r\n" in updated
        assert "\n" not in updated.replace("\r\n", "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/skill-test-environment/unit/test_format_validation.py -v
```

Expected: FAIL because `parse_todos_document_sections` is missing and existing `read_next_todo_id()` still accepts a single line anywhere.

- [ ] **Step 3: Add the section parser and canonical replacement**

In `tests/skill-test-environment/skill_logic.py`, add this near the regex constants:

```python
from dataclasses import dataclass


SECTION_HEADINGS = ("## Metadata", "## Entry Schema", "## Entries")


@dataclass(frozen=True)
class TodoDocumentSections:
    metadata: str
    schema: str
    entries: str
    diagnostics: list[str]
    newline: str
    has_canonical_layout: bool
```

Replace `_metadata_line_indexes()`, `read_next_todo_id()`, and `replace_next_todo_id_line()` with:

```python
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


def _sectioned_schema_from_legacy(lines: list[str]) -> list[str]:
    preamble_indexes = set(_preamble_line_indexes(lines))
    return [
        line
        for index, line in enumerate(lines)
        if index in preamble_indexes and not NEXT_TODO_ID_LINE_RE.fullmatch(_line_without_ending(line))
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


def read_next_todo_id(text: str) -> tuple[int | None, list[str]]:
    sections = parse_todos_document_sections(text)
    lines = text.splitlines(keepends=True)
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


def replace_next_todo_id_line(text: str, next_id: int) -> str:
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
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/skill-test-environment/unit/test_format_validation.py -v
```

Expected: PASS for the new section tests. Existing legacy tests may need assertion updates because old "global metadata" is now migrated/invalid instead of accepted as canonical.

- [ ] **Step 5: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_format_validation.py
git commit -m "feat: add sectioned TODO metadata parser"
```

### Task 2: Section-Aware Entry Consumers

**Files:**
- Modify: `tests/skill-test-environment/skill_logic.py`
- Modify: `tests/skill-test-environment/unit/test_entry_parsing.py`
- Modify: `tests/skill-test-environment/unit/test_archive_logic.py`
- Modify: `tests/skill-test-environment/unit/test_format_validation.py`

**Interfaces:**
- Consumes: `parse_todos_document_sections(text: str) -> TodoDocumentSections`.
- Produces:
  - `parse_entries(text: str) -> list[dict]`, parsing only `sections.entries`
  - `validate_all_entries(text: str) -> list[dict]`, validating only entries under `## Entries`
  - `validate_dependency_refs(text: str) -> list[str]`, resolving only entries under `## Entries`
  - `find_completed_entries(text: str) -> list[dict]`, finding only completed entries under `## Entries`
  - `extract_entry_blocks(text: str) -> list[str]`, extracting only blocks under `## Entries`
  - `simulate_archive(todos_text: str, archive_text: str) -> tuple[str, str]`, preserving metadata/schema sections

- [ ] **Step 1: Write failing entry-boundary tests**

Add to `tests/skill-test-environment/unit/test_entry_parsing.py`:

```python
    def test_ignores_schema_example_todo(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "- [ ] **TODO-99: Example** — Documentation only\n"
            "  - **What:** Example\n"
            "  - **Why:** Example\n"
            "  - **Decisions:** Example\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Real** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        entries = parse_entries(text)

        assert [entry["id"] for entry in entries] == [7]

    def test_ignores_todos_outside_entries_section(self):
        text = (
            "# TODOS\n\n"
            "- [ ] **TODO-1: Legacy Outside** — Summary\n"
            "  - **What:** Work\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 3\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-2: Real** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        entries = parse_entries(text)

        assert [entry["id"] for entry in entries] == [2]
```

Add to `TestValidateAllEntries` in `test_format_validation.py`:

```python
    def test_schema_example_missing_fields_is_not_validated(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 3\n\n"
            "## Entry Schema\n\n"
            "- [ ] **TODO-1: Example Missing Fields** — Documentation only\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-2: Real** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        results = validate_all_entries(text)

        assert [result["id"] for result in results] == [2]
        assert results[0]["issues"] == []
```

Add to `TestValidateDependencyRefs`:

```python
    def test_schema_example_does_not_satisfy_dependency_reference(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 3\n\n"
            "## Entry Schema\n\n"
            "- [ ] **TODO-99: Example** — Documentation only\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-2: Real** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Depends on:** `TODO-99`\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        broken = validate_dependency_refs(text)

        assert broken == ["TODO-2: Dependency TODO-99 does not exist"]
```

Add to `tests/skill-test-environment/unit/test_archive_logic.py`:

```python
    def test_schema_completed_example_is_not_archived(self):
        todos = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 4\n\n"
            "## Entry Schema\n\n"
            "- [x] **TODO-99: Done Example** — Documentation only\n\n"
            "## Entries\n\n"
            "- [x] **TODO-3: Done Real** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        completed = find_completed_entries(todos)
        blocks = extract_entry_blocks(todos)
        new_todos, new_archive = simulate_archive(todos, "")

        assert [entry["id"] for entry in completed] == [3]
        assert len(blocks) == 1
        assert "TODO-99" in new_todos
        assert "TODO-3" not in new_todos
        assert "TODO-3" in new_archive
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/skill-test-environment/unit/test_entry_parsing.py tests/skill-test-environment/unit/test_format_validation.py tests/skill-test-environment/unit/test_archive_logic.py -v
```

Expected: FAIL because existing entry consumers scan the whole file.

- [ ] **Step 3: Route entry consumers through `## Entries`**

In `tests/skill-test-environment/skill_logic.py`, update `parse_entries()` and `extract_entry_blocks()` to read the section text:

```python
def _entries_text(text: str) -> str:
    sections = parse_todos_document_sections(text)
    return sections.entries if sections.has_canonical_layout else text


def parse_entries(text: str) -> list[dict]:
    """Parse TODO entries under ## Entries from TODOS.md markdown text."""
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
```

Update `extract_entry_blocks()`:

```python
def extract_entry_blocks(text: str) -> list[str]:
    """Extract raw markdown blocks for entries under ## Entries."""
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
```

Replace the reconstruction part of `simulate_archive()` with section-preserving logic:

```python
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
```

Then in `simulate_archive()` after `remaining_blocks` and `archived_blocks`:

```python
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
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/skill-test-environment/unit/test_entry_parsing.py tests/skill-test-environment/unit/test_format_validation.py tests/skill-test-environment/unit/test_archive_logic.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_entry_parsing.py tests/skill-test-environment/unit/test_format_validation.py tests/skill-test-environment/unit/test_archive_logic.py
git commit -m "feat: bound TODO entry consumers to entries section"
```

### Task 3: Add, Audit, Convert, And Golden Flow Migration

**Files:**
- Modify: `tests/skill-test-environment/skill_logic.py`
- Modify: `tests/skill-test-environment/unit/test_id_sequencing.py`
- Modify: `tests/skill-test-environment/golden/*.yaml`
- Modify: `tests/skill-test-environment/demo-project/TODOS.md`

**Interfaces:**
- Consumes: `replace_next_todo_id_line()`, `parse_todos_document_sections()`, `_replace_entries_section()`.
- Produces:
  - `assign_next_todo_id(project_dir: Path, entry_builder: Callable[[int], str]) -> tuple[int, list[str]]`, now inserts under `## Entries`
  - `reconcile_next_todo_id(project_dir: Path, mode: str) -> tuple[int, list[str]]`, now repairs to canonical layout
  - Golden files whose expected content shows `## Metadata`, `## Entry Schema`, and `## Entries`

- [ ] **Step 1: Write failing add/reconcile tests**

Add to `tests/skill-test-environment/unit/test_id_sequencing.py`:

```python
    def test_assign_next_todo_id_appends_under_entries_section(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n",
            encoding="utf-8",
        )

        assigned, messages = assign_next_todo_id(
            tmp_path,
            lambda todo_id: (
                f"- [ ] **TODO-{todo_id}: Added** — Summary\n"
                "  - **What:** Work\n"
                "  - **Why:** Reason\n"
                "  - **Decisions:** Priority `P1`"
            ),
        )

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert assigned == 8
        assert messages == []
        assert updated.index("TODO-8: Added") > updated.index("## Entries")
        assert updated.index("TODO-8: Added") > updated.index("TODO-7: Existing")
        assert updated.index("NEXT_TODO_ID: 9") < updated.index("## Entry Schema")

    def test_reconcile_migrates_legacy_layout_to_sections(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "NEXT_TODO_ID: 2\n\n"
            "> **Format rules:**\n"
            "> - Entry header: example\n\n"
            "- [ ] **TODO-1: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n",
            encoding="utf-8",
        )

        reconciled, messages = reconcile_next_todo_id(tmp_path, "audit")

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert reconciled == 2
        assert "## Metadata\n\nNEXT_TODO_ID: 2" in updated
        assert "## Entry Schema\n\n> **Format rules:**" in updated
        assert "## Entries\n\n- [ ] **TODO-1: Existing**" in updated
        assert any("inserted NEXT_TODO_ID" in message or "corrected NEXT_TODO_ID" in message for message in messages)

    def test_reconcile_repairs_misplaced_metadata_under_entries(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "## Metadata\n\n"
            "\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n\n"
            "NEXT_TODO_ID: 99\n",
            encoding="utf-8",
        )

        reconciled, messages = reconcile_next_todo_id(tmp_path, "audit")

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert reconciled == 8
        assert updated.count("NEXT_TODO_ID:") == 1
        assert "## Metadata\n\nNEXT_TODO_ID: 8" in updated
        assert "NEXT_TODO_ID: 99" not in updated
        assert any("outside ## Metadata" in message for message in messages)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -v
```

Expected: FAIL because add still appends at the file end and messages may still use preamble wording.

- [ ] **Step 3: Insert new entries inside `## Entries`**

In `assign_next_todo_id()` replace the transform return with:

```python
        updated = replace_next_todo_id_line(text, assigned + 1)
        entry = entry_builder(assigned).rstrip()
        sections = parse_todos_document_sections(updated)
        if sections.has_canonical_layout:
            existing_entries = sections.entries.rstrip()
            combined_entries = f"{existing_entries}\n\n{entry}" if existing_entries else entry
            return _replace_entries_section(updated, combined_entries)
        return updated.rstrip() + "\n\n" + entry + "\n"
```

In `reconcile_next_todo_id()`, keep the existing locked transform but adjust messages so section diagnostics are included before the mutation report:

```python
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
```

- [ ] **Step 4: Update fixtures and golden files**

Edit `tests/skill-test-environment/demo-project/TODOS.md` to this shape, preserving the existing entries below `## Entries`:

```markdown
# TODOS

## Metadata

NEXT_TODO_ID: 8

## Entry Schema

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Spec:**, **Reference:**
> - **Spec:**/**Reference:** are `--revise`-only (never suggested by `--add` or auto-research); always typed verbatim
> - ID: sequential, immutable TODO-<n>
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

## Entries
```

Then regenerate or hand-update golden YAML files whose expected `TODOS.md` text or audit messages include the old top-level metadata placement:

```bash
rg -n "NEXT_TODO_ID|Format rules|TODO-1|entries" tests/skill-test-environment/golden
```

Expected edits:

```yaml
todos_contains:
  - "## Metadata"
  - "NEXT_TODO_ID: 2"
  - "## Entry Schema"
  - "## Entries"
```

- [ ] **Step 5: Run focused oracle and golden tests**

Run:

```bash
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py tests/skill-test-environment/unit/test_entry_parsing.py tests/skill-test-environment/unit/test_archive_logic.py tests/skill-test-environment/unit/test_format_validation.py -v
uv run pytest tests/skill-test-environment -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_id_sequencing.py tests/skill-test-environment/golden tests/skill-test-environment/demo-project/TODOS.md
git commit -m "feat: migrate TODO oracle flows to sectioned layout"
```

### Task 4: Production Counter Grammar Alignment

**Files:**
- Modify: `hermes_pipeline/counter.py`
- Modify: `tests/test_recover_counter_cli.py`

**Interfaces:**
- Consumes: Existing `recover_counter(project_dir: Path) -> int`.
- Produces: `_read_tracked_next_todo_id(todos_text: str) -> int | None`, accepting tracked state only when exactly one positive `NEXT_TODO_ID` appears inside `## Metadata` and no misplaced duplicate exists.

- [ ] **Step 1: Write failing production counter tests**

In `tests/test_recover_counter_cli.py`, update any old top-level metadata fixture to sectioned layout. Add these tests to `TestRecoverCounterCLI`:

```python
    def test_recover_counter_uses_sectioned_metadata(self, tmp_path, monkeypatch):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "myproject"
        project_dir.mkdir(parents=True)
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Active** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(projects_dir))

        result = main(["recover-counter", "myproject"])

        assert result == 0
        assert (project_dir / ".hermes/todo_id_counter").read_text(encoding="utf-8") == "7"

    def test_recover_counter_ignores_misplaced_tracked_metadata(self, tmp_path, monkeypatch):
        projects_dir = tmp_path / "projects"
        project_dir = projects_dir / "myproject"
        project_dir.mkdir(parents=True)
        (project_dir / ".hermes").mkdir()
        (project_dir / ".hermes/todo_id_counter").write_text("20", encoding="utf-8")
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n"
            "## Metadata\n\n"
            "\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Active** — Summary\n"
            "NEXT_TODO_ID: 8\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(projects_dir))

        result = main(["recover-counter", "myproject"])

        assert result == 0
        assert (project_dir / ".hermes/todo_id_counter").read_text(encoding="utf-8") == "20"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_recover_counter_cli.py -v
```

Expected: FAIL because production `_read_tracked_next_todo_id()` still accepts a single metadata line outside `## Metadata`.

- [ ] **Step 3: Implement section-aware counter parsing**

In `hermes_pipeline/counter.py`, replace `_read_tracked_next_todo_id()` with:

```python
SECTION_HEADINGS = ("## Metadata", "## Entry Schema", "## Entries")


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


def _read_tracked_next_todo_id(todos_text: str) -> int | None:
    lines = todos_text.splitlines(keepends=True)
    heading_positions = [
        _line_without_ending(line)
        for line in lines
        if _line_without_ending(line) in SECTION_HEADINGS
    ]
    if heading_positions != list(SECTION_HEADINGS):
        return None
    spans = _section_spans(lines)
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
```

Keep `NEXT_TODO_ID_RE` CRLF-aware:

```python
NEXT_TODO_ID_RE = re.compile(
    r"^(?:>[ \t]+-[ \t]+)?NEXT_TODO_ID:[ \t]*([1-9][0-9]*)[ \t]*$"
)
```

- [ ] **Step 4: Run focused counter tests**

Run:

```bash
uv run pytest tests/test_recover_counter_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/counter.py tests/test_recover_counter_cli.py
git commit -m "feat: align counter recovery with sectioned metadata"
```

### Task 5: Runtime Docs, Root Files, And Full Verification

**Files:**
- Modify: `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/schema.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/id-assignment.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/entry-boundary.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/list.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/revise.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/convert-mode-b.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/error-messages.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`
- Modify: `TODOS.md`
- Modify: `docs/howto-todos-manager.md`
- Modify: `docs/reference-counter.md`
- Modify: `docs/howto-debugging-and-recovery.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/explanation-skill-test-harness-design.md`

**Interfaces:**
- Consumes: Sectioned oracle behavior from Tasks 1-4.
- Produces: User-runtime Markdown instructions that create, audit, convert, add, archive, list, and revise only through the canonical `## Metadata`, `## Entry Schema`, `## Entries` grammar.

- [ ] **Step 1: Search all old metadata wording**

Run:

```bash
rg -n "preamble|standalone file-level metadata|NEXT_TODO_ID|end of file|after last entry|Format rules" hermes_pipeline/data/skills/todos-manager docs TODOS.md tests/skill-test-environment/golden tests/skill-test-environment/demo-project
```

Expected: Identify every runtime/doc sentence that still says `NEXT_TODO_ID` belongs in a preamble or before a blockquote.

- [ ] **Step 2: Update bundled SKILL command algorithms**

In `hermes_pipeline/data/skills/todos-manager/SKILL.md`, apply these semantic replacements:

```markdown
Read `NEXT_TODO_ID` from `## Metadata`. Treat any `NEXT_TODO_ID:` line under
`## Entry Schema`, under `## Entries`, or outside the canonical sections as
invalid tracked state. `## Entry Schema` is documentation only; never count,
list, archive, revise, validate, or use TODO-like examples in that section.
```

Replace the add write instruction with:

```markdown
Under the TODO write lock, insert the formatted entry under `## Entries` after
the last active entry and increment `NEXT_TODO_ID` under `## Metadata` in the
same atomic replacement. If replacement fails, leave `TODOS.md` byte-for-byte
unchanged.
```

Replace audit wording with:

```markdown
Report and repair section-layout issues before entry schema findings. Repair
missing, malformed, duplicated, misplaced, stale, or conflicting
`NEXT_TODO_ID` metadata by writing exactly one `NEXT_TODO_ID: <n>` line under
`## Metadata`.
```

- [ ] **Step 3: Update section docs with exact canonical skeleton**

In `sections/schema.md` and `sections/id-assignment.md`, include this canonical skeleton:

```markdown
# TODOS

## Metadata

NEXT_TODO_ID: <n>

## Entry Schema

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Spec:**, **Reference:**
> - **Spec:**/**Reference:** are `--revise`-only (never suggested by `--add` or auto-research); always typed verbatim
> - ID: sequential, immutable TODO-<n>
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

## Entries
```

In `sections/list.md` and `sections/revise.md`, state:

```markdown
Only entries under `## Entries` are active TODO entries. Ignore TODO-like text
under `## Entry Schema` and anywhere outside `## Entries`.
```

In `sections/convert-mode-b.md`, state:

```markdown
Converted active entries are written under `## Entries`; converted or preserved
schema/reference text never goes under `## Entries` unless it is an actual TODO
entry.
```

- [ ] **Step 4: Update root and demo TODO files**

Migrate `TODOS.md` and `tests/skill-test-environment/demo-project/TODOS.md` to:

```markdown
# TODOS

## Metadata

NEXT_TODO_ID: <current next id>

## Entry Schema

<existing format rules blockquote>

## Entries

<existing active entries, unchanged where practical>
```

Compute `<current next id>` by scanning active plus archived entries:

```bash
rg -o "TODO-[0-9]+" TODOS.md TODOS-archive.md | sed 's/.*TODO-//' | sort -n | tail -1
```

Expected: `NEXT_TODO_ID` is one greater than the highest active or archived ID.

- [ ] **Step 5: Update user docs**

Apply these wording changes:

```markdown
`TODOS.md` stores tracked ID state under `## Metadata` as `NEXT_TODO_ID: <n>`.
The `## Entry Schema` section documents the format and is never parsed as
active TODO content. Active entries live under `## Entries`.
```

Update counter docs:

```markdown
When `TODOS.md` has valid sectioned tracked metadata and the value equals the
scan-derived next ID, `recover-counter` writes `NEXT_TODO_ID - 1` to
`.hermes/todo_id_counter`. Legacy or invalid section placement falls back to
scanning active plus archived IDs without decreasing a higher existing cache.
```

- [ ] **Step 6: Run focused and full verification**

Run:

```bash
uv run pytest tests/skill-test-environment -v
uv run pytest tests/test_recover_counter_cli.py -v
uv run pytest tests/test_skills_install.py -v
uv run pytest -v
uv run ruff check .
git diff --check
```

Expected: All tests pass, Ruff passes, whitespace check passes. If `tests/test_skills_install.py` exposes the pre-existing unresolved uninstall transaction/dangling-symlink defects, fix those defects in a separate follow-up task or explicitly stop and report that the existing branch blocker remains outside this sectioned-metadata migration.

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/data/skills/todos-manager tests/skill-test-environment/golden tests/skill-test-environment/demo-project/TODOS.md TODOS.md docs/howto-todos-manager.md docs/reference-counter.md docs/howto-debugging-and-recovery.md docs/ARCHITECTURE.md docs/explanation-skill-test-harness-design.md
git commit -m "docs: document sectioned TODO metadata grammar"
```

## Self-Review

Spec coverage:
- Sectioned metadata validity is covered by Task 1.
- Entry parsing only under `## Entries` is covered by Task 2.
- Add/audit/reconciliation migration behavior is covered by Task 3.
- Production `recover-counter` grammar alignment is covered by Task 4.
- Runtime docs, fixtures, goldens, root TODO migration, and full verification are covered by Task 5.

Placeholder scan:
- No task contains "TBD", "implement later", "add appropriate error handling", or "write tests for the above" without concrete test content.

Type consistency:
- `TodoDocumentSections`, `parse_todos_document_sections()`, `read_next_todo_id()`, `replace_next_todo_id_line()`, `parse_entries()`, `extract_entry_blocks()`, `assign_next_todo_id()`, and `reconcile_next_todo_id()` signatures are consistent across tasks.

Residual risk:
- The snippets intentionally preserve legacy parsing fallback in `_entries_text()` for unsectioned files until reconciliation migrates them. If review wants strict invalidation everywhere immediately, remove that fallback only after `--convert` and `--audit` tests prove all command paths normalize before parsing.
- This plan does not fix the known installer uninstall transaction/dangling-symlink blocker except by keeping `tests/test_skills_install.py` in the final verification gate.
