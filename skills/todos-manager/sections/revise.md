# `--revise` Subcommand — Revise an Existing TODO Entry

## Purpose

Closes the audit-to-fix loop: run `--audit` to find entries with missing or weak fields, then `--revise` to fix them with AI-pre-filled suggestions. Reuses the auto-research phase from `--add` to derive field values from codebase signals.

## Constraints

- Only revises **active** entries in TODOS.md. TODOS-archive.md is never modified.
- One entry at a time — user selects by TODO-ID.
- Reuses `sections/auto-research.md` without modifying it.

## Workflow

1. **Validate context:** Does TODOS.md exist at repo root? If not, print:
   ```
   Error: TODOS.md does not exist.
   Remediation: Run `todos-manager --init` or create the file manually.
   ```
   and exit.

2. **Prompt for TODO-N:** "Enter the TODO ID to revise (e.g. TODO-5):"
   - Validate the ID matches `TODO-<digits>` pattern. If not, print "Invalid TODO ID format. Expected TODO-<digits> (e.g. TODO-5)." and re-prompt.
   - **Lookup order:** Scan TODOS.md first, then TODOS-archive.md.
     - If found in TODOS.md → proceed.
     - If found ONLY in TODOS-archive.md → print "TODO-N is archived. Archived entries cannot be revised." and exit.
     - If found in both files → use the TODOS.md entry (the archive copy is stale).
   - **Reject completed entries:** If the entry in TODOS.md has status `[x]`, print "TODO-N is completed. Completed entries are archived and cannot be revised." and exit.
   - **Reject non-existent entries:** If the ID is not found in either file, print "TODO-N not found in TODOS.md." and exit.

3. **Scan for gaps:** Read the entry text. Check each field:

   **Required fields:**
   - **What:** Missing if absent; weak if present but empty after trimming whitespace.
   - **Why:** Missing if absent; weak if present but fewer than 10 characters after trimming.
   - **Decisions:** Missing if absent; weak if present but missing any of the required sub-keys (Priority, Effort, Phase, Branch, Test Coverage, Security Review).

   **Optional fields** (flag as optional gaps if absent):
   - **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**

   A field is "weak" if present but below minimums (e.g., Why < 10 chars). `--audit` only checks presence; `--revise` applies stricter heuristics because it fills gaps, not just reports them.

   - If **no gaps** (all required fields present and non-weak, and all optional fields present): print "TODO-N has no missing or weak fields. Nothing to revise." and exit.
   - Collect the list of missing or weak fields as `gap_fields`.

4. **Auto-research scoped to gaps:** Read `sections/auto-research.md` and execute the research phase.

   **Layering contract:** This is a behavioral layer on top of `auto-research.md`, not a modification to the file. The scoping logic lives entirely in this section:

   - **Pass to auto-research:**
     (a) The TODO title and summary (used as keywords for signal matching).
     (b) The list of `gap_fields` (fields that are missing or weak).
     (c) The existing entry's current values for `gap_fields` (so derivations can improve on existing content rather than starting blank).
   - **Filter the synthesis:** After auto-research produces its full synthesis block, extract only the rows corresponding to `gap_fields`. The remaining fields retain their existing values.

   - If auto-research fills nothing useful (budget cap hit with zero signals, or no keyword matches for any gap field):
     Print "Auto-research found no signals for the missing fields. Please provide values manually:" and ask the user for each missing required field one at a time, then skip to Step 6.

5. **Present synthesis block:** Show all fields in the same format as `--add`'s synthesis block.

   - Fields that were already good (not in `gap_fields`) show with `(unchanged)` tag and their current value.
   - Fields derived by auto-research show with `[Confidence: high/medium/low]` tags.
   - Fields provided manually by the user (from the auto-research-empty fallback) show with `[Confidence: high]`.
   - **Status** is also shown in the synthesis block, displaying the current status marker.

   Example synthesis block:
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
   Pros:            Testable modules, clear boundaries    [Confidence: medium]
   Cons:            Migration effort, import path updates [Confidence: medium]
   Context:         docs/pipeline-modularization-plan.md  [Confidence: high]
   Depends on:      TODO-10                               [Confidence: high]
   ======== END SYNTHESIS ========

   These are pre-fills — confirm or edit each in the next step.
   ```

6. **Single confirm/edit gate:**
   "Reply `confirm` to accept all as-is, or list edits as `field: new value` — only the fields you mention change."

   - The user may edit any field, including `Status` (e.g., `Status: [→]`).
   - If the reply contains an invalid edit (e.g., bad Depends on ID, out-of-range Decisions value, invalid status marker), report just that field's error and re-prompt for that field only — do not discard the other confirmed edits.

7. **Preview gate:** Show the complete before/after so the user sees the full revised entry:
   ```
   ======== REVISION PREVIEW ========
   BEFORE:
   <current entry text>

   AFTER:
   <revised entry text>

   Proceed? [y / edit / cancel]
   ```
   - `y` → proceed to Step 8
   - `edit` → return to Step 6 (re-prompt for batch edits; does NOT re-trigger auto-research)
   - `cancel` → print "Revision discarded." and exit

8. **Write to TODOS.md:** Replace the entry at its original position, preserving entry order among all entries.
   - Determine entry boundaries using `sections/entry-boundary.md`.
   - Adding fields increases the entry's line count, shifting subsequent entries downward. Line numbers change, but entry order is preserved.
   - If the file is locked or the write fails for any reason, print the error and do not perform a partial write.

9. **Confirm:** "✓ TODO-N revised. Updated fields: [comma-separated list of changed fields]."

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| All fields present and valid | Print "TODO-N has no missing or weak fields. Nothing to revise." and exit |
| User enters non-existent TODO-N | Print "TODO-N not found in TODOS.md." and exit |
| User enters archived TODO-N | Print "TODO-N is archived. Archived entries cannot be revised." and exit |
| User enters completed `[x]` TODO-N | Print "TODO-N is completed. Completed entries are archived and cannot be revised." and exit |
| Auto-research budget cap hit | Log which signals were skipped; treat remaining gaps as user questions |
| Auto-research fills nothing useful | Ask user for each missing required field one at a time |
| Write fails (file locked) | Print error; no partial write |
| Preview `edit` action | Return to Step 6 for batch re-edit; does NOT re-trigger auto-research |
