---
name: todos-manager
description: "TODOS.md 항목 추가 및 관리 — gstack 형식 기반, TODO-<n> 안정 ID 자동 부여, 핵심 결정 사항 사전 정의"
version: 2.1.0
author: hyonchoi
license: MIT
metadata:
  hermes:
    tags: [todos, gstack, planning, pipeline]
    related_skills: [gstack-plan-eng-review, gstack-office-hours]
---

## Purpose

The **todos-manager** skill automates the addition and management of TODOS.md entries in gstack-format projects. It enforces the canonical schema (What/Why/Decisions + optional fields), stable TODO-<n> ID assignment, and provides a preview/confirm gate before writing to disk. Completed TODOs can be archived to keep TODOS.md clean.

### When to use

- Adding a new entry to an existing TODOS.md file (`--add`)
- Initializing TODOS.md in a new project (`--init`)
- Converting an existing TODOS.md to enforced format (`--convert`)
- Auditing TODOS.md for format compliance (`--audit`)
- Archiving completed TODOs to TODOS-archive.md (`--archive`)

### Prerequisite state

- Project has a canonical `TODOS.md` file at the repo root (or create one with `--init`)
- TODOS.md follows the gstack schema (see ## TODOS.md Schema)
- User has write access to TODOS.md

---

## TODOS.md Schema

### File location and format

TODOS.md is stored at the repo root. Each entry occupies a single markdown list item (`- [ ] ...`), with fields as sub-bullets using bold labels.

### Entry header line

```markdown
- [ ] **TODO-<n>: <Title>** — <One-line summary>
```

### Status markers

| Marker | Meaning |
|--------|---------|
| `[ ]` | Pending |
| `[→]` | In progress |
| `[x]` | Done |
| `[~]` | On hold |

### Required fields

| Field | Description | Format |
|-------|-------------|--------|
| **What:** | What needs to be done | Free text |
| **Why:** | Why this task matters | Free text |
| **Decisions:** | Key decisions | Backtick-delimited values: `Priority \`P1\`, Effort \`M\`, Phase \`4 (Development)\`, Branch \`feature/...\`, Test Coverage \`필요/불필요\`, Security Review \`필요/불필요\`` |

### Optional fields

| Field | Description |
|-------|-------------|
| **Pros:** | Benefits |
| **Cons:** | Risks/drawbacks |
| **Context:** | References, design doc pointers, file locations |
| **Depends on:** | Other TODO-<n> references |
| **Assumptions:** | Preconditions |
| **Completed:** | Version + date (set when done) |
| **Resolved design:** | Design decisions (zero or more) |

### Example: complete entry

```markdown
- [ ] TODO-42: refactor pipeline-watcher.py into uv modules
  - **What:** Split `pipeline-watcher.py` into modular Python packages under `hermes_pipeline/`.
  - **Why:** Single-file monolith is hard to test and extend. Modularization unblocks CI integration.
  - **Pros:** Testable modules, shared utilities, clear boundaries
  - **Cons:** Migration effort, import path updates across test suite
  - **Context:** Design lives in [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md)
  - **Depends on:** `TODO-40` (design review finalized)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/modularize-watcher`, Test Coverage `필요`, Security Review `불필요`
```

---

## Stable TODO-<n> ID Assignment

### ID sequencing rule

- IDs are assigned sequentially in **insertion order**, starting from 1.
- Once a TODO-<n> is committed, its ID is **immutable** (even if the entry is moved, deferred, or deleted).
- The next new entry receives `max(all IDs in TODOS.md + TODOS-archive.md) + 1`.
- Archived entries count toward ID computation — do not skip archived IDs.

### Bootstrap algorithm

On each invocation, scan **both** TODOS.md and TODOS-archive.md for existing IDs:

1. **Parse all entries** in TODOS.md using regex `/TODO-(\d+)/g`.
2. **Parse all entries** in TODOS-archive.md (if it exists) using same regex.
3. **Collect used IDs** from both files into a single set.
4. **Compute next ID** as `max(used_ids) + 1`.
5. **If both files are empty:** Start at `TODO-1`.
6. **If IDs are non-contiguous** (e.g., `{1, 2, 5}`), still use `6` for the next entry. Do not attempt to fill gaps.

### Counter cache

`.hermes/todo_id_counter` is a performance cache — not authoritative. On write, update the counter to match the computed value. If the counter exists but diverges from the scan, trust the scan and correct the cache.

---

## First-run Bootstrap (`--init`)

When the user invokes `todos-manager --init` on a project with no TODOS.md:

1. **Check if TODOS.md exists** at repo root.
   - If absent, create TODOS.md with preamble blockquote (see ## Preamble Template below).
2. **Create TODOS-archive.md** at repo root with minimal header:
   ```markdown
   # TODOS Archive

   Completed TODOs, archived via `todos-manager --archive`.
   ```
3. **Initialize `.hermes/todo_id_counter`** to 0 (if `.hermes/` directory exists).
4. **Print:** "✓ TODOS.md initialized. Use `todos-manager --add` to add entries."

## Preamble Template

When creating or converting TODOS.md, use this blockquote as the file header:

```markdown
# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`
```

## Workflow

The skill supports five subcommands. Each has its own workflow below.

### `--add`: Add new entry with schema enforcement

1. **Validate context:** Does TODOS.md exist? If not, prompt to run `--init` first.
2. **Compute next TODO-<n>:** Scan TODOS.md + TODOS-archive.md (see ## Stable TODO-<n> ID Assignment).
   - **Output to user:** "Next ID will be `TODO-<n>`."
3. **Prompt for title:** "Enter the TODO title (required):"
   - Validation: 10–200 characters, non-empty.
4. **Prompt for summary:** "One-line summary after the em dash (required):"
   - Validation: Non-empty, 10–100 characters.
5. **Prompt for required fields:**
   - **What:** "What needs to be done? (required):" — Free text, non-empty.
   - **Why:** "Why does this task matter? (required):" — Free text, 10–200 characters.
   - **Decisions:** Prompt for key decisions — guide user through: Priority (`P0`-`P3`), Effort (`S`/`M`/`L`), Phase, Branch name, Test Coverage (`필요`/`불필요`), Security Review (`필요`/`불필요`).
6. **Prompt for optional fields:**
   - **Pros:** Benefits (optional, free text)
   - **Cons:** Risks/drawbacks (optional, free text)
   - **Context:** References, file pointers (optional, free text)
   - **Depends on:** Comma-separated TODO-<n> references (optional; validate each exists in TODOS.md or TODOS-archive.md)
   - **Assumptions:** Preconditions (optional, free text)
7. **Assemble entry in memory** — Format per ## TODOS.md Schema example. Do **not** write to disk yet.
8. **Preview gate:** Show assembled entry exactly as it will appear:
   ```
   ======== PREVIEW ========
   - [ ] TODO-<n>: <Title> — <Summary>
     - **What:** ...
     - **Why:** ...
     [all fields]
   ======== END PREVIEW ========

   Proceed? [y / edit / cancel]
   ```
   - **`y`** → Proceed to step 9.
   - **`edit`** → Jump back to step 5 (no ID burned, no files written).
   - **`cancel`** → Abort. Print: "Entry discarded."
9. **Write to TODOS.md:** Insert formatted entry at end of file (before blank lines, after last entry).
10. **Update counter cache:** Write next_id to `.hermes/todo_id_counter` if `.hermes/` exists.
11. **Confirm:** "✓ Entry added as TODO-<n>."

### `--convert`: Convert existing TODOS.md to enforced format

1. **Read TODOS.md.** If absent, print "TODOS.md not found. Run `todos-manager --init` first." and exit.
2. **Check for preamble:** If blockquote format rules are absent, insert preamble (see ## Preamble Template) after `# TODOS` header.
3. **Validate each entry:** Scan for TODO-<n> entries. For each entry, check:
   - Required fields present: **What:**, **Why:**, **Decisions:**
   - Status marker is one of `[ ]`, `[→]`, `[x]`, `[~]`
   - ID matches `TODO-<digits>` pattern
4. **Report findings:** Output structured report (see ## Audit Report Format).
5. **Do not auto-fix.** Leave entry bodies as-is. Report only.

### `--audit`: Audit TODOS.md for format compliance

1. **Scan TODOS.md** for all TODO-<n> entries.
2. **Scan TODOS-archive.md** (if exists) for archived TODO-<n> entries.
3. **Per-entry checks:**
   - Required fields: **What:**, **Why:**, **Decisions:** present?
   - Status marker valid?
   - ID format correct?
   - Dependency references (if any) exist in TODOS.md or TODOS-archive.md?
4. **Cross-entry checks:**
   - ID sequence contiguous? (gaps OK, just report)
   - Counter cache (`.hermes/todo_id_counter`) matches max scanned ID?
5. **Output report** per ## Audit Report Format below.
   Report only — no automatic fixes.

### `--archive`: Move completed TODOs to archive

1. **Scan TODOS.md** for `[x]` entries (header line + all sub-bullets until next `- [ ]).
2. **If no `[x]` entries found:** Print "No completed TODOs to archive." and exit.
3. **If TODOS-archive.md does not exist:** Create it with minimal header:
   ```markdown
   # TODOS Archive

   Completed TODOs, archived via `todos-manager --archive`.

   Archived: <ISO-8601 timestamp>
   ```
4. **For each `[x]` entry (newest first by ID):**
   - Extract entry (header line + sub-bullets)
   - Append to end of TODOS-archive.md
5. **Remove archived entries from TODOS.md.**
6. **Confirm:** "✓ Archived N entries to TODOS-archive.md."

---

## Audit Report Format

```markdown
## TODOS.md Audit Report

Schema version: 2.0
Scanned: TODOS.md (N entries), TODOS-archive.md (M entries)
ID range: 1-max_id

Issues found: K
- TODO-X: Missing required field **Decisions:**
- TODO-Y: Invalid dependency reference `TODO-Z` (not found)
- TODO-W: Status marker `[->]` — expected `[→]`

ID gap check: OK (max=23, counter=23)
```

Report only — no automatic fixes.

---

## Error Messages

### T8 convention: Path + remediation verb

Each error message **names the absolute file path** and a one-line action verb. Examples:

```
Error: /path/to/TODOS.md does not exist.
Remediation: Create the file or run `todos-manager --init`.

Error: Title must be 10–200 characters.
Remediation: Edit your input and re-enter the title.

Error: **What:** field is empty.
Remediation: Describe what needs to be done (required).

Error: **Why:** field must be 10–200 characters.
Remediation: Provide a rationale for why this task matters.

Error: **Decisions:** field is missing.
Remediation: Set key decisions: Priority, Effort, Phase, Branch, Test Coverage, Security Review.

Error: Dependency TODO-99 does not exist in TODOS.md or TODOS-archive.md.
Remediation: Check the list of valid IDs or remove TODO-99 from the depends_on list.

Error: Status marker "[->]" is not recognized.
Remediation: Use one of: [ ] pending, [→] in progress, [x] done, [~] on hold.
```

### Error & Rescue Map

| Error | Root Cause | Remediation |
|-------|-----------|-------------|
| TODOS.md not found | First-run on new project | Run `todos-manager --init` |
| Title is empty or too short | Invalid input | Re-enter title (10–200 characters) |
| **What:** is empty | Missing required field | Re-enter What description |
| **Why:** is too short or too long | Invalid input | Re-enter Why (10–200 characters) |
| **Decisions:** is missing | Missing required field | Provide key decisions with backtick-delimited values |
| Dependency TODO-<n> does not exist | Invalid reference | Verify TODO-<n> exists in TODOS.md or archive |
| Invalid status marker | Typo in marker | Use one of: [ ], [→], [x], [~] |

### Observability

The skill logs the following to `.claude/gstack/todos-manager.log`:

```
[2026-06-11T10:30:45Z] todos-manager: start
[2026-06-11T10:30:45Z] todos-manager: bootstrap - scanned 8 existing IDs
[2026-06-11T10:30:45Z] todos-manager: next_id = TODO-9
[2026-06-11T10:30:50Z] todos-manager: user_input - title="Refactor state module"
[2026-06-11T10:30:55Z] todos-manager: user_input - assigned_to="@alice"
[2026-06-11T10:31:00Z] todos-manager: preview - gate reached
[2026-06-11T10:31:02Z] todos-manager: user_action - confirm="edit"
[2026-06-11T10:31:05Z] todos-manager: user_input - title="Refactor state module (v2)"
[2026-06-11T10:31:15Z] todos-manager: preview - gate reached (retry 2)
[2026-06-11T10:31:17Z] todos-manager: user_action - confirm="y"
[2026-06-11T10:31:17Z] todos-manager: write - inserted at line 42
[2026-06-11T10:31:17Z] todos-manager: done - TODO-9 committed
```

---

## Acceptance Scenarios

### Scenario A1: Happy path — `--add` with all fields

**Setup:**
- TODOS.md exists with entries TODO-1 through TODO-5.
- TODOS-archive.md does not exist yet.
- No uncommitted changes in the repo.

**Walkthrough:**
1. User invokes `todos-manager --add`.
2. Skill computes next ID: `TODO-6`.
3. Skill prompts for title; user enters "Implement rate-limiting in API server".
4. Skill prompts for summary; user enters "Prevent API overload under load".
5. Skill prompts for **What:** — user enters description.
6. Skill prompts for **Why:** — user enters "Critical for production stability".
7. Skill guides through Decisions: P1, M, Phase 4, Branch, Test 필요, Security 불필요.
8. Skill prompts for optional fields (Pros, Cons, Context, Depends on, Assumptions).
9. **Skill shows preview gate; user types `y`.**
10. Skill inserts entry at end of TODOS.md.
11. Skill prints "✓ Entry added as TODO-6."

**Expected outcome:**
- TODOS.md contains the new entry formatted per schema.
- Entry uses new field names (What, Why, Decisions, etc.).

---

### Scenario A2: `--init` on new project

**Setup:**
- No TODOS.md or TODOS-archive.md exists.

**Walkthrough:**
1. User invokes `todos-manager --init`.
2. Skill creates TODOS.md with preamble blockquote.
3. Skill creates TODOS-archive.md with minimal header.
4. Skill initializes `.hermes/todo_id_counter` to 0 (if `.hermes/` exists).
5. Skill prints "✓ TODOS.md initialized."

**Expected outcome:**
- TODOS.md exists with preamble blockquote at repo root.
- TODOS-archive.md exists with minimal header.

---

### Scenario A3: `--convert` on existing TODOS.md without preamble

**Setup:**
- TODOS.md exists with entries but no preamble blockquote.

**Walkthrough:**
1. User invokes `todos-manager --convert`.
2. Skill detects missing preamble, inserts it after `# TODOS` header.
3. Skill validates each entry against schema.
4. Skill outputs audit report listing any missing required fields.
5. Skill does not rewrite entry bodies.

**Expected outcome:**
- TODOS.md now has preamble blockquote.
- Entry bodies unchanged.
- Report surfaces any schema violations.

---

### Scenario A4: `--archive` completed TODOs

**Setup:**
- TODOS.md has 15 entries, 10 marked `[x]`.
- TODOS-archive.md does not exist.

**Walkthrough:**
1. User invokes `todos-manager --archive`.
2. Skill scans for `[x]` entries — finds 10.
3. Skill creates TODOS-archive.md with header.
4. Skill moves all 10 entries to TODOS-archive.md (newest first by ID).
5. Skill removes 10 entries from TODOS.md.
6. Skill prints "✓ Archived 10 entries to TODOS-archive.md."

**Expected outcome:**
- TODOS.md has 5 entries (non-completed).
- TODOS-archive.md has 10 entries + header.
- ID computation still considers archived IDs.

---

### Scenario A5: `--audit` with issues found

**Setup:**
- TODOS.md has entries with missing fields and invalid dependencies.

**Walkthrough:**
1. User invokes `todos-manager --audit`.
2. Skill scans all entries, checks required fields, validates dependencies.
3. Skill outputs structured report listing issues.
4. Skill does not modify any files.

**Expected outcome:**
- Report lists all issues (missing fields, invalid deps, marker issues).
- No files modified.
