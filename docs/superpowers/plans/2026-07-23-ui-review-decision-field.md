# UI Review Decision Field Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `UI Review` Decisions sub-field to the `todos-manager` skill, mirroring the existing `Security Review` field exactly, so `phase_6_2_qa` has a documented skip signal on every newly created TODO entry.

**Architecture:** Three additive doc edits to `skills/todos-manager/` (`sections/schema.md`, `sections/auto-research.md`, `SKILL.md`), each adding `UI Review` as a sibling to the existing `Security Review` field — same vocabulary (`required`/`not-required`), same auto-derivation-from-keywords pattern. No code changes, no `phases.yaml` edit, no `TODOS.md` preamble edit (confirmed out of scope by the design doc). Verification is a manual dry run of the `--add` auto-research flow, not an automated test, since `todos-manager` is a markdown-driven skill with no executable test suite.

**Tech Stack:** Markdown skill files (`skills/todos-manager/**/*.md`) — no code, no build step.

## Global Constraints

- Must mirror the existing `Security Review required/not-required` field exactly — same vocabulary, same auto-derivation-from-keywords pattern.
- Backfilling ~30 existing TODO entries with a UI Review value is out of scope — new entries only.
- No changes to `phases.yaml`.
- No edit to `TODOS.md`'s preamble blockquote (it does not enumerate Decisions sub-fields today; `schema.md` is the sole source of truth for sub-field definitions).
- Keyword list for auto-derivation (verbatim from TODO-31's own "What"): ui, frontend, design, visual, layout, component, css, style, dashboard, artifact, page, screen, modal, form, navigation, button, icon, animation.

---

### Task 1: Add `UI Review` to schema.md

**Files:**
- Modify: `skills/todos-manager/sections/schema.md`

**Interfaces:**
- Consumes: nothing (pure doc edit)
- Produces: the `Decisions:` row's example format and the "Example: complete entry" block now document `UI Review` — Task 2 and Task 3 reference this exact field name and vocabulary (`UI Review \`required/not-required\``)

- [ ] **Step 1: Edit the Required fields table row for `Decisions:`**

In `skills/todos-manager/sections/schema.md`, find this line in the Required fields table:

```markdown
| **Decisions:** | Key decisions | Backtick-delimited: `Priority \`P1\`, Effort \`M\`, Phase \`4 (Development)\`, Branch \`feature/...\`, Test Coverage \`required/not-required\`, Security Review \`required/not-required\`` |
```

Replace it with:

```markdown
| **Decisions:** | Key decisions | Backtick-delimited: `Priority \`P1\`, Effort \`M\`, Phase \`4 (Development)\`, Branch \`feature/...\`, Test Coverage \`required/not-required\`, Security Review \`required/not-required\`, UI Review \`required/not-required\`` |
```

- [ ] **Step 2: Edit the "Example: complete entry" block's Decisions line**

Find this line inside the fenced example block:

```markdown
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/modularize-watcher`, Test Coverage `required`, Security Review `not-required`
```

Replace it with:

```markdown
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/modularize-watcher`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
```

- [ ] **Step 3: Verify the edit**

Run: `grep -n "UI Review" skills/todos-manager/sections/schema.md`
Expected: two matches — the Required fields table row and the Example block's Decisions line.

- [ ] **Step 4: Commit**

```bash
git add skills/todos-manager/sections/schema.md
git commit -m "docs(todos-manager): add UI Review field to schema.md"
```

---

### Task 2: Add `UI Review` derivation rule to auto-research.md

**Files:**
- Modify: `skills/todos-manager/sections/auto-research.md`

**Interfaces:**
- Consumes: `UI Review \`required/not-required\`` field name and vocabulary from Task 1
- Produces: the `UI Review` row in the Field derivation rules table, the `UI Review:` line in the Synthesis block template — Task 3's `SKILL.md` edit and the plan's verification test (Task 4) rely on this synthesis block containing `UI Review:`

- [ ] **Step 1: Add a derivation rule row to the Field derivation rules table**

Find this row in `skills/todos-manager/sections/auto-research.md`:

```markdown
| `Security Review` | `required` if title/summary contains: auth, token, secret, permission, credential, API key; else `not-required` |
```

Add a new row immediately after it:

```markdown
| `UI Review` | `required` if title/summary contains: ui, frontend, design, visual, layout, component, css, style, dashboard, artifact, page, screen, modal, form, navigation, button, icon, animation; else `not-required` |
```

- [ ] **Step 2: Add `UI Review` to the Synthesis block output template**

Find this line in the Synthesis block fenced example:

```markdown
Security Review: <derived or answered>          [Confidence: high/medium/low]
```

Add a new line immediately after it:

```markdown
UI Review:       <derived or answered>          [Confidence: high/medium/low]
```

- [ ] **Step 3: Update the Confidence rule note to reference UI Review's default case**

Find this sentence in the Confidence rule paragraph:

```markdown
`low` if defaulted with no supporting
signal (e.g. Priority defaulted to `P2`, Security Review defaulted to
`not-required` with no keyword match). Never mark a defaulted field `high`.
```

Replace it with:

```markdown
`low` if defaulted with no supporting
signal (e.g. Priority defaulted to `P2`, Security Review or UI Review
defaulted to `not-required` with no keyword match). Never mark a defaulted
field `high`.
```

- [ ] **Step 4: Verify the edit**

Run: `grep -n "UI Review" skills/todos-manager/sections/auto-research.md`
Expected: three matches — the derivation rule row, the Synthesis block template line, and the Confidence rule note.

- [ ] **Step 5: Commit**

```bash
git add skills/todos-manager/sections/auto-research.md
git commit -m "docs(todos-manager): add UI Review derivation rule to auto-research.md"
```

---

### Task 3: Add `UI Review` to SKILL.md's editable fields list and remediation text

**Files:**
- Modify: `skills/todos-manager/SKILL.md`

**Interfaces:**
- Consumes: `UI Review` field name from Task 1 and Task 2
- Produces: `SKILL.md` now lists `UI Review` alongside `Security Review` in both the `--add` workflow's step 6 editable-fields list and the Error Messages remediation text — completes all three documented touch points from the design doc

- [ ] **Step 1: Edit the `--add` workflow step 6 Decisions bullet**

Find this line under `### --add: Add new entry with schema enforcement`, step 6:

```markdown
   - **Decisions:** Priority, Effort, Phase, Branch, Test Coverage, Security Review — all editable in the same batched reply
```

Replace it with:

```markdown
   - **Decisions:** Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review — all editable in the same batched reply
```

- [ ] **Step 2: Edit the Error Messages remediation text**

Find this line under `## Error Messages`:

```markdown
Error: **Decisions:** field is missing.
Remediation: Set key decisions: Priority, Effort, Phase, Branch, Test Coverage, Security Review.
```

Replace it with:

```markdown
Error: **Decisions:** field is missing.
Remediation: Set key decisions: Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review.
```

- [ ] **Step 3: Edit the Error & Rescue Map remediation row**

Find this row in the `### Error & Rescue Map` table:

```markdown
| **Decisions:** is missing | Missing required field | Provide key decisions with backtick-delimited values |
```

This row is field-generic (no field names listed), so it does not need editing — leave it as-is. No change to this table.

- [ ] **Step 4: Verify the edit**

Run: `grep -n "UI Review" skills/todos-manager/SKILL.md`
Expected: two matches — the `--add` workflow step 6 bullet and the Error Messages remediation text.

- [ ] **Step 5: Commit**

```bash
git add skills/todos-manager/SKILL.md
git commit -m "docs(todos-manager): add UI Review to SKILL.md editable fields and remediation text"
```

---

### Task 4: Verification test — confirm auto-derivation end-to-end

**Files:**
- None modified — this task exercises the skill via its own `--add` workflow, driven manually through conversation with the skill's instructions (`sections/auto-research.md` as edited in Task 2).

**Interfaces:**
- Consumes: the derivation rule and synthesis block template from Task 2, the field name from Task 1
- Produces: a confirmation artifact (terminal transcript excerpt) proving the skip signal is wired end-to-end — no downstream task depends on this output

- [ ] **Step 1: Invoke todos-manager --add with a UI-flavored title**

Run the `todos-manager` skill's `--add` subcommand (via the Skill tool or however this project's skill harness invokes it) and, when prompted for a title, enter:

```
Add dashboard filter component
```

For the one-line summary prompt, enter:

```
Adds a filterable dashboard component to the UI layer
```

- [ ] **Step 2: Confirm the synthesis block shows `UI Review: required`**

Let auto-research (step 4.5 of the `--add` workflow) run to completion. Inspect the `======== AUTO-RESEARCH SYNTHESIS ========` block printed before the confirm/edit gate.

Expected: the block contains a line reading:

```
UI Review:       required          [Confidence: high]
```

(Confidence is `high` because "dashboard" and "component" are exact keyword matches from the derivation rule added in Task 2, Step 1.)

- [ ] **Step 3: Cancel the entry — this is a verification dry run, not a real TODO**

At the Preview gate (`Proceed? [y / edit / cancel]`), reply `cancel`.

Expected output: `Entry discarded.` — confirms no ID was burned and no write occurred to `TODOS.md` or `.hermes/todo_id_counter`.

- [ ] **Step 4: Record the verification result**

No commit for this task — it modifies no tracked files. If the synthesis block did not show `UI Review: required`, stop and re-check Task 2's edits to `sections/auto-research.md` before proceeding further (do not mark this plan complete).

---

## Self-Review

**Spec coverage:**
- Design doc's four touch points → Tasks 1–3 cover `schema.md`, `auto-research.md`, `SKILL.md` exactly. `TODOS.md` preamble confirmed by the design doc as needing no edit — correctly omitted.
- Design doc's "Verification test" section → Task 4 implements it verbatim (same example title "Add dashboard filter component").
- Design doc's keyword list → reproduced verbatim in Global Constraints and Task 2 Step 1.
- Design doc's Success Criteria (all four touch points documented; auto-derivation confirmed; phase_6_2_qa can find the signal) → satisfied by Tasks 1–4 collectively; phase_6_2_qa's ability to read the field was already established by TODO-24 (dependency, already landed) and requires no code change here.

**Placeholder scan:** No TBD/TODO/"handle appropriately" patterns present. All steps show exact before/after text.

**Type consistency:** Field name `UI Review` and vocabulary `required`/`not-required` used identically across Tasks 1, 2, 3, and 4's expected synthesis output.
