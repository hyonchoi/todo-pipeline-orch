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

## Workflow

The skill follows a 9-step workflow, with a **preview/confirm gate at step 7.5**.

### Step 1: Validate context

- **Check:** Does TODOS.md exist? (if not, bootstrap as per ## First-run bootstrap)
- **Check:** Is `.claude/gstack/` directory writable?
- **Action:** If validation fails, jump to ## Error Messages.

### Step 2: Compute next TODO-<n>

- **Action:** Run the bootstrap algorithm (see ## Stable TODO-<n> ID Assignment).
- **Output to user:** "Next ID will be `TODO-<n>`."

### Step 3: Prompt for entry title

- **Prompt:** "Enter the TODO title (required):"
- **Validation:** Title must be 10–200 characters and non-empty.
- **On fail:** Re-prompt with error message from ## Error Messages.

### Step 4: Prompt for metadata

Iterate through the template fields (see ## TODOS.md Schema):

- **Title:** Already captured in step 3.
- **Assigned To:** Prompt: "Who should own this task? (name or @handle, required):"
  - Validation: Non-empty, recommended format is `@handle` or full name.
- **Estimate:** Prompt: "Time estimate (e.g., `1h`, `2d`, `1w`):"
  - Validation: Must match regex `/^\d+[hd w]$/` (no default; re-prompt if invalid).
- **Rationale:** Prompt: "Why is this task important? (one-line, required):"
  - Validation: Non-empty, 10–150 characters.
- **Status:** Prompt: "Status? (`active`, `blocked`, `done`, `deferred`, default: `active`):"
  - Validation: Must be one of the 4 enum values.
- **Depends on:** Prompt: "Depends on which TODOs? (comma-separated IDs, e.g., `TODO-1, TODO-3`, optional):"
  - Validation: Each ID must exist in TODOS.md. On fail, list valid IDs.
- **Notes:** Prompt: "Additional notes (optional, multi-line, `END` to finish):"
  - Validation: None; free text.

### Step 5: Assemble entry in memory

Format the captured data as a markdown list item (see ## TODOS.md Schema, Example section).

Do **not** write to disk yet.

### Step 6: Locate insertion point

- **Action:** Scan TODOS.md for section headers (`## In Progress`, `## Blocked`, `## Done`).
- **Prompt:** "Which section should this entry go in? (`in-progress`, `blocked`, `done`, default: `in-progress`):"
- **Action:** Identify the line number where the new entry will be inserted (after the section header).

### Step 7: Apply entry format rules

- **Ensure:** Entry is formatted exactly as per ## TODOS.md Schema.
- **Ensure:** ID is set to the computed `TODO-<n>` from step 2.
- **Ensure:** Status field matches the user's selection from step 4 (do not hardcode `active`; use what they picked).

### Step 7.5: Preview gate (T10)

**Show the assembled entry exactly as it will be written:**

```
======== PREVIEW ========
- [ ] TODO-<n>: <Title>
  - **Assigned To:** <name>
  - **Estimate:** <estimate>
  - **Rationale:** <rationale>
  - **Status:** <status>
  - **Depends on:** <depends_on_list or "None">
  - **Notes:** <notes or "(None)">
======== END PREVIEW ========

Proceed? [y / edit / cancel]
```

**Branches:**
- **`y`** → Proceed to step 8.
- **`edit`** → Jump back to step 4 (no ID burned, no files written).
- **`cancel`** → Abort entirely (no ID burned, no Slack notify). Print: "Entry discarded."

### Step 8: Write to TODOS.md

- **Action:** Insert the formatted entry at the computed insertion point.
- **Action:** Update the `Last updated` timestamp in the TODOS.md header.
- **Verify:** File is valid markdown (no syntax errors).

### Step 9: Confirm and notify

- **Print:** "✓ Entry added as TODO-<n>."
- **(Optional future):** Post to Slack (requires gstack Slack integration).
- **Return control** to the user.

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
