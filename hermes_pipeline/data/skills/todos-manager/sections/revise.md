# `--revise` Subcommand — Revise an Existing TODO Entry

## Purpose

Closes the audit-to-fix loop: run `--audit` to find entries with missing or weak fields, then `--revise` to fix them with AI-pre-filled suggestions. Reuses the auto-research phase from `--add` to derive field values from codebase signals.

## Constraints

- Only entries under `## Entries` are active TODO entries. Ignore TODO-like text
  under `## Entry Schema` and anywhere outside `## Entries`.
- Only revises **active** entries in TODOS.md. TODOS-archive.md is never modified.
- One entry at a time — user selects by TODO-ID.
- Always uses `sections/document-attachments.md`; uses
  `sections/auto-research.md` only when ordinary TODO fields need research.

## Workflow

1. **Validate context:** Does TODOS.md exist at repo root? If not, print:
   ```
   Error: TODOS.md does not exist.
   Remediation: Run `todos-manager --init` or create the file manually.
   ```
   and exit.

2. **Prompt for TODO-N:** "Enter the TODO ID to revise (e.g. TODO-5):"
   - Validate the ID matches `TODO-<digits>` pattern. If not, print "Invalid TODO ID format. Expected TODO-<digits> (e.g. TODO-5)." and re-prompt.
   - **Lookup order:** Scan only `## Entries` in TODOS.md first, then only `## Entries` in TODOS-archive.md when those files use the canonical layout. Ignore TODO-like examples in `## Entry Schema`.
     - If found in TODOS.md → proceed.
     - If found ONLY in TODOS-archive.md → print "TODO-N is archived. Archived entries cannot be revised." and exit.
     - If found in both files → use the TODOS.md entry (the archive copy is stale).
   - **Reject completed entries:** If the entry in TODOS.md has status `[x]`, print "TODO-N is completed. Completed entries are archived and cannot be revised." and exit.
   - **Reject non-existent entries:** If the ID is not found in either file, print "TODO-N not found in TODOS.md." and exit.

3. **Scan for gaps:** Read the entry text. Check each field:

   **Required fields:**
   - **What:** Missing if absent; weak if present but empty after trimming whitespace.
   - **Why:** Missing if absent; weak if present but fewer than 10 characters after trimming.
   - **Decisions:** Missing if absent; weak if present but missing any of the required sub-keys (Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review).

   **Optional fields** (flag as optional gaps if absent):
   - **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**

   A field is "weak" if present but below minimums (e.g., Why < 10 chars). `--audit` only checks presence; `--revise` applies stricter heuristics because it fills gaps, not just reports them.

   - If **no ordinary field gaps** (all required fields present and non-weak),
     do not exit yet; continue to attachment discovery in Step 4.
   - If only optional fields are missing (required fields are present and non-weak): skip auto-research for required fields; present a summary of the entry and ask whether to fill optional gaps. If the user declines, retain those fields unchanged and continue to attachment discovery.
   - Collect the list of missing or weak fields as `gap_fields`.

4. **Attachment discovery, then auto-research scoped to gaps:** Always run document attachment discovery by reading `sections/document-attachments.md`, even when ordinary fields have no gaps. Validate explicit attachment paths, reserve their reads, and complete bounded attachment discovery before using the remaining shared budget for general research.

   - If `gap_fields` is non-empty, read `sections/auto-research.md` and execute
     its research phase with the shared counters after attachment discovery.
   - If `gap_fields` is empty, skip general auto-research and proceed with the
     attachment candidates and states.
   - Do not take the no-gaps exit before the attachment rows have been shown at
     the confirm/edit gate: a user may invoke `--revise` specifically to attach
     a planning output created after the TODO. Exit with "TODO-N has no missing
     or weak fields. Nothing to revise." only when `gap_fields` is empty, no
     qualified attachment candidate was found, and the user explicitly makes
     no attachment replacement, removal, or addition.

   **Layering contract:** Gap scoping remains a behavioral layer on top of
   `auto-research.md`; attachment discovery is the shared pre-research phase:

   - **Pass to auto-research:**
     (a) The TODO title and summary (used as keywords for signal matching).
     (b) The list of `gap_fields` (fields that are missing or weak).
     (c) The existing entry's current values for `gap_fields` (so derivations can improve on existing content rather than starting blank).
   - **Filter the synthesis:** After auto-research produces its full synthesis block, extract only the rows corresponding to `gap_fields`. The remaining fields retain their existing values.

   - If `gap_fields` is non-empty and auto-research fills nothing useful (budget cap hit with zero signals, or no keyword matches for any gap field):
     Print "Auto-research found no signals for the missing fields. Please provide values manually:" and ask the user for each missing required field one at a time, then skip to Step 6.

5. **Present synthesis block:** Show all fields in the same format as `--add`'s synthesis block.

   - Fields that were already good (not in `gap_fields`) show with `(unchanged)` tag and their current value.
   - Fields derived by auto-research show with `[Confidence: high/medium/low]` tags.
   - Fields provided manually by the user (from the auto-research-empty fallback) show with `[Confidence: high]`.
   - Add **Plan**, **Spec**, and **Reference** revision rows from the shared
     attachment policy. Each row shows its normalized path value and
     `suggested`, `unresolved`, `none detected`, or `preserved` state. Existing
     values stay `preserved` unless the user supplies an explicit mutation.
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
   UI Review:       not-required                          [Confidence: high]
   Pros:            Testable modules, clear boundaries    [Confidence: medium]
   Cons:            Migration effort, import path updates [Confidence: medium]
   Context:         docs/pipeline-modularization-plan.md  [Confidence: high]
   Depends on:      TODO-10                               [Confidence: high]
   Plan:            docs/gstack/feature-plan.md           [suggested]
   Spec:            (none detected)
   Reference:       docs/adr/0001-example.md              [preserved]
   ======== END SYNTHESIS ========

   Confidence: high = derived from strong codebase signal, medium = inferred from context, low = best guess.
   These are pre-fills — confirm or edit each in the next step.
   ```

6. **Single confirm/edit gate:**
   "Reply `confirm` to accept all as-is, or list edits as `field: new value` — only the fields you mention change."

   - The user may edit any field, including `Status` (e.g., `Status: [→]`) and
     Plan, Spec, or Reference. Validate attachment edits with
     `sections/document-attachments.md`.
   - Preserve an existing singleton `Plan` or `Spec` by default. Mutate one
     only with `Plan: replace <path>`, `Spec: replace <path>`, `Plan: remove`,
     or `Spec: remove`; a bare path must not overwrite an existing value. When
     the singleton is absent, a validated selected candidate or `Plan: <path>`
     / `Spec: <path>` attaches it.
   - Preserve existing `Reference` paths in their current order. `Reference:
     append <path>` validates and appends a new normalized path, then
     deduplicates normalized values without changing the order of the retained
     paths. `Reference: remove <path>` is the only way to remove an existing
     Reference. Reject a `Reference: append <path>`
     when the normalized path matches the selected or existing Plan or Spec; a
     Plan or Spec path must never also be added to Reference, whether discovered
     or explicitly requested.
   - Apply the same exclusion in the reverse direction: selecting, manually
     setting, or replacing Plan/Spec, including `attach as Plan and Spec`, is
     rejected when the normalized path is already present in Reference.
   - Offer `attach as Plan and Spec` only when the same validated document
     strongly qualifies for both roles. The user must explicitly select that
     combined Plan and Spec action; it does not imply a Reference attachment.
     Reject confirmation while any candidate role remains unresolved, asking
     only for that role's selection or `none`.
   - If an existing attachment path is invalid existing content (missing,
     non-regular, outside the repository, or otherwise invalid under the shared
     policy), warn about the invalid existing value in its attachment row but
     do not block unrelated field edits. An invalid new or replacement path
     reports the shared error and re-prompts only that attachment edit. Do not
     rerun discovery or general research after an edit-path validation failure.
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
| All ordinary fields present and valid | Still run attachment discovery; print "TODO-N has no missing or weak fields. Nothing to revise." only when it finds no candidate and no attachment edit was requested |
| User enters non-existent TODO-N | Print "TODO-N not found in TODOS.md." and exit |
| User enters archived TODO-N | Print "TODO-N is archived. Archived entries cannot be revised." and exit |
| User enters completed `[x]` TODO-N | Print "TODO-N is completed. Completed entries are archived and cannot be revised." and exit |
| Auto-research budget cap hit | Log which signals were skipped; treat remaining gaps as user questions |
| Auto-research fills nothing useful | Ask user for each missing required field one at a time |
| Write fails (file locked) | Print error; no partial write |
| Preview `edit` action | Return to Step 6 for batch re-edit; does NOT re-trigger auto-research |
