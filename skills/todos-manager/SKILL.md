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

The **todos-manager** skill automates the addition and management of TODOS.md entries in gstack-format projects. It enforces stable TODO-<n> ID assignment, captures key decisions upfront (Assigned To, Estimate, Rationale), and provides a workflow with a preview/confirm gate before writing to disk.

### When to use

- Adding a new entry to an existing TODOS.md file
- Batch-importing entries from a plan document or PR
- Ensuring consistent TODO-<n> ID sequencing across team checkpoints
- Capturing pre-defined decisions (assignee, estimate, rationale) before workflow execution
- (Future) Syncing TODOS.md with gstack project metadata (PRD, design doc, eng-review)

### Prerequisite state

- Project has a canonical `TODOS.md` file at the repo root (or `docs/gstack/TODOS.md`)
- TODOS.md follows the gstack schema (see ## TODOS.md Schema)
- User has write access to TODOS.md and `.claude/gstack/` metadata

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
Error: /Users/hyonchoi/Personal/todo-pipeline-orchestrator/TODOS.md does not exist.
Remediation: Create the file or run `todos-manager --init`.

Error: /Users/hyonchoi/Personal/todo-pipeline-orchestrator/.claude/gstack is not writable.
Remediation: Check directory permissions or create the directory.

Error: Title must be 10–200 characters.
Remediation: Edit your input and re-enter the title.

Error: Estimate "5" does not match expected format (e.g., "2h", "1d").
Remediation: Set estimate to one of: 1h, 2h, 1d, 2d, 1w, etc.

Error: Dependency TODO-99 does not exist in TODOS.md.
Remediation: Check the list of valid IDs or remove TODO-99 from the depends_on list.

Error: Status "in_progress" is not recognized.
Remediation: Set status to one of: active, blocked, done, deferred.
```

### Error & Rescue Map

| Error | Root Cause | Remediation |
|-------|-----------|-------------|
| TODOS.md not found | First-run on new project | Run `todos-manager --init` or create TODOS.md at repo root |
| `.claude/gstack/` not writable | Permission issue | Check directory permissions with `ls -ld` |
| Title is empty or too short | Invalid input | Re-enter title (10–200 characters) |
| Assigned To is empty | Invalid input | Re-enter assignee (@handle or name) |
| Estimate does not match `/^\d+[hd w]$/` | Invalid format | Re-enter estimate (e.g., "2h", "1d", "1w") |
| Rationale is too short or too long | Invalid input | Re-enter rationale (10–150 characters) |
| Status is not in `[active, blocked, done, deferred]` | Invalid enum | Re-enter status from allowed list |
| Dependency TODO-<n> does not exist | Invalid reference | Verify TODO-<n> is in TODOS.md; remove or correct |
| Entry preview differs from disk | Data corruption | Inspect TODOS.md and re-run step 8 |

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

### Scenario A1: Happy path — new entry with all fields

**Setup:**
- TODOS.md exists with entries TODO-1 through TODO-5.
- `.claude/gstack/` is writable.
- No uncommitted changes in the repo.

**Walkthrough:**
1. User invokes todos-manager skill.
2. Bootstrap computes next ID: `TODO-6`.
3. Skill prompts for title; user enters "Implement rate-limiting in API server".
4. Skill prompts for assignee; user enters "@bob".
5. Skill prompts for estimate; user enters "3d".
6. Skill prompts for rationale; user enters "Critical for production stability".
7. Skill prompts for status; user accepts default "active".
8. Skill prompts for dependencies; user enters "TODO-2, TODO-4".
9. Skill prompts for notes; user enters "Coordinate with DevOps team" then types `END`.
10. **Skill shows preview gate; user types `y`.**
11. Skill inserts entry into the "In Progress" section of TODOS.md.
12. Skill prints "✓ Entry added as TODO-6."

**Expected outcome:**
- TODOS.md contains the new entry at line N (after section header).
- Entry is formatted exactly as specified in ## TODOS.md Schema.
- Last-updated timestamp is current.
- Log entry in `.claude/gstack/todos-manager.log` records the full flow.

---

### Scenario A2: Edit on preview gate

**Setup:**
- Same as A1.

**Walkthrough:**
1. Steps 1–10 proceed as in A1.
2. **At preview gate, user types `edit`.**
3. **Skill jumps back to step 4 (metadata prompts).**
4. Skill re-prompts for Assigned To; user changes "@bob" to "@carol".
5. Skill re-prompts for remaining fields; user accepts all defaults.
6. **Skill shows preview gate again (retry 2); user types `y`.**
7. Entry is inserted with "@carol" as assignee.

**Expected outcome:**
- TODOS.md is unchanged until the final `y` at the second preview gate.
- Assigned To field shows "@carol" (not "@bob").
- Log contains two preview-gate attempts.

---

### Scenario A3: Cancel on preview gate

**Setup:**
- Same as A1.

**Walkthrough:**
1. Steps 1–10 proceed as in A1.
2. **At preview gate, user types `cancel`.**
3. **Skill aborts and prints "Entry discarded."**
4. No ID is reserved; next entry will still be TODO-6.
5. No files are modified.
6. No Slack notification is sent.

**Expected outcome:**
- TODOS.md is unchanged.
- Log contains a `user_action - confirm="cancel"` entry.
- If user re-invokes the skill immediately, next ID is still TODO-6.

---

### Scenario A4: Invalid dependency — error + recovery

**Setup:**
- TODOS.md has entries TODO-1 through TODO-5.

**Walkthrough:**
1. Steps 1–7 proceed; user enters "TODO-1, TODO-99" in the depends_on field.
2. **Skill detects TODO-99 does not exist in TODOS.md.**
3. **Skill prints error message** (following T8 convention):
   ```
   Error: Dependency TODO-99 does not exist in TODOS.md.
   Remediation: Check the list of valid IDs or remove TODO-99 from the depends_on list.
   Valid IDs in this project: TODO-1, TODO-2, TODO-3, TODO-4, TODO-5
   ```
4. **Skill re-prompts for Depends on field.**
5. User enters "TODO-1" (corrected).
6. Workflow continues; preview gate is shown with corrected depends_on.

**Expected outcome:**
- Invalid ID is detected before preview gate.
- Valid ID list is provided for user reference.
- Corrected entry proceeds to preview gate and is committed.

---

### Scenario A5: Estimate validation — retry with correct format

**Setup:**
- Same as A1.

**Walkthrough:**
1. Steps 1–5 proceed; skill prompts for estimate.
2. User enters "5" (invalid format; expected `/^\d+[hd w]$/`).
3. **Skill prints error message:**
   ```
   Error: Estimate "5" does not match expected format (e.g., "2h", "1d").
   Remediation: Set estimate to one of: 1h, 2h, 1d, 2d, 1w, etc.
   ```
4. **Skill re-prompts for estimate.**
5. User enters "5d" (valid).
6. Workflow continues.

**Expected outcome:**
- Invalid input is caught immediately.
- User is re-prompted for the same field.
- Next iteration proceeds with valid input.

---

### Scenario A6: Empty TODOS.md on first run

**Setup:**
- TODOS.md does not exist.
- User is running todos-manager for the first time.

**Walkthrough:**
1. **Skill detects TODOS.md is missing.**
2. **Skill initializes TODOS.md** with gstack header:
   ```markdown
   # TODOS.md

   > Generated by [todos-manager skill](/.claude/skills/todos-manager)
   > Last updated: 2026-06-11T10:30:45Z

   ## In Progress

   ## Blocked

   ## Done
   ```
3. **Skill assigns TODO-1** to the first entry.
4. Skill prompts for entry title, proceeding as in A1.
5. Entry is inserted under "In Progress" section.

**Expected outcome:**
- TODOS.md is created at repo root with proper structure.
- First entry is TODO-1.
- All subsequent prompts and validation proceed normally.
