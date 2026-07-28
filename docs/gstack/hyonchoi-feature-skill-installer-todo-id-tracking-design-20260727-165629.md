# Sectioned TODOS.md Metadata Plan

Branch: `feature/skill-installer-todo-id-tracking`
Date: 2026-07-27

## Problem

`NEXT_TODO_ID` moved from the format-rules blockquote into standalone file metadata, but the current readers accept a single `NEXT_TODO_ID:` line anywhere in `TODOS.md`. That means a misplaced metadata line after TODO entries can still be treated as valid state.

The file needs a durable grammar so humans and parsers agree where metadata, schema documentation, and actual entries live.

## Recommendation

Adopt an explicit three-section layout:

```markdown
# TODOS

## Metadata

NEXT_TODO_ID: 39

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

- [ ] **TODO-4: Example task** — Example summary
  - **What:** Do the work.
  - **Why:** Explain the reason.
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `feature/example`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
```

This is a grammar migration, not a parser tweak. The section headers make the format self-describing and prevent misplaced metadata from being silently accepted.

## Contract

- `NEXT_TODO_ID` is valid only inside `## Metadata`.
- `## Entry Schema` is documentation only.
- TODO entries are valid only under `## Entries`.
- Duplicate or misplaced `NEXT_TODO_ID:` lines anywhere outside `## Metadata` are invalid.
- `--audit`, `--convert`, and pre-add reconciliation migrate old layouts into the canonical three-section shape.
- Migration must preserve existing TODO entry text byte-for-byte where practical.
- After migration, a second audit should be idempotent.

## Implementation Plan

1. Add one shared section layout primitive in both production and the deterministic
   test oracle:

   ```python
   def parse_todos_document_sections(text: str) -> TodoDocumentSections:
       ...
   ```

   Recognize exactly:
   - `## Metadata`
   - `## Entry Schema`
   - `## Entries`

   The helper owns all section boundary rules and returns metadata, schema, entries,
   and diagnostics. Do not duplicate "find the entries section" logic in each
   command path.

2. Change `read_next_todo_id()` and `_read_tracked_next_todo_id()`:

   - scan all lines for `NEXT_TODO_ID:` to detect duplicates and misplacement
   - parse the valid value only from the `## Metadata` range
   - treat metadata under `## Entry Schema`, under `## Entries`, or outside any section as invalid tracked state

3. Change `replace_next_todo_id_line()` to normalize layout:

   - ensure `# TODOS` exists
   - ensure the three section headers exist in canonical order
   - remove all existing `NEXT_TODO_ID:` lines
   - insert exactly one `NEXT_TODO_ID: <n>` under `## Metadata`
   - preserve the existing schema blockquote and entries where possible

4. Update append behavior so `assign_next_todo_id()` adds new entries under `## Entries`, not just at end of file. This prevents future metadata or schema text from being appended after entries by accident.

5. Route every TODO entry consumer through the shared `## Entries` range:

   - `parse_entries()`
   - `validate_all_entries()`
   - `validate_dependency_refs()`
   - `find_completed_entries()`
   - `extract_entry_blocks()`
   - archive simulation / archive workflow
   - list and revise workflow instructions
   - golden verifier assertions that count entries

   `## Entry Schema` examples are documentation only and must never count as
   active TODOs, dependency targets, archive candidates, or revision targets.

6. Review and update every todos-manager workflow that creates, migrates, audits,
   or mutates `TODOS.md`:

   - `--init`: create the canonical three-section file from scratch, with
     `NEXT_TODO_ID: 1` under `## Metadata`, the format-rules blockquote under
     `## Entry Schema`, and an empty `## Entries` section.
   - `--convert` Mode A: migrate canonical-but-unsectioned files into the
     three-section envelope without rewriting entry bodies.
   - `--convert` Mode B: convert header-based `## Open` / `## Completed` files
     into entries under `## Entries`, preserve non-convertible reference output,
     and set `NEXT_TODO_ID` under `## Metadata`.
   - `--audit`: validate section placement, repair missing/malformed/duplicated/
     misplaced/stale metadata, count only active entries under `## Entries`, and
     report section-layout repairs before schema findings.
   - `--add`: reconcile under the same layout helper, insert only under
     `## Entries`, and advance metadata in the same locked atomic replacement.
   - `--archive`: move only completed entries from `## Entries`, leaving
     `## Metadata` and `## Entry Schema` untouched.
   - `--list` and `--revise`: expose only active entries under `## Entries`.

7. Update docs and fixtures:

   - `TODOS.md`
   - `tests/skill-test-environment/demo-project/TODOS.md`
   - `tests/skill-test-environment/golden/*.yaml`
   - `hermes_pipeline/data/skills/todos-manager/SKILL.md`
   - `hermes_pipeline/data/skills/todos-manager/sections/schema.md`
   - `hermes_pipeline/data/skills/todos-manager/sections/id-assignment.md`
   - README and user docs that mention metadata placement

## Migration Rules

Legacy files without section headers should migrate mechanically:

```text
# TODOS
  -> keep as heading

existing NEXT_TODO_ID line, if valid
  -> move to ## Metadata

existing format-rules blockquote
  -> move under ## Entry Schema

existing TODO list
  -> move under ## Entries
```

If tracked metadata is missing, malformed, duplicated, stale, or conflicts with an existing TODO ID, compute `max(TODOS.md IDs + TODOS-archive.md IDs) + 1` and write that value under `## Metadata`.

## Test Plan

Add focused coverage for:

- valid three-section file reads `NEXT_TODO_ID`
- `NEXT_TODO_ID` under `## Entries` is rejected and repaired
- `NEXT_TODO_ID` under `## Entry Schema` is rejected and repaired
- duplicate metadata across sections is rejected and repaired
- legacy no-section file migrates to three sections
- add path appends the new TODO only under `## Entries`
- audit is idempotent after migration
- CRLF sectioned file still works
- `recover-counter` uses `NEXT_TODO_ID - 1` only when tracked state is valid under `## Metadata`
- `--init` creates the canonical three-section envelope
- `--convert` Mode A preserves existing entry bodies while adding the envelope
- `--convert` Mode B writes converted header-based entries only under `## Entries`
- `--audit` reports and repairs section-layout problems before entry schema findings
- TODO-like examples under `## Entry Schema` are ignored by `parse_entries()`
- schema examples are ignored by audit counts and missing-field validation
- schema examples are ignored by dependency validation
- schema examples are not archived by completed-entry discovery or archive block extraction
- list/revise instructions state that only `## Entries` contains active entries

## Eng Review Decisions

- D1: Proceed with the full three-section grammar instead of a parser-only top-matter validation patch.
- D2: Make entry parsing section-aware too; TODO entries are valid only under `## Entries`.
- D3: Centralize section parsing in one shared layout primitive rather than duplicating boundary logic.
- D4: Add consumer regression tests for parse/list/audit/deps/archive/revise boundaries.
- D5: Treat `--init`, `--convert`, and `--audit` as first-class review targets because they create or normalize the sectioned file.

## Test Coverage Diagram

```text
CODE PATHS                                            USER FLOWS / COMMANDS
[+] parse_todos_document_sections()                  [+] todos-manager --init
  ├── [GAP] valid Metadata/Schema/Entries               ├── [GAP] creates ## Metadata
  ├── [GAP] missing/misordered sections                 ├── [GAP] creates ## Entry Schema
  ├── [GAP] duplicate/misplaced NEXT_TODO_ID            └── [GAP] creates empty ## Entries
  └── [GAP] CRLF boundaries

[+] migration/normalization                           [+] todos-manager --convert
  ├── [GAP] legacy top metadata -> Metadata             ├── [GAP] Mode A preserves entry bodies
  ├── [GAP] blockquote -> Entry Schema                  ├── [GAP] Mode B converts header entries
  └── [GAP] existing entries -> Entries                 └── [GAP] non-convertible reference output

[+] audit/reconciliation                              [+] todos-manager --audit
  ├── [GAP] reports section-layout repairs              ├── [GAP] migrates legacy layout
  ├── [GAP] counts only ## Entries                      ├── [GAP] rejects misplaced metadata
  └── [GAP] idempotent after repair                     └── [GAP] idempotent second audit

[+] read/replace NEXT_TODO_ID                         [+] todos-manager --add
  ├── [GAP] only Metadata value accepted                ├── [GAP] appends under ## Entries
  ├── [GAP] Entry Schema/Entries values rejected        └── [GAP] repairs then assigns on conflict
  └── [GAP] stale/malformed/duplicate repaired

[+] entry consumers                                   [+] archive/list/revise
  ├── [GAP] parse_entries ignores Entry Schema          ├── [GAP] schema examples not listed
  ├── [GAP] validate_all ignores Entry Schema           ├── [GAP] schema examples not archived
  ├── [GAP] dependency validation ignores schema        └── [GAP] schema examples not revisable
  └── [GAP] extract_entry_blocks bounded to Entries

[+] recover-counter
  ├── [GAP] valid Metadata uses NEXT_TODO_ID - 1
  └── [GAP] invalid section placement falls back to scan

COVERAGE TARGET: all listed paths require focused regression coverage before ship.
QUALITY TARGET: behavior + edge + repair-path tests for metadata and entry consumers.
```

## NOT in scope

- Removing `.hermes/todo_id_counter`; it remains compatibility/cache state.
- Reopening the already approved combined TODO-35/TODO-38 branch scope.
- Changing the TODO entry field schema beyond section placement.

## What Already Exists

- Locked atomic TODO replacement exists in the deterministic oracle.
- `recover-counter` already uses tracked `NEXT_TODO_ID` when valid and falls back to scanning.
- Existing tests cover many metadata validation and reconciliation cases, but not the new sectioned consumer boundaries.

## Failure Modes To Handle

- `NEXT_TODO_ID` under `## Entry Schema` or `## Entries` is accepted as tracked state.
- TODO-like examples under `## Entry Schema` are parsed, listed, archived, revised, or used as dependency targets.
- `--init` creates the old unsectioned preamble and every new project starts stale.
- `--convert` migrates metadata but leaves converted entries outside `## Entries`.
- `--audit` reports entry schema errors before repairing section layout, producing noisy or wrong counts.
- Migration reorders or rewrites entry bodies unnecessarily.
- Production `counter.py` and the deterministic oracle disagree on valid tracked state.

## Verification

Run:

```bash
uv run pytest tests/test_counter.py tests/skill-test-environment -q
uv run pytest -q
uv run ruff check .
git diff --check
```

## Risks

- This touches docs, fixtures, parser behavior, and writer behavior together. Keep the diff right-sized, but do not split the grammar from the oracle updates.
- Legacy migration can accidentally reorder prose. Prefer preserving entry bodies exactly and only normalizing the file envelope.
- The production `recover-counter` path and the skill-test oracle must stay aligned. A green oracle alone is not enough if `hermes_pipeline/counter.py` accepts a different grammar.

## Decision

Use the three-section grammar. It is more explicit than top-matter parsing, gives future metadata a home, and makes misplaced state detectable instead of ambiguous.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | - | Not run |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | - | Skipped |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 5 issues found, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | - | Not applicable |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | - | Not run |

- **VERDICT:** ENG CLEARED — ready to implement with D1-D5 folded into the plan.
NO UNRESOLVED DECISIONS
