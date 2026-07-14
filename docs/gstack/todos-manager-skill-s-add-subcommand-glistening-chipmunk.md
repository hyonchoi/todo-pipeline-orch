# todos-manager --add: Auto-Research + File Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) Split `skills/todos-manager/SKILL.md` into a thin skeleton + on-demand section files, then (2) add an auto-research phase to `--add` that derives todo fields from the codebase and only asks targeted questions for genuine gaps.

**Architecture:** Modeled on the office-hours skill pattern — SKILL.md becomes a ~150-line decision-tree skeleton with a section index table. Heavy content moves to `skills/todos-manager/sections/`. The `--add` workflow gains a new step 4.5 that reads `sections/auto-research.md` on demand, silently researches the codebase, and pre-fills all fields before prompting the user.

**Tech Stack:** SKILL.md prose instructions only (no code). Markdown. `Read` tool calls for on-demand section loading.

## Global Constraints

- All files live under `skills/todos-manager/` (project-local; install script symlinks to `~/.claude/skills/`)
- One question per message — never batch clarifying questions
- Auto-research runs before any user questions; never ask what can be derived
- Pre-fills are editable drafts — user can override any field at step 5
- Existing steps 1–4 and 7–11 of `--add` are unchanged in behavior

---

## File Map

| File | Status | Lines (est.) | Responsibility |
|------|--------|--------------|----------------|
| `skills/todos-manager/SKILL.md` | Rewrite (shrink) | ~150 | Skeleton: purpose, subcommand routing, section index, step lists with `Read` pointers |
| `skills/todos-manager/sections/schema.md` | Create | ~65 | Full TODOS.md schema, field table, example entry, preamble template |
| `skills/todos-manager/sections/id-assignment.md` | Create | ~30 | ID sequencing rules + bootstrap algorithm + counter cache |
| `skills/todos-manager/sections/auto-research.md` | Create | ~70 | Field derivation rules, gap detection, synthesis format (new) |
| `skills/todos-manager/sections/acceptance-scenarios.md` | Create | ~65 | The 5 acceptance test walkthroughs (moved from bottom of SKILL.md) |

**Net result:** SKILL.md shrinks from 416 → ~150 lines. Total content grows by ~70 lines (auto-research) spread across files.

---

## Task 1: Create `sections/schema.md`

**Files:**
- Create: `skills/todos-manager/sections/schema.md`

Move the TODOS.md Schema section (lines 33–87 of current SKILL.md) and Preamble Template (lines 130–145) into this file verbatim.

- [ ] **Step 1: Create `sections/` directory and write schema.md**

Content to write to `skills/todos-manager/sections/schema.md`:

```markdown
# TODOS.md Schema

## File location and format

TODOS.md is stored at the repo root. Each entry occupies a single markdown list item (`- [ ] ...`), with fields as sub-bullets using bold labels.

## Entry header line

\`\`\`markdown
- [ ] **TODO-<n>: <Title>** — <One-line summary>
\`\`\`

## Status markers

| Marker | Meaning |
|--------|---------|
| `[ ]` | Pending |
| `[→]` | In progress |
| `[x]` | Done |
| `[~]` | On hold |

## Required fields

| Field | Description | Format |
|-------|-------------|--------|
| **What:** | What needs to be done | Free text |
| **Why:** | Why this task matters | Free text |
| **Decisions:** | Key decisions | Backtick-delimited: `Priority \`P1\`, Effort \`M\`, Phase \`4 (Development)\`, Branch \`feature/...\`, Test Coverage \`required/not-required\`, Security Review \`required/not-required\`` |

## Optional fields

| Field | Description |
|-------|-------------|
| **Pros:** | Benefits |
| **Cons:** | Risks/drawbacks |
| **Context:** | References, design doc pointers, file locations |
| **Depends on:** | Other TODO-<n> references |
| **Assumptions:** | Preconditions |
| **Completed:** | Version + date (set when done) |
| **Resolved design:** | Design decisions (zero or more) |

## Example: complete entry

\`\`\`markdown
- [ ] TODO-42: refactor pipeline-watcher.py into uv modules
  - **What:** Split `pipeline-watcher.py` into modular Python packages under `hermes_pipeline/`.
  - **Why:** Single-file monolith is hard to test and extend. Modularization unblocks CI integration.
  - **Pros:** Testable modules, shared utilities, clear boundaries
  - **Cons:** Migration effort, import path updates across test suite
  - **Context:** Design lives in [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md)
  - **Depends on:** `TODO-40` (design review finalized)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/modularize-watcher`, Test Coverage `required`, Security Review `not-required`
\`\`\`

## Preamble Template

When creating or converting TODOS.md, insert this blockquote as the file header:

\`\`\`markdown
# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`
\`\`\`
```

- [ ] **Step 2: Verify file was written**

```bash
wc -l skills/todos-manager/sections/schema.md
```

Expected: ~65 lines.

---

## Task 2: Create `sections/id-assignment.md`

**Files:**
- Create: `skills/todos-manager/sections/id-assignment.md`

Move the Stable TODO-<n> ID Assignment section (lines 89–113 of current SKILL.md) into this file verbatim.

- [ ] **Step 1: Write id-assignment.md**

Content mirrors current SKILL.md lines 89–113 exactly (ID sequencing rule, bootstrap algorithm, counter cache). No changes to the content — this is a pure move.

- [ ] **Step 2: Verify**

```bash
wc -l skills/todos-manager/sections/id-assignment.md
```

Expected: ~28 lines.

---

## Task 3: Create `sections/auto-research.md` (new content)

**Files:**
- Create: `skills/todos-manager/sections/auto-research.md`

This is the new auto-research phase logic. Write the full content:

- [ ] **Step 1: Write auto-research.md**

```markdown
# Auto-Research Phase for --add (Step 4.5)

## Purpose

After the user provides a title and summary, silently research the codebase to
derive all todo fields. Only ask targeted questions for gaps that research
couldn't resolve. Never ask what can be determined.

## Research signals — collect silently before any output

| Signal | What to read |
|--------|-------------|
| `TODOS.md` | Keyword-match related entries → candidate `Depends on`, existing Priority patterns |
| `TODOS-archive.md` | Prior similar work → informs `Effort` estimate |
| `git log --oneline -20` | Recent activity, branch naming conventions, phase references |
| `docs/gstack/` or `docs/superpowers/` | Design docs matching title keywords → `Why`, `What`, `Context` |
| `CLAUDE.md` | Phase definitions, branch naming rules |
| Relevant source files implied by title | Confirms scope → `What` boundaries, `Effort` sizing |

## Field derivation rules

| Field | How to derive |
|-------|--------------|
| `Why` | Matching design doc rationale → related TODOS `Why` fields → git commit messages on same area |
| `What` | Title/summary + scope implied by related files found |
| `Pros` | Inverse of `Why` (what improves) + design doc benefits sections |
| `Cons` | Related TODOS `Cons` + design doc risk language + migration cost if existing code changes |
| `Priority` | Default `P2`; upgrade to `P1` if a related TODO is `[→]` or a design doc is APPROVED; upgrade to `P0` if summary contains "blocking" or "broken" |
| `Effort` | `S` = single-file change; `M` = multi-file or new module; `L` = new subsystem |
| `Phase` | Match CLAUDE.md phase list via current branch name or latest commit phase reference |
| `Branch` | Follow naming convention observed in last 5 branches (`git branch --sort=-committerdate`) |
| `Test Coverage` | `required` if `What` implies new logic or new function; `not-required` if docs-only or config-only |
| `Security Review` | `required` if title/summary contains: auth, token, secret, permission, credential, API key; else `not-required` |
| `Depends on` | TODO-<n> IDs found in matching design docs, or `[→]` TODOs on related topics |
| `Context` | Path to matching design doc if found |

## Gap detection — only ask for these

After derivation, identify fields that are still empty or ambiguous. Ask gap
questions **one at a time**, in this priority order:

1. `Why` — if no design doc or related TODO rationale found  
   → Ask: "Why does this matter? What breaks or stays slow without it?"
2. `What` — if scope is still vague after file search  
   → Ask: "What's the minimal deliverable? What's explicitly out of scope?"
3. `Priority` — if no blocking signal found (no `[→]` TODO, no APPROVED doc, no urgency keyword)  
   → Offer: `[P0] Blocking now / [P1] This sprint / [P2] Backlog / [P3] Someday`
4. `Effort` — if file scope is ambiguous  
   → Offer: `[S] Hours / [M] 1–3 days / [L] Week+`
5. `Depends on` — only if the title explicitly references another task and no ID was found

Accept the user's first answer without pushing back — this is not an interrogation.

## Synthesis block

After all gaps are resolved, show:

\`\`\`
======== AUTO-RESEARCH SYNTHESIS ========
Why:             <derived or answered>          [Confidence: high/medium/low]
What:            <derived or answered>          [Confidence: high/medium/low]
Pros:            <derived>
Cons:            <derived>
Context:         <path to design doc, or "(none found)">
Priority:        <derived or answered>          [Confidence: high/medium/low]
Effort:          <derived or answered>          [Confidence: high/medium/low]
Phase:           <derived>                      [Confidence: high/medium/low]
Branch:          <derived>                      [Confidence: high/medium/low]
Test Coverage:   <derived>                      [Confidence: high/medium/low]
Security Review: <derived>                      [Confidence: high/medium/low]
Depends on:      <derived or answered, or "(none)">
======== END SYNTHESIS ========

These are pre-fills — confirm or edit each in the next step.
\`\`\`

Confidence rule: fields answered directly by the user (via gap questions) are
always `high`. Derived fields are `high` if backed by an exact match (design
doc found, related TODO with same keywords, explicit blocking keyword in
summary), `medium` if inferred from a pattern (branch naming convention,
recent commit phase reference), and `low` if defaulted with no supporting
signal (e.g. Priority defaulted to `P2`, Security Review defaulted to
`not-required` with no keyword match). Never mark a defaulted field `high`.

- [ ] **Step 2: Verify**

```bash
wc -l skills/todos-manager/sections/auto-research.md
```

Expected: ~70 lines.

---

## Task 4: Create `sections/acceptance-scenarios.md`

**Files:**
- Create: `skills/todos-manager/sections/acceptance-scenarios.md`

Move the Acceptance Scenarios section (lines ~350–416 of current SKILL.md) verbatim — no content changes.

- [ ] **Step 1: Write acceptance-scenarios.md**

Copy lines 350–416 of `skills/todos-manager/SKILL.md` exactly. Five scenarios: A1 (--add happy path), A2 (--init), A3 (--convert), A4 (--archive), A5 (--audit with issues).

- [ ] **Step 2: Verify**

```bash
wc -l skills/todos-manager/sections/acceptance-scenarios.md
```

Expected: ~65 lines.

---

## Task 5: Rewrite `SKILL.md` as skeleton

**Files:**
- Modify: `skills/todos-manager/SKILL.md`

Replace the full body with the skeleton below. Keep the YAML frontmatter unchanged.

- [ ] **Step 1: Read current frontmatter to copy it exactly**

```bash
sed -n '1,12p' skills/todos-manager/SKILL.md
```

- [ ] **Step 2: Write the new skeleton body (after frontmatter)**

New body content after the closing `---` of frontmatter:

```markdown
## Purpose

The **todos-manager** skill automates TODOS.md entries in gstack-format projects.
It enforces the canonical schema, stable TODO-<n> IDs, and provides a preview gate
before writing to disk.

### When to use

- `--add` — add a new entry (auto-researches codebase to pre-fill fields)
- `--init` — initialize TODOS.md in a new project
- `--convert` — convert existing TODOS.md to enforced format
- `--audit` — audit format compliance
- `--archive` — move completed TODOs to TODOS-archive.md

### Prerequisite state

- TODOS.md exists at repo root (or create with `--init`)
- User has write access to TODOS.md

---

## Section index — Read each section when its situation applies

This skill is a decision-tree skeleton. Steps below point to on-demand sections.
**Read the section in full before executing its step.**

| When | Read this section |
|------|-------------------|
| Any `--add` or `--audit` or `--convert` step references schema | `sections/schema.md` |
| Computing or validating TODO-<n> IDs | `sections/id-assignment.md` |
| Executing --add step 4.5 (auto-research) | `sections/auto-research.md` |
| Running acceptance tests or verifying behavior | `sections/acceptance-scenarios.md` |

---

## Workflow

### `--add`: Add new entry with schema enforcement

1. **Validate context:** Does TODOS.md exist? If not, prompt to run `--init` first.
2. **Compute next TODO-<n>:** Read `sections/id-assignment.md`. Output: "Next ID will be `TODO-<n>`."
3. **Prompt for title:** "Enter the TODO title (required):" — 10–200 characters, non-empty.
4. **Prompt for summary:** "One-line summary after the em dash (required):" — 10–100 characters, non-empty.
5. **Auto-research:** Read `sections/auto-research.md`. Execute the research phase: collect signals, derive field drafts, ask gap questions one at a time, show synthesis block.
6. **Confirm or edit fields** (pre-filled from auto-research):
   - Present all fields from the synthesis block in a single message, in the same
     order shown there, with their `Confidence:` tags. Instruction: "Reply
     `confirm` to accept all as-is, or list edits as `field: new value` — only
     the fields you mention change." This is a chat interface, not a terminal;
     do not ask field-by-field. One round-trip covers all 10 fields.
   - **What:** (required, non-empty)
   - **Why:** (required, 10–200 chars)
   - **Decisions:** Priority, Effort, Phase, Branch, Test Coverage, Security Review — all editable in the same batched reply
   - **Pros:** (optional)
   - **Cons:** (optional)
   - **Context:** (optional)
   - **Depends on:** (optional; validate each TODO-<n> exists in TODOS.md or TODOS-archive.md)
   - **Assumptions:** (optional)
   - If the reply contains an invalid edit (e.g. bad Depends-on ID, out-of-range
     Decisions value), report just that field's error and re-prompt for that
     field only — do not discard the other confirmed edits.
7. **Assemble entry in memory** — format per `sections/schema.md`. Do **not** write to disk yet.
8. **Preview gate:**
   ```
   ======== PREVIEW ========
   - [ ] **TODO-<n>: <Title>** — <Summary>
     - **What:** ...
     - **Why:** ...
     [all fields]
   ======== END PREVIEW ========

   Proceed? [y / edit / cancel]
   ```
   - `y` → proceed to step 9
   - `edit` → return to step 6 (no ID burned, no files written)
   - `cancel` → print "Entry discarded." and exit
9. **Write to TODOS.md:** Insert formatted entry at end of file (after last entry, before trailing blank lines).
10. **Update counter cache:** Write next_id to `.hermes/todo_id_counter` if `.hermes/` exists.
11. **Confirm:** "✓ Entry added as TODO-<n>."

---

### `--init`: Initialize TODOS.md

1. Check if TODOS.md exists at repo root. If present, print "TODOS.md already exists." and exit.
2. Read `sections/schema.md` for Preamble Template. Create TODOS.md with preamble blockquote.
3. Create TODOS-archive.md with minimal header.
4. Initialize `.hermes/todo_id_counter` to 0 if `.hermes/` directory exists.
5. Print "✓ TODOS.md initialized. Use `todos-manager --add` to add entries."

---

### `--convert`: Convert existing TODOS.md to enforced format

1. Read TODOS.md. If absent, print error and exit.
2. Read `sections/schema.md` for Preamble Template and field definitions.
3. If preamble blockquote is missing, insert it after `# TODOS` header.
4. Validate each entry against required fields. Output audit report listing violations.
5. Do not rewrite entry bodies — report only.

---

### `--audit`: Audit TODOS.md for format compliance

1. Read `sections/schema.md` for field and format rules.
2. Scan all entries in TODOS.md + TODOS-archive.md.
3. Check required fields, dependency references, status markers.
4. Output structured audit report. Do not modify any files.

Audit report format:
\`\`\`
## TODOS.md Audit Report
Schema version: 2.0
Scanned: TODOS.md (N entries), TODOS-archive.md (M entries)
Issues found: K
- TODO-X: Missing required field **Decisions:**
- TODO-Y: Invalid dependency reference `TODO-Z` (not found)
ID gap check: OK (max=23, counter=23)
\`\`\`

---

### `--archive`: Move completed TODOs to archive

1. Scan TODOS.md for `[x]` entries.
2. If TODOS-archive.md does not exist, create it with minimal header.
3. Move all `[x]` entries to TODOS-archive.md (appended, newest ID last).
4. Remove moved entries from TODOS.md.
5. Print "✓ Archived N entries to TODOS-archive.md."

---

## Error Messages

Each error names the absolute file path and a one-line remediation:

```
Error: /path/to/TODOS.md does not exist.
Remediation: Run `todos-manager --init`.

Error: Title must be 10–200 characters.
Remediation: Re-enter the title.

Error: **What:** field is empty.
Remediation: Describe what needs to be done (required).

Error: **Why:** field must be 10–200 characters.
Remediation: Provide a rationale (required).

Error: **Decisions:** field is missing.
Remediation: Set Priority, Effort, Phase, Branch, Test Coverage, Security Review.

Error: Dependency TODO-99 does not exist in TODOS.md or TODOS-archive.md.
Remediation: Check valid IDs or remove TODO-99 from depends_on.

Error: Status marker "[->]" is not recognized.
Remediation: Use one of: [ ] [→] [x] [~]
```

---

## Observability

Log to `.claude/gstack/todos-manager.log`:

```
[timestamp] todos-manager: start
[timestamp] todos-manager: bootstrap - scanned N existing IDs
[timestamp] todos-manager: next_id = TODO-<n>
[timestamp] todos-manager: auto-research - derived Why from design doc
[timestamp] todos-manager: auto-research - gap: Priority (no blocking signal found)
[timestamp] todos-manager: preview - gate reached
[timestamp] todos-manager: user_action - confirm="y"
[timestamp] todos-manager: write - inserted at line N
[timestamp] todos-manager: done - TODO-<n> committed
```
```

- [ ] **Step 3: Verify skeleton line count**

```bash
wc -l skills/todos-manager/SKILL.md
```

Expected: ~150 lines.

- [ ] **Step 4: Verify sections directory**

```bash
ls -la skills/todos-manager/sections/
```

Expected: 4 files — schema.md, id-assignment.md, auto-research.md, acceptance-scenarios.md.

- [ ] **Step 5: Commit via git-atomic-commits skill**

Use `Skill(git-atomic-commits)` — do not run `git commit` directly (CLAUDE.md requirement).

---

## Verification

End-to-end test after all tasks complete:

1. Run `todos-manager --add`. Enter a title matching a known design doc (e.g. "pipeline modularization").
   - **Expect:** No questions asked; synthesis block appears with `Why`/`What`/`Context` populated.
2. Run `todos-manager --add`. Enter a novel title with no codebase matches.
   - **Expect:** Only gap questions appear (Why first, then any remaining gaps), one at a time.
3. At preview gate, choose `edit` → confirm returns to step 6 (field confirm), not step 5 (auto-research).
4. Run `todos-manager --audit` → confirm schema.md is read and audit report is correct.
5. Check `.claude/gstack/todos-manager.log` contains auto-research derivation entries.
6. Confirm final TODOS.md entry is correctly formatted per `sections/schema.md`.
7. **Schema sync check (T3):** Diff `sections/schema.md`'s Required/Optional field
   tables against `tests/skill-test-environment/skill_logic.py`'s `REQUIRED_FIELDS`
   / `OPTIONAL_FIELDS` constants (and any status-marker set). Confirm the two stay
   in agreement — flag drift if `schema.md` lists a field the Python oracle
   doesn't recognize, or vice versa.

---

## NOT in scope

- Updating `tests/skill-test-environment/skill_logic.py` to mirror section structure — test oracle is a behavioral snapshot, not a schema mirror
- Updating golden files in `tests/skill-test-environment/golden/` — test harness design is covered by a separate APPROVED office-hours design doc
- Modifying the `--list` subcommand workflow — unchanged by this plan

## What already exists

- `tests/skill-test-environment/skill_logic.py` — Python implementation of ID sequencing, entry parsing, format validation, archive logic. Serves as both test oracle and golden-file generator. Schema changes should trigger sync verification.
- `tests/skill-test-environment/unit/` — 5 unit test files covering ID sequencing, entry parsing, format validation, archive logic, verification
- Office-hours skill (`~/.claude/skills/gstack/office-hours/`) — the proven section-split pattern this plan models itself after
- `scripts/install-todos-manager.sh` — symlink installer that copies the skill to `~/.claude/skills/todos-manager/` and `~/.agents/skills/todos-manager/`

## Worktree Parallelization

Sequential implementation — all tasks touch `skills/todos-manager/` and depend on prior tasks (skeleton depends on sections being created first).

## Implementation Tasks

Synthesized from this review's findings. Each task derives from a specific finding above.

- [ ] **T1 (P1, human: ~15min / CC: ~3min)** — Auto-research — Add effort budget cap
  - Surfaced by: Architecture review — unbounded file reads during research phase
  - Files: `skills/todos-manager/sections/auto-research.md`
  - Verify: Confirm cap is stated in the section file

- [ ] **T2 (P1, human: ~20min / CC: ~5min)** — Section files — Add missing convert/list sections
  - Surfaced by: Architecture review — SKILL.md 594 vs 416 lines; --list and --convert Mode B not covered
  - Files: `skills/todos-manager/sections/convert-mode-b.md`, `skills/todos-manager/sections/list.md`, `skills/todos-manager/SKILL.md` (section index)
  - Verify: Skeleton references all section files; line count stays near target

- [ ] **T3 (P2, human: ~10min / CC: ~3min)** — Schema DRY — Add sync verification step
  - Surfaced by: Code quality review — schema duplicated across 3 places
  - Files: Plan verification section
  - Verify: Verification step cross-checks schema.md against skill_logic.py

- [ ] **T4 (P2, human: ~10min / CC: ~3min)** — Acceptance scenarios — Update for auto-research
  - Surfaced by: Test review — A1 doesn't include auto-research phase
  - Files: `skills/todos-manager/sections/acceptance-scenarios.md`
  - Verify: Scenarios cover both matched and novel title paths

- [ ] **T5 (P3, human: ~10min / CC: ~3min)** — Install script — Update symlink for sections directory
  - Surfaced by: Performance review — symlink copies SKILL.md; new sections/ subdirectory needs coverage
  - Files: `scripts/install-todos-manager.sh`
  - Verify: Installed skill includes sections/ subdirectory

- [ ] **T6 (P2, human: ~5min / CC: ~2min)** — Task 5 — Add backup-before-rewrite step
  - Surfaced by: CEO review — Architecture: Task 5 rewrites SKILL.md in place as the final step with no rollback if the rewrite goes wrong mid-edit
  - Files: `docs/gstack/todos-manager-skill-s-add-subcommand-glistening-chipmunk.md` (Task 5), `skills/todos-manager/SKILL.md.bak` (transient)
  - Verify: Task 5 includes a `cp skills/todos-manager/SKILL.md skills/todos-manager/SKILL.md.bak` step before the rewrite, with a note to restore from backup if post-rewrite verification (Steps 3-4) fails

- [x] **T7 (P2)** — Auto-research — Add per-field confidence tags to synthesis
  - Surfaced by: CEO review — Codex outside voice: heuristic-derived fields (Priority, Effort, Security Review, Depends on) can look authoritative even when the underlying signal is weak, eroding trust in silently-wrong pre-fills
  - Files: `skills/todos-manager/sections/auto-research.md`
  - Resolved by: DX review — folded directly into Task 3's synthesis block content (was previously a bolted-on follow-up whose target section still showed the old, untagged block), plus an explicit confidence-assignment rule so `high` is never applied to a defaulted field.

- [x] **T8 (P2)** — `--add` Step 6 — Batch field confirmation instead of per-field terminal metaphor
  - Surfaced by: DX review — "press Enter to accept or type a replacement" is a terminal/REPL metaphor that doesn't map to a chat-based agent; taken literally it forces up to 10 round-trips to confirm one entry, undoing most of the friction reduction auto-research was built to deliver.
  - Files: `docs/gstack/todos-manager-skill-s-add-subcommand-glistening-chipmunk.md` (Task 5, `--add` step 6)
  - Verify: Step 6 presents all pre-filled fields (with confidence tags) in one message; user replies `confirm` or lists `field: new value` edits; invalid edits re-prompt only for that field, without discarding other confirmed edits.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_open | 2 issues, HOLD SCOPE confirmed |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 (via CEO review) | resolved | 1 new finding (confidence tags), 1 already-settled (bundling) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 2 | clean | 6 issues (run 1), 0 new issues (run 2 — verified T6/T7) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps (DX TRIAGE mode) | 1 | resolved | 2 issues: T7's confidence tags weren't wired into Task 3's actual section content (fixed); Step 6's per-field terminal metaphor doesn't fit a chat interface (fixed — batched confirm) |

VERDICT: CLEAR — CEO review's T6 (backup step) and T7 (confidence tags) verified sound on eng re-review; DX review closed the gap between T7's intent and Task 3's actual content, and fixed the confirm-step UX mismatch. 10 total findings incorporated across four reviews, 0 outstanding.

NO UNRESOLVED DECISIONS
