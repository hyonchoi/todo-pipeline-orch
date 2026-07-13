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
- Converting an existing TODOS.md to enforced format — including migrating `### Title` header-based entries (`--convert`)
- Auditing TODOS.md for format compliance (`--audit`)
- Archiving completed TODOs to TODOS-archive.md (`--archive`)
- Listing active TODO entries (`--list`)

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

The skill supports six subcommands. Each has its own workflow below.

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
   - [ ] **TODO-<n>: <Title>** — <Summary>
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

2. **Ensure preamble:** If blockquote format rules are absent, insert preamble (see ## Preamble Template) after `# TODOS` header.

3. **Detect format mode.** Scan the file for two patterns:
   - **Mode A (canonical):** Lines matching `- (\[[ →x~]\])` that contain `TODO-\d+`
   - **Mode B (header-based):** Lines matching `### ` followed by a title, with `**Field:**` sub-lines
   - Count entries for each mode. If both counts are zero, print "No recognizable entries found in TODOS.md" and exit.
   - If Mode A count > 0, proceed with Mode A (canonical validation below).
   - If Mode A count = 0 AND Mode B count > 0, proceed with Mode B (header-based conversion below).

#### Mode A: Canonical format validation

4a. **Validate each entry:** Scan for TODO-<n> entries. For each entry, check:
    - Required fields present: **What:**, **Why:**, **Decisions:**
    - Status marker is one of `[ ]`, `[→]`, `[x]`, `[~]`
    - ID matches `TODO-<digits>` pattern
5a. **Report findings:** Output structured report (see ## Audit Report Format). Report only — no automatic fixes.

#### Mode B: Header-based format conversion

4b. **Parse entries:**
    - Split the file by `## ` section headers. Each section groups entries.
    - Within each section, find all `### Title` lines. Each marks a new entry.
    - For each entry, collect all `**FieldName:** value` lines until the next `###`, `##`, or EOF.
    - Store: raw title, section name, and a map of field names to values.

5b. **Derive status** for each entry (first matching rule wins):
    - `**Completed:**` field present with non-empty value → `[x]`
    - Title ends with ` — Completed` → `[x]` (strip the suffix from the title)
    - Entry is in `## Completed` section → `[x]`
    - Entry is in `## Open` section → `[ ]`
    - Section name contains "WIP", "Blocked", or "In Progress" → `[→]`
    - Section name contains "Hold", "Deferred", or "Parking" → `[~]`
    - Unknown section → `[ ]` (flag for user review)

6b. **Assign IDs:**
    - Scan TODOS.md + TODOS-archive.md for ALL `TODO-(\d+)` references.
    - Compute `base_id = max(all_ids) + 1`. If no IDs found, `base_id = 1`.
    - Assign IDs sequentially to parsed entries in document order (top to bottom).

7b. **Convertibility gate:** For each entry, check if it has enough content:
    - **Convertible:** entry has both `**What:**` AND `**Why:**` fields
    - **Not convertible:** missing `**What:**` OR `**Why:**` — insufficient context for a meaningful TODO
    - Convertible entries proceed to transformation
    - Non-convertible entries are collected for `TODOS-reference.md`

8b. **Transform fields** for each convertible entry:
    - `**Resolution:**` → `**Resolved design:**` (rename label, preserve value)
    - `**Depends on / blocked by:**` → `**Depends on:**` (rename label, preserve value)
    - All other known fields (**What:**, **Why:**, **Pros:**, **Cons:**, **Context:**, **Assumptions:**, **Completed:**) → direct copy
    - Unknown `**Field:**` labels → preserve as-is
    - If `**Decisions:**` is absent → insert: `- **Decisions:** <<USER-REVIEW>> Priority, Effort, Phase, Branch not yet determined`
    - Build the header line: `- [STATUS] **TODO-<n>: <Title>** — <Summary>` where Summary is the first sentence of `**What:**` (text up to first `. ` or end of field). If `**What:**` is absent for summary, use the first sentence of `**Why:**`. If neither exists, use `No summary available`.

9b. **Backup:** Copy current TODOS.md to `TODOS.md.backup.<YYYY-MM-DD>` (e.g., `TODOS.md.backup.2026-07-13`). If today's backup already exists, skip.

10b. **Preview gate:** Display a structured preview:
    ```
    ======== CONVERSION PREVIEW ========

    File: TODOS.md
    Format detected: Header-based (### entries)
    Entries to convert: <count convertible>
    Base ID: TODO-<base_id> (assigned <base_id> through <last_id>)

    --- Entry mapping ---
    ### Old Title  →  TODO-X: New Title  [status]
    ...

    --- Field transformations ---
      - **Resolution:** → **Resolved design:** (N entries)
      - **Depends on / blocked by:** → **Depends on:** (N entries)
      - **Decisions:** added as default (N entries need user review)

    --- Status derivation ---
      [x] done:    N entries
      [ ] pending: N entries
      [→] WIP:     N entries
      [~] on hold: N entries

    --- Non-convertible entries → TODOS-reference.md ---
      ### Entry Title (missing: What/Why)
      ...

    --- Converted output (first 3 entries shown, use --full for all) ---
    [formatted canonical entries]

    ======== END PREVIEW ========

    Proceed? [y / edit / cancel / --full]
      y      → Apply conversion to TODOS.md
      edit   → Specify entries to skip or modify
      cancel → Abort. No files modified.
      --full → Show all N converted entries in preview
    ```

    - **`y`** → Proceed to step 11b.
    - **`edit`** → Prompt which entries to skip/modify, re-show preview.
    - **`cancel`** → Print "Conversion cancelled. No files modified." and exit.
    - **`--full`** → Show all N entries, then re-prompt for y/edit/cancel.

11b. **Apply conversion on confirm:**
    - Remove `## Open`, `## Completed`, and other section headers used only for grouping.
    - Remove all `### Title` header-based entries from TODOS.md.
    - Preserve any existing canonical `- [ ] TODO-<n>` entries (if hybrid file).
    - Insert all converted entries in canonical format at the end of the file.
    - If there are non-convertible entries, write them to `TODOS-reference.md`:
      ```markdown
      # TODOS Reference

      Entries that could not be auto-converted (missing required fields).
      Use these as reference when adding entries via `todos-manager --add`.

      Generated: <ISO-8601 timestamp> from TODOS.md conversion.

      [original entry text for each non-convertible entry]
      ```
    - Count entries with `<<USER-REVIEW>>` markers.
    - **Confirm:** "✓ Converted N entries to canonical format. M entries saved to TODOS-reference.md. Z entries need user review for <<USER-REVIEW>> markers."

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

1. **Scan TODOS.md** for `[x]` entries (header line + all sub-bullets until next `- [ ]` or `- [→]` or `- [x]` or `- [~]`).
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

### `--list`: List active TODO entries

1. **Validate context:** Does TODOS.md exist? If not, print "TODOS.md not found. Run `todos-manager --init` first." and exit.
2. **Scan TODOS.md** for entry header lines: `- [ ]`, `- [→]`, `- [x]`, or `- [~]` followed by `**TODO-<n>: ...`.
3. **If no entries found in TODOS.md:**
   - If `--all` was passed: skip the active table (do not exit) and continue to step 6 to show archived entries.
   - If `--all` was NOT passed: print "No active TODOs found." and exit.
4. **For each matched entry header line**, extract:
   - Status marker: `[ ]` → Pending, `[→]` → In Progress, `[x]` → Done, `[~]` → On Hold
   - ID: `TODO-<n>`
   - Title: text between `TODO-<n>: ` and the closing `**` bold delimiter (strip `**` markup)
   - Summary: text after ` — ` on the header line. If ` — ` is not present, display `[no summary]`.
   - If any field cannot be extracted from a matching line, display `[not set]` in the corresponding column.
5. **Display output** as a formatted markdown table (entries sorted by ID ascending):
   ```
   ### Active TODOs

   | ID | Status | Title | Summary |
   |----|--------|-------|---------|
   | TODO-1 | Pending | Example title | One-line summary |
   ```
6. **If `--all` flag is present**, also scan TODOS-archive.md (if exists):
   - Apply the same scan and extraction rules as steps 2 and 4 (entry matching and field extraction) to TODOS-archive.md
   - Display as a separate table section labeled "Archived TODOs" below the active table
   - If TODOS-archive.md does not exist or contains no entries, skip the archived section silently
7. **Print summary line:**
   - Without `--all`: "Showing N active entries."
   - With `--all`: "Showing N active entries. M archived entries."

Report only — no files modified.

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

### Scenario A3: `--convert` on existing TODOS.md without preamble (Mode A)

**Setup:**
- TODOS.md exists with canonical `- [ ] TODO-N` entries but no preamble blockquote.

**Walkthrough:**
1. User invokes `todos-manager --convert`.
2. Skill detects Mode A (canonical entries), inserts preamble after `# TODOS` header.
3. Skill validates each entry against schema.
4. Skill outputs audit report listing any missing required fields.
5. Skill does not rewrite entry bodies.

**Expected outcome:**
- TODOS.md now has preamble blockquote.
- Entry bodies unchanged.
- Report surfaces any schema violations.

---

### Scenario A6: `--convert` on header-based TODOS.md (Mode B)

**Setup:**
- TODOS.md has `## Open` / `## Completed` sections with `### Title` entries.
- No `- [ ] TODO-N` entries exist.
- Some entries have `**Resolution:**` instead of `**Resolved design:**`.
- Some entries lack `**Decisions:**`.
- One entry lacks `**Why:**` (non-convertible).

**Walkthrough:**
1. User invokes `todos-manager --convert`.
2. Skill detects Mode B (header-based format), parses all `### Title` entries.
3. Skill derives status: entries in `## Completed` or with `**Completed:**` → `[x]`, entries in `## Open` → `[ ]`.
4. Skill assigns IDs starting from TODO-1.
5. Skill gates convertibility: entry missing `**Why:**` is flagged as non-convertible.
6. Skill transforms fields for convertible entries: `**Resolution:**` → `**Resolved design:**`, inserts default `**Decisions:**` with `<<USER-REVIEW>>` marker.
7. Skill creates `TODOS.md.backup.2026-07-13`.
8. Skill shows preview gate with entry mapping, field transformations, status summary, and non-convertible list.
9. User types `y`.
10. Skill writes preamble + converted entries to TODOS.md, removes section headers.
11. Skill writes non-convertible entry to `TODOS-reference.md`.
12. Skill prints "✓ Converted N entries. 1 entry saved to TODOS-reference.md. Z entries need user review for <<USER-REVIEW>> markers."

**Expected outcome:**
- TODOS.md has preamble blockquote + all entries in canonical `- [ ] TODO-N:` format.
- `## Open` / `## Completed` headers removed.
- `**Resolution:**` renamed to `**Resolved design:**`.
- `**Depends on / blocked by:**` renamed to `**Depends on:**`.
- Missing `**Decisions:**` filled with `<<USER-REVIEW>>` marker.
- Non-convertible entry preserved in `TODOS-reference.md`.
- `TODOS.md.backup.<date>` exists as a safety copy.

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
