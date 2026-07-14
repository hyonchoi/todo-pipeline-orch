# How to manage TODOS.md with the todos-manager skill

This guide covers the six subcommands of the `todos-manager` skill for adding, converting, auditing, listing, and archiving TODOS.md entries. Each section shows real commands and expected output.

- **`--init`** — create TODOS.md with schema preamble and TODOS-archive.md
- **`--add`** — add a new entry with field prompts and a preview gate
- **`--convert`** — add the schema preamble to an existing TODOS.md
- **`--audit`** — check format compliance without modifying files
- **`--archive`** — move completed `[x]` entries to TODOS-archive.md
- **`--list`** — display active TODO entries as a table (`--all` also shows archived)

## Prerequisites

- The todos-manager skill installed via `scripts/install-todos-manager.sh`
- A project with write access to the repo root

## Initialize a new project

Create TODOS.md with the enforced schema preamble and a companion TODOS-archive.md:

```bash
todos-manager --init
```

**What it does:**
- Creates `TODOS.md` with a `# TODOS` header and a blockquote preamble documenting the schema rules
- Creates `TODOS-archive.md` with a minimal header for completed entries
- Initializes `.hermes/todo_id_counter` to `0` (if `.hermes/` exists)

If TODOS.md already exists, the skill prints a note and skips creation.

**Output:**
```
✓ TODOS.md initialized. Use todos-manager --add to add entries.
```

**Verification:**

```bash
head -10 TODOS.md
```

The file should start with the `# TODOS` header followed by a blockquote with format rules.

## Add a new TODO entry

The `--add` subcommand creates schema-compliant entries with auto-research to pre-fill fields before you type them. After you provide a title and summary, it silently scans the codebase to derive `What`, `Why`, `Decisions`, and optional fields. Only unresolved gaps become prompts.

```bash
todos-manager --add
```

**Interactive workflow:**

1. The skill computes the next `TODO-<n>` ID by scanning both TODOS.md and TODOS-archive.md
2. Prompts for **title** and **summary**
3. **Auto-research phase** — silently reads TODOS.md, TODOS-archive.md, git log, design docs under `docs/gstack/`, CLAUDE.md, and source files implied by the title. Derives `What`, `Why`, `Pros`, `Cons`, `Context`, `Priority`, `Effort`, `Phase`, `Branch`, `Test Coverage`, `Security Review`, and `Depends on` from what it finds. Budget capped at 20 file reads and 10 searches.
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
Depends on:      TODO-6                                       [Confidence: high]
======== END SYNTHESIS ========

These are pre-fills — confirm or edit each in the next step.
```

6. You confirm the synthesis or edit individual fields
7. Shows a preview of the assembled entry (see below)

**Preview gate — before writing, you see the full entry:**
```
======== PREVIEW ========
- [ ] **TODO-9: Implement rate-limiting** — Prevent API overload under load
  - **What:** Add rate-limiting middleware to the API server.
  - **Why:** Critical for production stability under concurrent load.
  - **Depends on:** `TODO-6` (authentication middleware)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/rate-limit`, Test Coverage `필요`, Security Review `불필요`
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
| `Decisions` | Must include Priority, Effort, Phase, Branch, Test Coverage, Security Review |
| `Depends on` | Each `TODO-<n>` must exist in TODOS.md or TODOS-archive.md |

## Convert an existing TODOS.md

If your project has a TODOS.md without the enforced schema preamble, add it and validate entries:

```bash
todos-manager --convert
```

**What it does:**
1. Checks whether the preamble blockquote exists
2. Inserts the preamble after `# TODOS` if absent
3. Scans each entry for required fields and valid status markers
4. Outputs an audit report listing any issues

**What it does NOT do:** Rewrite entry bodies or auto-fix missing fields. It reports only.

**Example output:**
```
## TODOS.md Audit Report

Schema version: 2.0
Scanned: TODOS.md (8 entries), TODOS-archive.md (3 entries)
ID range: 1-11

Issues found: 2
- TODO-3: Missing required field **Decisions:**
- TODO-7: Status marker `[->]` — expected `[→]`

ID gap check: OK (max=11, counter=11)
```

## Audit TODOS.md for compliance

Run a format compliance check without modifying any files:

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
- Counter cache (`.hermes/todo_id_counter`) matches max scanned ID

The skill outputs a structured report and modifies no files.

## Archive completed TODOs

Move entries marked `[x]` to TODOS-archive.md, keeping TODOS.md focused on active work:

```bash
todos-manager --archive
```

**What it does:**
1. Scans TODOS.md for `[x]` entries (header line plus all sub-bullets)
2. Appends them to TODOS-archive.md, newest first by ID
3. Removes them from TODOS.md
4. If TODOS-archive.md doesn't exist, creates it with a header

**Output:**
```
✓ Archived 3 entries to TODOS-archive.md.
```

**Important:** Archived entries count toward ID computation. After archiving TODO-1 through TODO-3, the next `todos-manager --add` will use `TODO-4` — not `TODO-1`.

**If no entries are marked `[x]`:**
```
No completed TODOs to archive.
```

## List active TODOs

Show active entries as a formatted table, without modifying any files:

```bash
todos-manager --list
```

**What it does:**
1. Scans TODOS.md for entry header lines (`- [ ]`, `- [→]`, `- [x]`, `- [~]`)
2. Extracts status, ID, title, and summary for each entry
3. Displays a markdown table sorted by ID ascending

**Example output:**
```
### Active TODOs

| ID | Status | Title | Summary |
|----|--------|-------|---------|
| TODO-1 | Pending | Example title | One-line summary |

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

## Verification

After any subcommand, verify the result:

- **`--init`:** `head -10 TODOS.md` shows the preamble blockquote; `cat TODOS-archive.md` shows the header
- **`--add`:** Tail of TODOS.md contains the new entry with all required fields
- **`--convert`:** TODOS.md has the preamble; entry bodies are unchanged
- **`--audit`:** A structured report with zero or more issues
- **`--archive`:** TODOS.md has fewer entries; TODOS-archive.md has the moved entries
- **`--list`:** A markdown table matching the current entries in TODOS.md (and TODOS-archive.md if `--all`)

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
- [TODOS Manager skill reference](../skills/todos-manager/SKILL.md) — full schema, ID rules, and acceptance scenarios
- [Architecture overview](ARCHITECTURE.md) — how the skill fits into the project structure
