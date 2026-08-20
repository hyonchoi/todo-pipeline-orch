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

### Canonical entry scope

Canonical TODOS.md files contain `# TODOS`, `## Metadata`, `## Entry Schema`,
and `## Entries` in that order. Only TODO entries under `## Entries` are active
TODOs. `## Entry Schema` is documentation only: TODO-like examples there must
not be parsed, validated, dependency-resolved, listed, revised, archived, or
counted. All entry consumers must use `sections/entry-boundary.md` and limit
their scan to `## Entries` (or use the documented legacy fallback for a file
not yet converted).

Read `NEXT_TODO_ID` from `## Metadata`. Treat any `NEXT_TODO_ID:` line under
`## Entry Schema`, under `## Entries`, or outside the canonical sections as
invalid tracked state. `## Entry Schema` is documentation only; never count,
list, archive, revise, validate, or use TODO-like examples in that section.

### When to use

- Adding a new entry to an existing TODOS.md file (`--add`) — auto-researches the codebase to pre-fill fields
- Initializing TODOS.md in a new project (`--init`)
- Converting an existing TODOS.md to enforced format — including migrating `### Title` header-based entries (`--convert`)
- Auditing TODOS.md for format compliance (`--audit`)
- Archiving completed TODOs to TODOS-archive.md (`--archive`)
- Revising an existing TODO entry with AI-pre-filled suggestions (`--revise`)
- Listing active TODO entries (`--list`)
- Completing one TODO after a TPO-verified pull-request handoff (`--complete`)

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
| Any step references the TODOS.md schema, field definitions, or canonical document layout | `sections/schema.md` |
| Computing or validating TODO-<n> IDs | `sections/id-assignment.md` |
| Executing `--add` step 4.5 (auto-research) | `sections/auto-research.md` |
| Discovering or validating Plan, Spec, or Reference attachments for `--add` or `--revise` | `sections/document-attachments.md` |
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
   - If absent, create TODOS.md with the canonical three-section layout (read `sections/schema.md`).
2. **Create TODOS-archive.md** at repo root with minimal header:
   ```markdown
   # TODOS Archive

   Completed TODOs, archived via `todos-manager --archive`.
   ```
3. **Initialize `.hermes/todo_id_counter`** to 0 (if `.hermes/` directory exists).
4. **Print:** "✓ TODOS.md initialized. Use `todos-manager --add` to add entries."

`--init` performs no Plan discovery, validation, authoring, or mutation.

---

## Workflow

The skill supports eight subcommands. Each has its own workflow below.

### `--complete`: Deterministic pipeline closeout

This mode is noninteractive and is used only by a TPO closeout card after TPO
has validated the finish result and pull request identity. Invoke the packaged
backend instead of editing or reparsing the entry in the prompt:

```bash
uv run tpo todos complete --project-root . --todo TODO-N --pr N --date YYYY-MM-DD
```

The backend uses the canonical entry parser, changes exactly one `[ ]` or `[→]`
entry to `[x]`, and appends `Completed: PR #N, YYYY-MM-DD`. It is idempotent
only when that exact completion already exists and rejects conflicting state.
It preserves the attached Plan bytes and never edits the Plan file.
The closeout worker then commits only `TODOS.md` in its own commit and pushes;
it never merges, deletes a branch, resets a worktree, or repairs remote drift.

### `--add`: Add new entry with schema enforcement

1. **Validate context:** Does TODOS.md exist? If not, prompt to run `--init` first.
2. **Compute next TODO-<n>:** Read `sections/id-assignment.md`, then read `NEXT_TODO_ID` from `## Metadata`. Reconcile it by scanning entries under `## Entries` in TODOS.md plus TODOS-archive.md only when it is missing, malformed, misplaced, stale, duplicated, or conflicts with an active TODO; repair tracked state by writing exactly one `NEXT_TODO_ID: <n>` line under `## Metadata`.
   - **Output to user:** "Next ID will be `TODO-<n>`."
3. **Prompt for title:** "Enter the TODO title (required):"
   - Validation: 10–200 characters, non-empty.
4. **Prompt for summary:** "One-line summary after the em dash (required):"
   - Validation: Non-empty, 10–100 characters.
5. **Attachment discovery and auto-research (step 4.5):** Read `sections/document-attachments.md` and `sections/auto-research.md`. Validate explicit attachment paths, reserve their reads, and complete attachment discovery before general research. Then collect the remaining research signals under the shared counters and derive field drafts only after attachment discovery. Ask gap questions one at a time and show the combined synthesis block.
   AI research remains authoritative for TODO field synthesis; deterministic
   Plan validation reports execution readiness but never supplies or replaces
   researched fields.
6. **Confirm or edit fields** (pre-filled from auto-research):
   - Present all fields from the synthesis block in a single message, in the same
     order shown there, with their `Confidence:` tags. Instruction: "Reply
     `confirm` to accept all as-is, or list edits as `field: new value` — only
     the fields you mention change." This is a chat interface, not a terminal;
     do not ask field-by-field. One round-trip covers all 10 fields.
   - **What:** (required, non-empty)
   - **Why:** (required, 10–200 chars)
   - **Decisions:** Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review — all editable in the same batched reply
   - **Pros:** (optional)
   - **Cons:** (optional)
   - **Context:** (optional)
   - **Depends on:** (optional; validate each TODO-<n> exists in TODOS.md or TODOS-archive.md)
   - **Assumptions:** (optional)
   - **Plan:**, **Spec:**, **Reference:** (optional attachment rows and states from `sections/document-attachments.md`; validate paths before confirmation)
   - **Plan selection:** Keep this row in the same batched confirmation. With
     zero qualified Plans, show `Plan: none detected` and allow confirmation
     without a Plan. With one candidate, show its path and relevance reason as
     a suggestion; require an explicit candidate selection, `Plan: <path>`, or
     `none` before `confirm`. With multiple candidates,
     show numbered paths and reasons and leave Plan unresolved. In that case,
     `confirm` must reject only Plan and request a selection, `Plan: <path>`,
     or `none`. A manual path uses the shared attachment validation without
     rerunning research. Do not show a Plan field in the preview until it is
     resolved; `none` omits it and permits a non-actionable TODO.
   - If the reply contains an invalid edit (e.g. bad Depends-on ID, out-of-range
     Decisions value), report just that field's error and re-prompt for that
     field only — do not discard the other confirmed edits.
   - After a Plan is selected or supplied and AI research is complete, follow
     the Plan-readiness validation in `sections/document-attachments.md`.
     Display its `manifest`, `legacy`, or `invalid` state in both the combined
     synthesis and the full-entry preview. `none` remains non-actionable. A
     selected valid manifest: attach it byte-for-byte unchanged. A new or
     legacy selection follows manifest authoring; if the user declines
     conversion, require another manifest-ready Plan, `none`, or cancellation.
     An invalid Plan blocks preview until the user selects, supplies, or
     generates a valid Plan (or explicitly resolves Plan as `none`).
7. **Assemble entry in memory** — format per `sections/schema.md`. Do **not** write to disk yet.
8. **Preview gate:**
   ```
   ======== PREVIEW ========
   - [ ] **TODO-<n>: <Title>** — <Summary>
     - **What:** ...
     - **Why:** ...
     [all resolved fields, including a selected or manual Plan, Spec, and Reference]
   ======== END PREVIEW ========

   Proceed? [y / edit / cancel]
   ```
   - `y` → proceed to step 9
   - `edit` → return to step 6 (no ID burned, no files written)
   - `cancel` → print "Entry discarded." and exit
9. **Write to TODOS.md:** Under the TODO write lock, insert the formatted entry under `## Entries` after the last active entry and increment `NEXT_TODO_ID` under `## Metadata` in the same atomic replacement. If replacement fails, leave `TODOS.md` byte-for-byte unchanged.
10. **Update counter cache:** Only after the TODO write succeeds, `.hermes/todo_id_counter` may be updated as compatibility/cache state. It does not decide the next ID.
11. **Confirm:** "✓ Entry added as TODO-<n>."

---

### `--convert`: Convert existing TODOS.md to enforced format

1. Read TODOS.md. If absent, print error and exit.
2. Read `sections/schema.md` for the canonical document layout and field definitions.
3. **Detect format:**
   - Canonical entries (`- [ ] TODO-N`) without the three canonical sections → **Mode A** migration.
   - Header-based sections (`## Open`/`## Completed` with `### Title` entries, no canonical entries) → **Mode B**. Read `sections/convert-mode-b.md` and follow its steps in full.

#### Mode A: Canonical format validation

4a. **Validate each entry:** Scan only the `## Entries` section for TODO-<n> entries. For each entry, check:
    - Required fields present: **What:**, **Why:**, **Decisions:**
    - Status marker is one of `[ ]`, `[→]`, `[x]`, `[~]`
    - ID matches `TODO-<digits>` pattern
5a. **Report findings:** Report and repair section-layout issues before entry schema findings. Repair missing, malformed, duplicated, misplaced, stale, or conflicting `NEXT_TODO_ID` metadata by writing exactly one `NEXT_TODO_ID: <n>` line under `## Metadata`. Do not rewrite entry bodies.
6a. **Report Plan readiness only:** Classify each attached Plan as `manifest`,
    `legacy`, `invalid`, or `none`. Conversion may migrate TODO layout but does
    not edit any Plan or author a manifest.

---

### `--audit`: Audit TODOS.md for format compliance

1. **Scan only the `## Entries` section of TODOS.md** for active TODO-<n> entries.
2. **Scan only the `## Entries` section of TODOS-archive.md** (if it uses the canonical layout) for archived TODO-<n> entries.
3. **Per-entry checks:**
   - Required fields: **What:**, **Why:**, **Decisions:** present?
   - Status marker valid?
   - ID format correct?
   - Dependency references (if any) exist in TODOS.md or TODOS-archive.md?
   - Validate every present attachment value using
     `sections/document-attachments.md`: validate Plan and Spec as single paths.
     For Reference, treat every stored comma as a separator, trim and validate
     each non-empty item independently, and report empty items without stopping
     validation of the remaining items. Because stored
     Reference text has no escaping syntax, never infer a literal-comma path
     after splitting; literal-comma candidates are rejected before storage.
     Report one path-specific finding per defect that the stored representation
     can express. Identify the TODO ID, attachment role,
     stored path, and exact defect for missing files, directory targets,
     containment or traversal escape, outside-target symlinks, and empty
     Reference items. Attachments remain optional: `--audit` must
     never require, remove, replace, or repair attachments.
   - Report each Plan as `manifest`, `legacy`, `invalid`, or `none`; do not edit
     any Plan while auditing.
4. **Reconcile tracked state:** Report and repair section-layout issues before entry schema findings. Repair missing, malformed, duplicated, misplaced, stale, or conflicting `NEXT_TODO_ID` metadata by writing exactly one `NEXT_TODO_ID: <n>` line under `## Metadata`.
5. **Cross-entry checks:**
   - ID sequence contiguous? (gaps OK, just report)
   - Counter cache (`.hermes/todo_id_counter`) is compatibility/cache state only.
6. **Output report** per `sections/error-messages.md`, including the `NEXT_TODO_ID` reconciliation result.

`--audit` reports each Plan as `manifest`, `legacy`, `invalid`, or `none` and
does not edit any Plan.

---

### `--archive`: Move completed TODOs to archive

Archive entry text verbatim so the operation and any recovery preserve every attached Plan byte-for-byte;
never inspect, edit, move, or delete the Plan file.

1. **Scan only the `## Entries` section of TODOS.md** for `[x]` entries. Use `sections/entry-boundary.md` for entry boundary detection.
2. **If no `[x]` entries found:** Print "No completed TODOs to archive." and exit.
3. **If TODOS-archive.md does not exist:** Create it with minimal header:
   ```markdown
   # TODOS Archive

   Completed TODOs, archived via `todos-manager --archive`.

   Archived: <ISO-8601 timestamp>
   ```
4. **Under the TODO write lock, compute both final files before writing:**
   - Extract `[x]` entries using `sections/entry-boundary.md`
   - Build the new TODOS-archive.md text with completed entries appended
   - Build the new TODOS.md text with those completed entries removed
5. **Write both files as one recoverable transaction:**
   - Write durable payload files for the intended final TODOS.md and
     TODOS-archive.md contents
   - Write a transaction journal only after both payloads are fsynced
   - Replace TODOS-archive.md, then TODOS.md, using same-directory temp files and
     atomic `os.replace`-style replacement
   - If a journal exists at the start of any TODO write, roll it forward before
     reading current TODO state
   - Remove the journal and payload files only after both target files are replaced
6. **Confirm:** "✓ Archived N entries to TODOS-archive.md."

---

### `--revise`: Revise an existing TODO entry with AI-pre-filled suggestions

Read `sections/revise.md` and `sections/document-attachments.md`, then follow
their steps in full.

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

NEXT_TODO_ID: 24 (valid)
ID gap check: OK (max=23)
```

`--audit` reports and repairs section-layout issues before entry schema findings. It repairs missing, malformed, duplicated, misplaced, stale, or conflicting `NEXT_TODO_ID` metadata by writing exactly one `NEXT_TODO_ID: <n>` line under `## Metadata`.

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
Remediation: Set key decisions: Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review.

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
