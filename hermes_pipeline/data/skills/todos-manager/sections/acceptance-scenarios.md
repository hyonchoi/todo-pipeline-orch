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

### Plan selection scenarios for `--add`

#### Scenario A1c: Zero qualified Plans

**Setup:** Auto-research and attachment discovery find zero qualified Plan
candidates within the shared budget.

**Walkthrough:** The synthesis displays `Plan: none detected`. The user replies
`confirm`, reviews a preview with no `**Plan:**` field, and types `y`.

**Expected outcome:** The entry is written without `Plan:` and remains
non-actionable. No separate Plan prompt appears.

#### Scenario A1d: One candidate and an explicit current-context path

**Setup:** The current task context explicitly names
`docs/gstack/api-rate-limit-plan.md`, which validates as the one relevant Plan
candidate. Its record includes the reason that it matches the TODO scope.

**Walkthrough:** The synthesis displays the one candidate path and relevance
reason as the Plan suggestion. The user replies `confirm` in the existing
batched confirmation, then types `y` at the final preview.

**Expected outcome:** The preview and written entry contain
`**Plan:** docs/gstack/api-rate-limit-plan.md`. The path is selected only by
that combined confirmation, not silently from current context.

#### Scenario A1e: Multiple candidates remain unresolved

**Setup:** Discovery finds multiple candidates: two relevant implementation
plans with their relevance reasons.

**Walkthrough:** The synthesis shows numbered paths, marks Plan unresolved,
and the user replies `confirm`. The skill rejects only Plan and asks for a
number, `Plan: <path>`, or `none`. The user selects one number, then confirms
the unchanged remaining fields and types `y` at the existing preview gate.

**Expected outcome:** No preview is shown while Plan is unresolved. The final
entry contains only the selected normalized Plan path.

#### Scenario A1f: Manual valid untracked Plan path

**Setup:** No suggested Plan is selected. An untracked regular file
`docs/superpowers/cache-rework-plan.md` exists inside the repository.

**Walkthrough:** The user replies
`Plan: docs/superpowers/cache-rework-plan.md`. The skill applies the shared
path validation and does not rerun discovery or general research. The user
then confirms the combined synthesis and types `y`.

**Expected outcome:** The normalized manual path appears in the preview and
written `**Plan:**` field.

#### Scenario A1g: Invalid Plan path correction

**Setup:** Plan is unresolved or being edited.

**Walkthrough:** The user supplies `/tmp/outside-plan.md`. The skill reports
`Error: Attachment path must be repository-relative.` with its shared
remediation, retains the other confirmed fields, and requests only a corrected
Plan value. The user supplies a valid repository-relative path and confirms.

**Expected outcome:** No extra research runs, the invalid value never reaches
the preview, and the corrected normalized value does.

#### Scenario A1h: Budget exhaustion and cancellation

**Setup:** The combined attachment-discovery and auto-research operation hits
the 20-file or 10-search cap before every source is searched.

**Walkthrough:** The synthesis discloses the skipped source and uses only
qualified candidates found before exhaustion. The user cancels at the final
preview instead of typing `y`.

**Expected outcome:** The skill does not exceed the budget, does not make a
new Plan guess after exhaustion, and `TODOS.md` remains byte-for-byte
unchanged after cancellation.

---

### Post-planning attachment scenarios for `--revise`

#### Scenario A1i: Three-session planning attachment

**Setup:** A user creates `TODO-12` with `todos-manager --add`. In a later
session, they invoke an explicit planning skill that creates and finalizes
`docs/gstack/cache-rework-plan.md` and `docs/gstack/cache-rework-spec.md`.
The active TODO has no ordinary gaps and has no existing attachments.

**Walkthrough:** The user invokes `todos-manager --revise`, selects `TODO-12`,
and provides the explicit invoking-skill paths. Attachment discovery validates
those paths first, then uses Git-changed fallback and bounded conventional
search only for remaining candidates. The synthesis offers the plan and spec;
the user confirms them and approves the preview.

**Expected outcome:** Revision does not exit just because ordinary fields have
no gaps. The selected Plan and Spec appear in the preview and are written only
after its existing confirmation and preview gates.

#### Scenario A1j: Combined role, ambiguity, and invalid edit correction

**Setup:** A finalized document strongly qualifies as both an executable Plan
and authoritative Spec. A second qualified Plan candidate also exists.

**Walkthrough:** The synthesis offers `attach as Plan and Spec` for the
combined-role document and leaves Plan unresolved because of the second
candidate. `confirm` is rejected until the user explicitly selects the
combined role or another Plan choice. If the user supplies an invalid
edit-path, the skill reports the shared validation error and re-prompts only
that attachment field.

**Expected outcome:** No candidate is silently selected, the combined document
is not added as a Reference, and the invalid edit never causes discovery or
general research to run again.

#### Scenario A1k: Preservation, replacement, removal, and ordered References

**Setup:** `TODO-12` has `Plan: docs/old-plan.md`,
`Spec: docs/old-spec.md`, and References in this order:
`docs/adr/0001.md, docs/context.md`. The old Spec file is now missing.

**Walkthrough:** The synthesis warns about the invalid existing Spec but marks
all attachment values preserved. The user sends `Plan: replace docs/new-plan.md`,
`Spec: remove`, and `Reference: append docs/context.md, docs/adr/0002.md`.
They later send `Reference: remove docs/context.md` before preview.

**Expected outcome:** The Plan changes only because of `replace`; the invalid
existing Spec warning does not block that unrelated edit and the explicit
`remove` deletes the Spec. Reference append normalizes then deduplicates,
retains pre-existing order, and the explicit removal leaves only
`docs/adr/0001.md, docs/adr/0002.md`. Cancel at the preview writes nothing.

---

### Scenario A2: `--init` on new project

**Setup:**
- No TODOS.md or TODOS-archive.md exists.

**Walkthrough:**
1. User invokes `todos-manager --init`.
2. Skill creates TODOS.md with canonical `## Metadata`, `## Entry Schema`, and `## Entries` sections, setting `NEXT_TODO_ID: 1` under `## Metadata`.
3. Skill creates TODOS-archive.md with minimal header.
4. Skill initializes `.hermes/todo_id_counter` to 0 (if `.hermes/` exists).
5. Skill prints "✓ TODOS.md initialized."

**Expected outcome:**
- TODOS.md exists with `# TODOS`, `## Metadata`, `## Entry Schema`, and `## Entries` in that order.
- `NEXT_TODO_ID: 1` appears under `## Metadata`, and the format-rules blockquote is under `## Entry Schema`.
- TODOS-archive.md exists with minimal header.

---

### Scenario A3: `--convert` on legacy flat TODOS.md (Mode A)

**Setup:**
- TODOS.md exists with canonical `- [ ] TODO-N` entries but without the canonical three-section layout.

**Walkthrough:**
1. User invokes `todos-manager --convert`.
2. Skill detects Mode A (legacy flat entries) and migrates the document to `# TODOS`, `## Metadata`, `## Entry Schema`, and `## Entries`.
3. Skill validates only entries under `## Entries` against the schema.
4. Skill outputs audit report listing any missing required fields.
5. Skill does not rewrite entry bodies.

**Expected outcome:**
- TODOS.md now has the canonical three-section layout with `NEXT_TODO_ID: <n>` under `## Metadata`.
- The schema blockquote is under `## Entry Schema`, and active entries are under `## Entries`.
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
10. Skill writes `# TODOS`, `## Metadata`, `## Entry Schema`, and `## Entries` to TODOS.md, sets `NEXT_TODO_ID` under `## Metadata` to one greater than the highest assigned ID, and removes legacy grouping headers.
11. Skill writes non-convertible entry to `TODOS-reference.md`.
12. Skill prints "✓ Converted N entries. 1 entry saved to TODOS-reference.md. Z entries need user review for <<USER-REVIEW>> markers."

**Expected outcome:**
- TODOS.md has the canonical three-section layout and all converted entries under `## Entries` in canonical `- [ ] TODO-N:` format.
- `## Open` / `## Completed` headers removed.
- `**Resolution:**` renamed to `**Resolved design:**`.
- `**Depends on / blocked by:**` renamed to `**Depends on:**`.
- Missing `**Decisions:**` filled with `<<USER-REVIEW>>` marker.
- Non-convertible entry preserved in `TODOS-reference.md`.
- `TODOS.md.backup.<date>` exists as a safety copy.

---

### Scenario A4: `--archive` completed TODOs

**Setup:**
- TODOS.md has 15 entries under `## Entries`, 10 marked `[x]`.
- TODOS-archive.md does not exist.

**Walkthrough:**
1. User invokes `todos-manager --archive`.
2. Skill scans only `## Entries` for `[x]` entries — finds 10.
3. Skill creates TODOS-archive.md with header.
4. Skill moves all 10 entries to TODOS-archive.md.
5. Skill removes 10 entries from TODOS.md.
6. Skill prints "✓ Archived 10 entries to TODOS-archive.md."

**Expected outcome:**
- TODOS.md has 5 entries (non-completed).
- TODOS-archive.md has 10 entries + header.
- ID computation still considers archived IDs.

---

### Scenario A4b: `--archive` ignores schema examples

**Setup:**
- TODOS.md uses the canonical three-section layout.
- `## Entry Schema` contains a completed example `TODO-99`.
- `## Entries` contains a completed real entry `TODO-3`.
- TODOS-archive.md also uses the canonical three-section layout and contains a schema example plus archived entries.

**Walkthrough:**
1. User invokes `todos-manager --archive`.
2. Skill scans only `## Entries` and selects `TODO-3`.
3. Skill preserves `## Metadata` and `## Entry Schema` in both canonical documents.
4. Skill moves `TODO-3` to the archive and does not move or count `TODO-99`.

**Expected outcome:**
- `TODO-99` remains in the schema documentation and is absent from active/archive entry results.
- `TODO-3` is removed from `## Entries` and appears in the archive's `## Entries` section.
- Schema examples in either document are not parsed or counted; `NEXT_TODO_ID` and schema text are preserved.

---

### Scenario A5: `--audit` with issues found

**Setup:**
- TODOS.md has entries with missing fields and invalid dependencies.

**Walkthrough:**
1. User invokes `todos-manager --audit`.
2. Skill scans only entries under `## Entries`, checks required fields, and validates dependencies.
3. Skill reports and repairs section-layout issues before entry schema findings, then reconciles missing, malformed, stale, duplicated, misplaced, or conflicting `NEXT_TODO_ID` metadata under `## Metadata`.
4. Skill outputs structured report listing issues and the `NEXT_TODO_ID` reconciliation result.

**Expected outcome:**
- Report lists all issues (missing fields, invalid deps, marker issues).
- Missing or invalid tracked metadata is repaired; entry bodies are not modified.

### Tracked ID scenarios

- Legacy migration: a file without `NEXT_TODO_ID` is repaired by `--audit` and before first `--add`.
- Stale low value: `NEXT_TODO_ID: 3` with existing `TODO-7` is corrected to `8`.
- Archive-only max: archived `TODO-9` with active max `TODO-4` is corrected to `10`.
- Failed write: if replacement fails, `TODOS.md` remains byte-for-byte unchanged and `.hermes/todo_id_counter` is not advanced.
- Conflict: if `NEXT_TODO_ID` points to an active TODO, reconciliation scans active plus archive IDs, writes the corrected value, and continues.
- Misplaced metadata: `NEXT_TODO_ID` under `## Entry Schema`, `## Entries`, or outside canonical sections is invalid and is repaired to exactly one line under `## Metadata`.
