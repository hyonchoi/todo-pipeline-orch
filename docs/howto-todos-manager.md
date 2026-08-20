# How to manage TODOS.md with the todos-manager skill

This guide covers the eight subcommands of the `todos-manager` skill for adding, converting, auditing, listing, archiving, completing, and revising TODOS.md entries. Each section shows real commands and expected output.

- **`--init`** — performs no Plan operation; it only creates the TODO documents
- **`--add`** — authors a validated manifest when a new or legacy actionable Plan is selected
- **`--convert`** — reports Plan readiness and migration needs while migrating TODO layout
- **`--audit`** — reports `manifest`, `legacy`, `invalid`, or `none` and reconciles tracked ID metadata
- **`--archive`** — preserves attached Plan bytes while moving completed entries
- **`--complete`** — preserves attached Plan bytes during TPO-verified closeout
- **`--list`** — reports Plan readiness for active and archived entries without mutation
- **`--revise`** — create, replace, remove, or convert one Plan for one active TODO

## Prerequisites

- The todos-manager skill installed via `tpo skills install --target all`
- A project with write access to the repo root

`tpo skills install` fails when `todos-manager` is already installed. Use `tpo skills install --reinstall` after reviewing the destination to replace it intentionally. Use `tpo skills uninstall --yes` to remove installed copies.

## Initialize a new project

Create TODOS.md with the canonical sections and a companion TODOS-archive.md:

```bash
todos-manager --init
```

**What it does:**
- Creates `TODOS.md` with `## Metadata`, `## Entry Schema`, and `## Entries`
- Creates `TODOS-archive.md` with a minimal header for completed entries
- Initializes `.hermes/todo_id_counter` to `0` (if `.hermes/` exists)

If TODOS.md already exists, the skill prints a note and skips creation.
It performs no Plan discovery, validation, authoring, or mutation.

**Output:**
```
✓ TODOS.md initialized. Use todos-manager --add to add entries.
```

**Verification:**

```bash
head -10 TODOS.md
```

The file should start with `# TODOS`, followed by `## Metadata`, `## Entry Schema`, and `## Entries`.

## Add a new TODO entry

The `--add` subcommand creates schema-compliant entries with auto-research to pre-fill fields before you type them. After you provide a title and summary, it silently scans the codebase to derive `What`, `Why`, `Decisions`, and optional fields. Only unresolved gaps become prompts.

```bash
todos-manager --add
```

**Interactive workflow:**

1. `TODOS.md` stores tracked ID state under `## Metadata` as `NEXT_TODO_ID: <n>`. The `## Entry Schema` section documents the format and is never parsed as active TODO content. Active entries live under `## Entries`. `todos-manager --add` uses that value on the common path, increments it after a successful write, and reconciles by scanning `TODOS.md` plus `TODOS-archive.md` only when the tracked value is missing, malformed, misplaced, stale, duplicated, or conflicting.
2. Prompts for **title** and **summary**
3. **Auto-research phase** — silently reads TODOS.md, TODOS-archive.md, git log, design docs under `docs/gstack/`, CLAUDE.md, and source files implied by the title. Derives `What`, `Why`, `Pros`, `Cons`, `Context`, `Priority`, `Effort`, `Phase`, `Branch`, `Test Coverage`, `Security Review`, `UI Review`, and `Depends on` from what it finds. Budget capped at 20 file reads and 10 searches.
4. **Gap questions** — for any field research couldn't resolve, asks one question at a time (`Why` first, then `What`, `Priority`, `Effort`, `Depends on`)
5. **Synthesis block** — shows all derived and user-answered fields with confidence tags (high/medium/low):

```
======== AUTO-RESEARCH SYNTHESIS ========
Why:             Prevent API overload under concurrent load    [Confidence: high]
What:            Add rate-limiting middleware to the API server [Confidence: high]
Pros:            Production stability, graceful degradation
Cons:            Migration effort, import path updates
Context:         docs/gstack/api-rate-limiting.md
Priority:        P1                                           [Confidence: high]
Effort:          M                                            [Confidence: medium]
Phase:           4 (Development)                              [Confidence: high]
Branch:          feature/rate-limit                           [Confidence: medium]
Test Coverage:   required                                     [Confidence: high]
Security Review: not-required                                 [Confidence: high]
UI Review:       not-required                                 [Confidence: high]
Depends on:      TODO-6                                       [Confidence: high]
======== END SYNTHESIS ========

These are pre-fills — confirm or edit each in the next step.
```

6. You confirm the synthesis or edit individual fields
7. Shows a preview of the assembled entry (see below)

If the selected actionable Plan already contains a valid `json tpo-plan`
manifest, `--add` attaches it byte-for-byte unchanged. For a new Plan or a
legacy Markdown Plan, the skill proposes a manifest using only digest-checked
evidence, stages it beside the target, and runs:

```bash
tpo plan validate <project> --todo TODO-N --plan <candidate> --require-manifest
```

You approve the Plan source, the exact Plan diff, and the final TODO preview
separately. Cancellation, validation failure, insufficient evidence, or Plan or
TODO drift leaves both files unchanged. Declining legacy migration requires
selecting another manifest-ready Plan, choosing `none` (non-actionable), or
cancelling.

**Preview gate — before writing, you see the full entry:**
```
======== PREVIEW ========
- [ ] **TODO-9: Implement rate-limiting** — Prevent API overload under load
  - **What:** Add rate-limiting middleware to the API server.
  - **Why:** Critical for production stability under concurrent load.
  - **Depends on:** `TODO-6` (authentication middleware)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/rate-limit`, Test Coverage `필요`, Security Review `불필요`, UI Review `불필요`
======== END PREVIEW ========

Proceed? [y / edit / cancel]
```

- **`y`** — writes the entry to TODOS.md
- **`edit`** — returns to the field prompts without burning the ID
- **`cancel`** — aborts; nothing written

**Output on success:**
```
✓ Entry added as TODO-9.
```

**Validation rules:**

| Field | Constraint |
|-------|------------|
| Title | 10–200 characters |
| Summary | 10–100 characters |
| `What` | Non-empty |
| `Why` | 10–200 characters |
| `Decisions` | Must include Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review |
| `Depends on` | Each `TODO-<n>` must exist in TODOS.md or TODOS-archive.md |

## Convert an existing TODOS.md

If your project has a TODOS.md without the canonical sections, migrate it and validate entries:

```bash
todos-manager --convert
```

**What it does:**
1. Checks whether `## Metadata`, `## Entry Schema`, and `## Entries` exist in canonical order
2. Migrates the document to that sectioned layout if needed
3. Scans only entries under `## Entries` for required fields and valid status markers
4. Outputs an audit report listing any issues

**What it does NOT do:** Auto-fill missing fields. Canonical entries keep their bodies unchanged; header-based legacy entries are converted into the canonical entry shape.

Conversion does not edit Plan files. It reports each attachment's readiness as
`manifest`, `legacy`, `invalid`, or `none`, so legacy Plan migration can be
handled later with `--add` or `--revise`.

**Example output:**
```
## TODOS.md Audit Report

Schema version: 2.0
Scanned: TODOS.md (8 entries), TODOS-archive.md (3 entries)
ID range: 1-11

Issues found: 2
- TODO-3: Missing required field **Decisions:**
- TODO-7: Status marker `[->]` — expected `[→]`

NEXT_TODO_ID: 12 (valid)
```

## Audit TODOS.md for compliance

Run a format compliance check. It modifies `TODOS.md` only when it must reconcile missing, malformed, stale, duplicated, or conflicting `NEXT_TODO_ID` metadata:

```bash
todos-manager --audit
```

**Per-entry checks:**
- Required fields (`What`, `Why`, `Decisions`) present
- Status marker is one of `[ ]`, `[→]`, `[x]`, `[~]`
- ID format matches `TODO-<digits>`
- Dependency references exist in TODOS.md or TODOS-archive.md

**Cross-entry checks:**
- ID sequence contiguity (gaps reported, not flagged as errors)
- `NEXT_TODO_ID` is present exactly once under `## Metadata`, valid, and consistent with active and archived IDs; the counter cache (`.hermes/todo_id_counter`) is compatibility state only

The skill outputs a structured report and reconciles missing, malformed, stale, duplicated, or conflicting tracked metadata before reporting.
It also reports each Plan as `manifest`, `legacy`, `invalid`, or `none` without
editing any Plan.

## Archive completed TODOs

Move entries marked `[x]` to TODOS-archive.md, keeping TODOS.md focused on active work:

```bash
todos-manager --archive
```

**What it does:**
1. Scans only `## Entries` in TODOS.md for `[x]` entries (header line plus all sub-bullets)
2. Appends them to TODOS-archive.md
3. Removes them from TODOS.md
4. If TODOS-archive.md doesn't exist, creates it with a header

**Output:**
```
✓ Archived 3 entries to TODOS-archive.md.
```

**Important:** Archived entries are still considered during reconciliation. After archiving TODO-1 through TODO-3, `NEXT_TODO_ID` should still point at `TODO-4` — not `TODO-1`.
Any attached Plan path and file bytes are preserved.

**If no entries are marked `[x]`:**
```
No completed TODOs to archive.
```

## Complete a TPO-verified TODO

After TPO has verified the pull-request handoff, complete exactly one TODO:

```bash
todos-manager --complete
```

The deterministic closeout records the verified outcome and moves the entry to
the archive. It preserves the attached Plan path and Plan bytes; it does not
rewrite the manifest.

## List active TODOs

Show active entries as a formatted table, without modifying any files:

```bash
todos-manager --list
```

**What it does:**
1. Scans only `## Entries` in TODOS.md for entry header lines (`- [ ]`, `- [→]`, `- [x]`, `- [~]`)
2. Extracts status, ID, title, and summary for each entry
3. Reports Plan readiness as `manifest`, `legacy`, `invalid`, or `none`
4. Displays a markdown table sorted by ID ascending

**Example output:**
```
### Active TODOs

| ID | Status | Title | Summary | Plan readiness |
|----|--------|-------|---------|----------------|
| TODO-1 | Pending | Example title | One-line summary | manifest |

Showing 1 active entries.
```

**Include archived entries** with `--all` — also scans TODOS-archive.md and prints a second "Archived TODOs" table below the active one:

```bash
todos-manager --list --all
```

```
Showing 1 active entries. 3 archived entries.
```

If TODOS.md has no entries and `--all` was not passed, the skill prints "No active TODOs found." and exits. This is a report-only subcommand — it never modifies files.

## Revise an existing TODO entry

After `--audit` surfaces entries with missing or weak fields, `--revise` closes the loop by filling gaps with AI-pre-filled values derived from codebase signals. Reuses the auto-research phase from `--add`.

```bash
todos-manager --revise
```

**Interactive workflow:**

1. Prompts for the TODO ID to revise (e.g. `TODO-5`)
2. Validates the ID exists in TODOS.md and is not archived or completed
3. Scans the entry for missing or weak fields (What, Why, Decisions, Pros, Cons, Context, Depends on, Assumptions)
4. **Auto-research phase** — reads relevant files to pre-fill gaps, scoped only to missing or weak fields
5. **Synthesis block** — shows all fields with `(unchanged)` for good fields and `[Confidence: high/medium/low]` for derived values
6. **Confirm or edit** — reply `confirm` to accept all as-is, or edit individual fields with `field: new value`
7. **Preview gate** — shows before/after diff of the full entry. Type `y` to confirm, `edit` to re-edit (no re-research), or `cancel` to abort

Unchanged attachments are preserved byte-for-byte. An explicit attachment
change may create, replace, remove, or convert one Plan for the selected TODO.
Removing the attachment edits only TODOS.md; it never deletes or edits the Plan
file. New or legacy actionable Plans follow the same staged
`tpo plan validate ... --require-manifest`, diff-confirmation, drift-check, and
final-approval sequence used by `--add`.

**Example synthesis block:**
```
======== REVISION SYNTHESIS ========
Status:          [ ] pending                        (unchanged)
What:            Split pipeline_watcher.py into modules per design doc (unchanged)
Why:             Single-file monolith is hard to test and extend.      (unchanged)
Priority:        P1                                    [Confidence: high]
Effort:          M                                     [Confidence: medium]
Phase:           4 (Development)                       [Confidence: medium]
Branch:          feature/modularize-watcher            [Confidence: high]
Test Coverage:   required                              [Confidence: high]
Security Review: not-required                          [Confidence: high]
UI Review:       not-required                          [Confidence: high]
Pros:            Testable modules, clear boundaries    [Confidence: medium]
Cons:            Migration effort, import path updates [Confidence: medium]
Context:         docs/pipeline-modularization-plan.md  [Confidence: high]
Depends on:      TODO-10                               [Confidence: high]
======== END SYNTHESIS ========

Confidence: high = derived from strong codebase signal, medium = inferred from context, low = best guess.
These are pre-fills — confirm or edit each in the next step.
```

**Constraints:**
- Only revises active entries in TODOS.md — archived entries in TODOS-archive.md are never modified
- One entry at a time — user selects by TODO-ID
- If all required fields are present and non-weak, prints "TODO-N has no missing or weak fields. Nothing to revise." and exits

**Output on success:**
```
✓ TODO-5 revised. Updated fields: Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review, Pros, Cons, Context, Depends on.
```

## Verification

After any subcommand, verify the result:

- **`--init`:** `head -20 TODOS.md` shows `## Metadata`, `## Entry Schema`, and `## Entries`; `cat TODOS-archive.md` shows the header
- **`--add`:** Tail of TODOS.md contains the new entry with all required fields
- **`--convert`:** TODOS.md has the canonical sections; canonical entry bodies are unchanged, while header-based legacy entries are converted to the canonical format
- **`--audit`:** A structured report with zero or more issues
- **`--archive`:** TODOS.md has fewer entries; TODOS-archive.md has the moved entries
- **`--complete`:** The verified entry is archived and its attached Plan remains byte-for-byte unchanged
- **`--list`:** A markdown table matching the current entries in TODOS.md (and TODOS-archive.md if `--all`)
- **`--revise`:** The targeted entry in TODOS.md has updated fields; entry order and other entries unchanged

## Troubleshooting

**"TODOS.md not found"**
- First-run on a new project. Run `todos-manager --init`.

**"Title must be 10–200 characters"**
- The title is too short or too long. Provide a descriptive name.

**"Dependency TODO-99 does not exist"**
- The referenced TODO-99 doesn't appear in either TODOS.md or TODOS-archive.md. Verify the ID or remove the dependency.

**"Status marker `[->]` is not recognized"**
- The marker `[->]` is a common ASCII approximation of `[→]`. Use the Unicode arrow `→` (U+2192) for the in-progress marker.

**"Entry discarded"**
- You chose `cancel` at the preview gate. The ID isn't burned — re-run `todos-manager --add` and it will propose the same ID.

## Related

- [Getting started with todos-manager](tutorial-todos-manager.md) — step-by-step walkthrough for first-time users
- [TODOS Manager skill reference](../hermes_pipeline/data/skills/todos-manager/SKILL.md) — full schema, ID rules, and acceptance scenarios
- [Architecture overview](ARCHITECTURE.md) — how the skill fits into the project structure
