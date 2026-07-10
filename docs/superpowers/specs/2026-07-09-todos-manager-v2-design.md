# TODOS Manager v2 — Format Enforcement & Archive Design

**Date:** 2026-07-09
**Status:** Implementation plan written
**Author:** hyonchoi

## Context

The `todos-manager` skill (v2.0.0) has a schema in `SKILL.md` that diverges from the actual `TODOS.md` format. The real format — `What`/`Why`/`Pros`/`Cons`/`Context`/`Depends on`/`Decisions`/`Assumptions` — is richer and better aligned with gstack design review practices. v2 aligns the skill to reality, adds format enforcement, and introduces archiving.

## Design Decisions

- **Canonical schema:** Existing TODOS.md format (not SKILL.md schema)
- **Archiving:** Single `TODOS-archive.md` at repo root (Option A)
- **Enforcement level:** Skill-gated + preamble (Option B)
- **Invocation:** Explicit subcommands (Option A): `--add`, `--init`, `--convert`, `--audit`, `--archive`
- **Archive behavior:** Bulk move all `[x]` entries (Option A)
- **ID source of truth:** File scan (both TODOS.md + archive). `.hermes/todo_id_counter` is a cache, corrected on divergence.

---

## 1. Canonical Schema

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
| **Decisions:** | Key decisions (Priority, Effort, Phase, Branch, Test Coverage, Security Review) | Backtick-delimited values, e.g., `Priority \`P1\`, Effort \`M\`, Phase \`4 (Development)\`, ...` |

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

### ID assignment rules

- Sequential: `next_id = max(all IDs in TODOS.md + TODOS-archive.md) + 1`
- Immutable once committed — even if moved, deferred, or deleted
- No gap filling
- `.hermes/todo_id_counter` is a performance cache, corrected on write if diverged from scan

---

## 2. Skill Workflow — Five Subcommands

### `--init`

Create TODOS.md from scratch:
1. Write preamble (Section 3) + header to `TODOS.md`
2. Create empty `TODOS-archive.md` with minimal header
3. Initialize `.hermes/todo_id_counter` to 0

### `--convert`

Convert existing TODOS.md to enforced format:
1. Insert preamble if absent
2. Validate each entry against schema
3. Report missing/malformed fields — do not rewrite entry bodies or auto-fix

### `--add`

Add new entry with schema enforcement:
1. Compute next ID (scan both TODOS.md + archive)
2. Prompt for required fields (What, Why, Decisions)
3. Prompt for optional fields
4. Assemble entry, show preview gate
5. On confirm: insert into TODOS.md, update counter cache

### `--audit`

Audit TODOS.md for format compliance:
1. Scan all entries in TODOS.md
2. Check required fields present
3. Validate status markers
4. Verify ID sequence, dependency references exist
5. Output structured report (Section 5)

### `--archive`

Move completed TODOs to archive:
1. Scan TODOS.md for `[x]` entries
2. Extract each entry (header + sub-bullets)
3. Append to `TODOS-archive.md` (newest first)
4. Remove from TODOS.md
5. Update timestamps on both files

---

## 3. Preamble

Blockquote at the top of TODOS.md:

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

---

## 4. Archive

- Single file: `TODOS-archive.md` at repo root
- Bulk archive — moves all `[x]` entries in one command
- Entries appended newest-first
- Minimal header: `# TODOS Archive` + date
- ID computation scans archive file for max ID

---

## 5. Audit Report Format

```markdown
## TODOS.md Audit Report

Schema version: 1.0
Scanned: TODOS.md (15 entries), TODOS-archive.md (8 entries)
ID range: 1-23

Issues found: 3
- TODO-4: Missing required field **Decisions:**
- TODO-5: Invalid dependency reference `TODO-99` (not found)
- TODO-14: Status marker `[->]` — expected `[→]`

ID gap check: OK (max=23, counter=23)
```

Report only — no automatic fixes.

---

## Changes Summary

| Artifact | Change |
|----------|--------|
| `TODOS.md` | Add preamble blockquote |
| `TODOS-archive.md` | New file (created by `--init` or `--archive`) |
| `.claude/skills/todos-manager/SKILL.md` | Update schema to match real format, add 5 subcommands, add archive workflow |
| `CLAUDE.md` | Add reference to todos-manager for TODOS.md mutations |
