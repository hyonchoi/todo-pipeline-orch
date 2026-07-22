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

- Adding a new entry to an existing TODOS.md file (`--add`) — auto-researches the codebase to pre-fill fields
- Initializing TODOS.md in a new project (`--init`)
- Converting an existing TODOS.md to enforced format — including migrating `### Title` header-based entries (`--convert`)
- Auditing TODOS.md for format compliance (`--audit`)
- Archiving completed TODOs to TODOS-archive.md (`--archive`)
- Revising an existing TODO entry with AI-pre-filled suggestions (`--revise`)
- Listing active TODO entries (`--list`)

### Prerequisite state

- Project has a canonical `TODOS.md` file at the repo root (or create one with `--init`)
- TODOS.md follows the gstack schema (see `sections/schema.md`)
- User has write access to TODOS.md

---

## Section index — Read each section when its situation applies

This skill is a decision-tree skeleton. Steps below point to on-demand sections.
**Read the section in full before executing its step.**

| When | Read this section |
|------|-------------------|
| Any step references the TODOS.md schema, field definitions, or the Preamble Template | `sections/schema.md` |
| Computing or validating TODO-<n> IDs | `sections/id-assignment.md` |
| Executing `--add` step 4.5 (auto-research) | `sections/auto-research.md` |
| `--convert` detects header-based format (Mode B: `## Open`/`## Completed` + `### Title` entries) | `sections/convert-mode-b.md` |
| Entry boundary parsing (--archive, --revise) | `sections/entry-boundary.md` |
| Executing `--list` | `sections/list.md` |
| Executing `--revise` | `sections/revise.md` |
| Running acceptance tests or verifying behavior | `sections/acceptance-scenarios.md` |
| Audit report format, error messages, or observability | `sections/error-messages.md` |

---

## First-run Bootstrap (`--init`)

When the user invokes `todos-manager --init` on a project with no TODOS.md:

1. **Check if TODOS.md exists** at repo root.
   - If absent, create TODOS.md with preamble blockquote (read `sections/schema.md` for the Preamble Template).
2. **Create TODOS-archive.md** at repo root with minimal header:
   ```markdown
   # TODOS Archive

   Completed TODOs, archived via `todos-manager --archive`.
   ```
3. **Initialize `.hermes/todo_id_counter`** to 0 (if `.hermes/` directory exists).
4. **Print:** "✓ TODOS.md initialized. Use `todos-manager --add` to add entries."

---

## Workflow

The skill supports seven subcommands. Each has its own workflow below.

### `--add`: Add new entry with schema enforcement

1. **Validate context:** Does TODOS.md exist? If not, prompt to run `--init` first.
2. **Compute next TODO-<n>:** Read `sections/id-assignment.md` and scan TODOS.md + TODOS-archive.md.
   - **Output to user:** "Next ID will be `TODO-<n>`."
3. **Prompt for title:** "Enter the TODO title (required):"
   - Validation: 10–200 characters, non-empty.
4. **Prompt for summary:** "One-line summary after the em dash (required):"
   - Validation: Non-empty, 10–100 characters.
5. **Auto-research (step 4.5):** Read `sections/auto-research.md`. Execute the research phase: collect signals, derive field drafts, ask gap questions one at a time, show synthesis block.
6. **Confirm or edit fields** (pre-filled from auto-research):
   - Present all fields from the synthesis block in a single message, in the same
     order shown there, with their `Confidence:` tags. Instruction: "Reply
     `confirm` to accept all as-is, or list edits as `field: new value` — only
     the fields you mention change." This is a chat interface, not a terminal;
     do not ask field-by-field. One round-trip covers all 10 fields.
   - **What:** (required, non-empty)
   - **Why:** (required, 10–200 chars)
   - **Decisions:** Priority, Effort, Phase, Branch, Test Coverage, Security Review — all editable in the same batched reply
   - **Pros:** (optional)
   - **Cons:** (optional)
   - **Context:** (optional)
   - **Depends on:** (optional; validate each TODO-<n> exists in TODOS.md or TODOS-archive.md)
   - **Assumptions:** (optional)
   - If the reply contains an invalid edit (e.g. bad Depends-on ID, out-of-range
     Decisions value), report just that field's error and re-prompt for that
     field only — do not discard the other confirmed edits.
7. **Assemble entry in memory** — format per `sections/schema.md`. Do **not** write to disk yet.
8. **Preview gate:**
   ```
   ======== PREVIEW ========
   - [ ] **TODO-<n>: <Title>** — <Summary>
     - **What:** ...
     - **Why:** ...
     [all fields]
   ======== END PREVIEW ========

   Proceed? [y / edit / cancel]
   ```
   - `y` → proceed to step 9
   - `edit` → return to step 6 (no ID burned, no files written)
   - `cancel` → print "Entry discarded." and exit
9. **Write to TODOS.md:** Insert formatted entry at end of file (after last entry, before trailing blank lines).
10. **Update counter cache:** Write next_id to `.hermes/todo_id_counter` if `.hermes/` exists.
11. **Confirm:** "✓ Entry added as TODO-<n>."

---

### `--convert`: Convert existing TODOS.md to enforced format

1. Read TODOS.md. If absent, print error and exit.
2. Read `sections/schema.md` for the Preamble Template and field definitions.
3. **Detect format:**
   - Canonical entries (`- [ ] TODO-N`) with no preamble → **Mode A**.
   - Header-based sections (`## Open`/`## Completed` with `### Title` entries, no canonical entries) → **Mode B**. Read `sections/convert-mode-b.md` and follow its steps in full.

#### Mode A: Canonical format validation

4a. **Validate each entry:** Scan for TODO-<n> entries. For each entry, check:
    - Required fields present: **What:**, **Why:**, **Decisions:**
    - Status marker is one of `[ ]`, `[→]`, `[x]`, `[~]`
    - ID matches `TODO-<digits>` pattern
5a. **Report findings:** Output structured report (see `sections/error-messages.md`). Report only — no automatic fixes.

---

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
5. **Output report** per `sections/error-messages.md`.
   Report only — no automatic fixes.

---

### `--archive`: Move completed TODOs to archive

1. **Scan TODOS.md** for `[x]` entries. Use `sections/entry-boundary.md` for entry boundary detection.
2. **If no `[x]` entries found:** Print "No completed TODOs to archive." and exit.
3. **If TODOS-archive.md does not exist:** Create it with minimal header:
   ```markdown
   # TODOS Archive

   Completed TODOs, archived via `todos-manager --archive`.

   Archived: <ISO-8601 timestamp>
   ```
4. **For each `[x]` entry (newest first by ID):**
   - Extract entry using `sections/entry-boundary.md`
   - Append to end of TODOS-archive.md
5. **Remove archived entries from TODOS.md.**
6. **Confirm:** "✓ Archived N entries to TODOS-archive.md."

---

### `--revise`: Revise an existing TODO entry with AI-pre-filled suggestions

**Exception:** `**Spec:**` and `**Reference:**` are never AI-pre-filled or auto-detected (e.g. no scanning `docs/pipeline/TODO-<n>-*.md` and offering it as a suggestion) — the user must type these values verbatim. A wrong guessed path is worse than an empty field. These two fields also never appear in `--add`'s auto-research (see step 4.5).

Read `sections/revise.md` and follow its steps in full.

---

### `--list`: List active TODO entries

Read `sections/list.md` and follow its steps in full.

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
[2026-06-11T10:30:55Z] todos-manager: auto-research - derived Why from design doc
[2026-06-11T10:30:57Z] todos-manager: auto-research - gap: Priority (no blocking signal found)
[2026-06-11T10:31:00Z] todos-manager: preview - gate reached
[2026-06-11T10:31:02Z] todos-manager: user_action - confirm="edit"
[2026-06-11T10:31:05Z] todos-manager: user_input - title="Refactor state module (v2)"
[2026-06-11T10:31:15Z] todos-manager: preview - gate reached (retry 2)
[2026-06-11T10:31:17Z] todos-manager: user_action - confirm="y"
[2026-06-11T10:31:17Z] todos-manager: write - inserted at line 42
[2026-06-11T10:31:17Z] todos-manager: done - TODO-9 committed
```
