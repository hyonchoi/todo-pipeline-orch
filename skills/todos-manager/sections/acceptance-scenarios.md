# Acceptance Scenarios

### Scenario A1: Happy path — `--add` with matched design doc (auto-research succeeds)

**Setup:**
- TODOS.md exists with entries TODO-1 through TODO-5.
- TODOS-archive.md does not exist yet.
- No uncommitted changes in the repo.
- A design doc under `docs/gstack/` matches the title's keywords.

**Walkthrough:**
1. User invokes `todos-manager --add`.
2. Skill computes next ID: `TODO-6`.
3. Skill prompts for title; user enters "Implement rate-limiting in API server".
4. Skill prompts for summary; user enters "Prevent API overload under load".
5. **Auto-research (step 4.5):** Skill reads `sections/auto-research.md`, silently collects signals (TODOS.md, TODOS-archive.md, git log, docs/gstack/, CLAUDE.md, related source files), finds the matching design doc, and derives all fields. No gap questions are needed — every field is confidently derived.
6. Skill shows the synthesis block with all fields and confidence tags (`Why`, `Context` marked `high` from the exact design-doc match).
7. Skill presents all fields in one batched message; user replies `confirm`.
8. **Skill shows preview gate; user types `y`.**
9. Skill inserts entry at end of TODOS.md.
10. Skill prints "✓ Entry added as TODO-6."

**Expected outcome:**
- TODOS.md contains the new entry formatted per schema.
- Entry uses new field names (What, Why, Decisions, etc.).
- No user-facing questions were asked beyond title/summary and the final confirm.

---

### Scenario A1b: `--add` with novel title (auto-research finds gaps)

**Setup:**
- TODOS.md exists with entries TODO-1 through TODO-5.
- No design doc or related TODO matches the title's keywords.

**Walkthrough:**
1. User invokes `todos-manager --add` with a title with no codebase matches.
2. Skill computes next ID.
3. Skill prompts for title and summary.
4. **Auto-research (step 4.5):** Skill collects signals but cannot derive `Why`, `Priority`, or `Effort` with confidence.
5. Skill asks gap questions **one at a time**, in priority order: `Why` first, then `Priority`, then `Effort` (skipping any field research already resolved).
6. Skill shows the synthesis block; user-answered fields are tagged `Confidence: high`, defaulted fields are tagged `low`.
7. User replies `confirm` (or edits specific fields).
8. Preview gate → `y` → entry written.

**Expected outcome:**
- Only gap questions appear (no batch of all 10 fields as questions) — one at a time.
- Synthesis block correctly distinguishes user-answered (`high`) from defaulted (`low`) fields.

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
