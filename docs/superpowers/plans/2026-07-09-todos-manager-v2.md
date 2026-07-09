# TODOS Manager v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align todos-manager skill schema to real TODOS.md format, add 5 subcommands (--init, --convert, --add, --audit, --archive), move skill to platform-neutral location with install script, insert preamble, and add CLAUDE.md reference.

**Architecture:** Document-only changes. Rewrite SKILL.md to match real format, move skill source to `skills/todos-manager/` (git-tracked), add install script (`scripts/install-todos-manager.sh`), add blockquote preamble to TODOS.md, add enforcement reference to CLAUDE.md.

**Tech Stack:** Markdown (SKILL.md, TODOS.md, CLAUDE.md), Bash (install script)

**Portability:** The skill source lives at `skills/todos-manager/SKILL.md` (platform-neutral, git-tracked). `scripts/install-todos-manager.sh` symlinks to user-level skill directories (`~/.claude/skills/todos-manager/`, `~/.agents/skills/todos-manager/`). Cloning the repo + running the install script makes the skill available.

## Global Constraints

- Schema version: 2.1.0 (update SKILL.md frontmatter version)
- Preamble must be a blockquote (not a section header) so it doesn't interfere with markdown TOC tools
- Archive file: `TODOS-archive.md` at repo root
- All SKILL.md examples must use real TODOS.md field names: What, Why, Pros, Cons, Context, Depends on, Decisions, Assumptions, Completed, Resolved design
- Old location `.claude/skills/todos-manager/` must be removed from git tracking after move

---

### Task 0: Move Skill to Platform-Neutral Location

**Files:**
- Create: `skills/todos-manager/SKILL.md` (copy from `.claude/skills/todos-manager/SKILL.md`)
- Delete from git: `.claude/skills/todos-manager/SKILL.md`
- Modify: `.gitignore` (no change needed — `.claude/` already ignored in agent client, project `skills/` is tracked)

**Interfaces:**
- Consumes: Current `.claude/skills/todos-manager/SKILL.md`
- Produces: `skills/todos-manager/SKILL.md` (git-tracked source), `.claude/skills/todos-manager/` untracked (local-only agent link)

- [ ] **Step 1: Create platform-neutral skill directory**

```bash
mkdir -p skills/todos-manager
cp .claude/skills/todos-manager/SKILL.md skills/todos-manager/SKILL.md
```

- [ ] **Step 2: Remove old location from git tracking**

`.claude/` should already be in `.gitignore` for agent client installs. Verify and remove from tracking:

```bash
git rm --cached .claude/skills/todos-manager/SKILL.md
```

If `.claude/` is not in `.gitignore`, add it:

```bash
# Append to .gitignore if not already present
grep -q '^\.claude/' .gitignore || echo '.claude/' >> .gitignore
```

Verify `.agents/skills/todos-manager/SKILL.md` is also not tracked (it's empty, so shouldn't be):

```bash
git ls-files .agents/skills/todos-manager/
```

If tracked, remove:

```bash
git rm --cached .agents/skills/todos-manager/SKILL.md 2>/dev/null || true
```

- [ ] **Step 3: Add new location to git**

```bash
git add skills/todos-manager/SKILL.md
```

- [ ] **Step 4: Verify git state**

```bash
git status --short
# Expected: M skills/todos-manager/SKILL.md (new, staged)
# Expected: D .claude/skills/todos-manager/SKILL.md (removed from tracking)
```

- [ ] **Step 5: Commit**

```bash
git commit -m "skill(todos-manager): move to platform-neutral location skills/todos-manager/"
```

---

### Task 1: Rewrite SKILL.md — Schema & ID Rules

**Files:**
- Modify: `skills/todos-manager/SKILL.md` — Replace `## TODOS.md Schema` and `## Stable TODO-<n> ID Assignment` sections

**Interfaces:**
- Consumes: Design doc `docs/superpowers/specs/2026-07-09-todos-manager-v2-design.md` Sections 1 and 4
- Produces: Updated schema section with correct field names, updated ID rules referencing archive file

- [ ] **Step 1: Update frontmatter version**

Replace `version: 2.0.0` with `version: 2.1.0` in the SKILL.md frontmatter.

- [ ] **Step 2: Replace `## TODOS.md Schema` section**

Replace the entire `## TODOS.md Schema` section with:

```markdown
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
```

- [ ] **Step 3: Replace `## Stable TODO-<n> ID Assignment` section**

Replace the entire `## Stable TODO-<n> ID Assignment` section with:

```markdown
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
```

- [ ] **Step 4: Commit**

```bash
git add skills/todos-manager/SKILL.md
git commit -m "skill(todos-manager): rewrite schema and ID rules to match real TODOS.md format"
```

---

### Task 2: Rewrite SKILL.md — Workflow with 5 Subcommands

**Files:**
- Modify: `skills/todos-manager/SKILL.md` — Replace `## First-run Bootstrap`, `## Workflow`, add `## Preamble Template`, add `## Audit Report Format`

**Interfaces:**
- Consumes: Updated schema from Task 1, design doc Section 2 (Skill Workflow)
- Produces: 5 subcommand workflows replacing single-workflow structure

- [ ] **Step 1: Replace `## First-run Bootstrap` section**

Replace with `--init` subcommand workflow:

```markdown
## First-run Bootstrap (`--init`)

When the user invokes `todos-manager --init` on a project with no TODOS.md:

1. **Check if TODOS.md exists** at repo root.
   - If absent, create TODOS.md with preamble blockquote (see ## Preamble Template below).
2. **Create TODOS-archive.md** at repo root with minimal header:
   ```markdown
   # TODOS Archive

   Completed TODOs, archived via `todos-manager --archive`.
   ```
3. **Initialize `.hermes/todo_id_counter`** to 0 (if `.hermes/` directory exists).
4. **Print:** "✓ TODOS.md initialized. Use `todos-manager --add` to add entries."
```

- [ ] **Step 2: Add `## Preamble Template` section**

Insert after `## First-run Bootstrap` section:

```markdown
## Preamble Template

When creating or converting TODOS.md, use this blockquote as the file header:

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
```

- [ ] **Step 3: Replace `## Workflow` section**

Replace the entire `## Workflow` section with five subcommand workflows:

```markdown
## Workflow

The skill supports five subcommands. Each has its own workflow below.

### `--add`: Add new entry with schema enforcement

1. **Validate context:** Does TODOS.md exist? If not, prompt to run `--init` first.
2. **Compute next TODO-<n>:** Scan TODOS.md + TODOS-archive.md (see ## Stable TODO-<n> ID Assignment).
   - **Output to user:** "Next ID will be `TODO-<n>`."
3. **Prompt for title:** "Enter the TODO title (required):"
   - Validation: 10–200 characters, non-empty.
4. **Prompt for summary:** "One-line summary after the em dash (required):"
   - Validation: Non-empty, 10–100 characters.
5. **Prompt for required fields:**
   - **What:** "What needs to be done? (required):" — Free text, non-empty.
   - **Why:** "Why does this task matter? (required):" — Free text, 10–200 characters.
   - **Decisions:** Prompt for key decisions — guide user through: Priority (`P0`-`P3`), Effort (`S`/`M`/`L`), Phase, Branch name, Test Coverage (`필요`/`불필요`), Security Review (`필요`/`불필요`).
6. **Prompt for optional fields:**
   - **Pros:** Benefits (optional, free text)
   - **Cons:** Risks/drawbacks (optional, free text)
   - **Context:** References, file pointers (optional, free text)
   - **Depends on:** Comma-separated TODO-<n> references (optional; validate each exists in TODOS.md or TODOS-archive.md)
   - **Assumptions:** Preconditions (optional, free text)
7. **Assemble entry in memory** — Format per ## TODOS.md Schema example. Do **not** write to disk yet.
8. **Preview gate:** Show assembled entry exactly as it will appear:
   ```
   ======== PREVIEW ========
   - [ ] TODO-<n>: <Title> — <Summary>
     - **What:** ...
     - **Why:** ...
     [all fields]
   ======== END PREVIEW ========

   Proceed? [y / edit / cancel]
   ```
   - **`y`** → Proceed to step 9.
   - **`edit`** → Jump back to step 5 (no ID burned, no files written).
   - **`cancel`** → Abort. Print: "Entry discarded."
9. **Write to TODOS.md:** Insert formatted entry at end of file (before blank lines, after last entry).
10. **Update counter cache:** Write next_id to `.hermes/todo_id_counter` if `.hermes/` exists.
11. **Confirm:** "✓ Entry added as TODO-<n>."

### `--convert`: Convert existing TODOS.md to enforced format

1. **Read TODOS.md.** If absent, print "TODOS.md not found. Run `todos-manager --init` first." and exit.
2. **Check for preamble:** If blockquote format rules are absent, insert preamble (see ## Preamble Template) after `# TODOS` header.
3. **Validate each entry:** Scan for TODO-<n> entries. For each entry, check:
   - Required fields present: **What:**, **Why:**, **Decisions:**
   - Status marker is one of `[ ]`, `[→]`, `[x]`, `[~]`
   - ID matches `TODO-<digits>` pattern
4. **Report findings:** Output structured report (see ## Audit Report Format).
5. **Do not auto-fix.** Leave entry bodies as-is. Report only.

### `--audit`: Audit TODOS.md for format compliance

1. **Scan TODOS.md** for all TODO-<n> entries.
2. **Scan TODOS-archive.md** (if exists) for archived TODO-<n> entries.
3. **Per-entry checks:**
   - Required fields: **What:**, **Why:**, **Decisions:** present?
   - Status marker valid?
   - ID format correct?
   - Dependency references (if any) exist in TODOS.md or TODOS-archive.md?
4. **Cross-entry checks:**
   - ID sequence contiguous? (gaps OK, just report)
   - Counter cache (`.hermes/todo_id_counter`) matches max scanned ID?
5. **Output report** per ## Audit Report Format below.
   Report only — no automatic fixes.

### `--archive`: Move completed TODOs to archive

1. **Scan TODOS.md** for `[x]` entries (header line + all sub-bullets until next `- [ ]).
2. **If no `[x]` entries found:** Print "No completed TODOs to archive." and exit.
3. **If TODOS-archive.md does not exist:** Create it with minimal header:
   ```markdown
   # TODOS Archive

   Completed TODOs, archived via `todos-manager --archive`.

   Archived: <ISO-8601 timestamp>
   ```
4. **For each `[x]` entry (newest first by ID):**
   - Extract entry (header line + sub-bullets)
   - Append to end of TODOS-archive.md
5. **Remove archived entries from TODOS.md.**
6. **Confirm:** "✓ Archived N entries to TODOS-archive.md."
```

- [ ] **Step 4: Add `## Audit Report Format` section**

Insert after `## Workflow`:

```markdown
## Audit Report Format

```markdown
## TODOS.md Audit Report

Schema version: 2.0
Scanned: TODOS.md (N entries), TODOS-archive.md (M entries)
ID range: 1-max_id

Issues found: K
- TODO-X: Missing required field **Decisions:**
- TODO-Y: Invalid dependency reference `TODO-Z` (not found)
- TODO-W: Status marker `[->]` — expected `[→]`

ID gap check: OK (max=23, counter=23)
```

Report only — no automatic fixes.
```

- [ ] **Step 5: Commit**

```bash
git add skills/todos-manager/SKILL.md
git commit -m "skill(todos-manager): add 5 subcommand workflows (--init/--convert/--add/--audit/--archive)"
```

---

### Task 3: Rewrite SKILL.md — Update Purpose, Scenarios, Error Messages

**Files:**
- Modify: `skills/todos-manager/SKILL.md` — Update `## Purpose`, `## Error Messages`, and `## Acceptance Scenarios` sections

**Interfaces:**
- Consumes: Updated schema from Task 1, updated workflow from Task 2
- Produces: Consistent Purpose, Error Messages, and Scenarios aligned with v2 schema

- [ ] **Step 1: Update `## Purpose` section**

Replace the current `## Purpose` section with:

```markdown
## Purpose

The **todos-manager** skill automates the addition and management of TODOS.md entries in gstack-format projects. It enforces the canonical schema (What/Why/Decisions + optional fields), stable TODO-<n> ID assignment, and provides a preview/confirm gate before writing to disk. Completed TODOs can be archived to keep TODOS.md clean.

### When to use

- Adding a new entry to an existing TODOS.md file (`--add`)
- Initializing TODOS.md in a new project (`--init`)
- Converting an existing TODOS.md to enforced format (`--convert`)
- Auditing TODOS.md for format compliance (`--audit`)
- Archiving completed TODOs to TODOS-archive.md (`--archive`)

### Prerequisite state

- Project has a canonical `TODOS.md` file at the repo root (or create one with `--init`)
- TODOS.md follows the gstack schema (see ## TODOS.md Schema)
- User has write access to TODOS.md
```

- [ ] **Step 2: Update `## Error Messages` section**

Replace current error messages with updated versions matching the new schema. Key changes:
- Remove old field references (Assigned To, Estimate, Rationale, Status enum)
- Add new field error messages (What, Why, Decisions)
- Keep T8 convention (path + remediation verb)

```markdown
## Error Messages

### T8 convention: Path + remediation verb

Each error message **names the absolute file path** and a one-line action verb. Examples:

```
Error: /path/to/TODOS.md does not exist.
Remediation: Create the file or run `todos-manager --init`.

Error: Title must be 10–200 characters.
Remediation: Edit your input and re-enter the title.

Error: **What:** field is empty.
Remediation: Describe what needs to be done (required).

Error: **Why:** field must be 10–200 characters.
Remediation: Provide a rationale for why this task matters.

Error: **Decisions:** field is missing.
Remediation: Set key decisions: Priority, Effort, Phase, Branch, Test Coverage, Security Review.

Error: Dependency TODO-99 does not exist in TODOS.md or TODOS-archive.md.
Remediation: Check the list of valid IDs or remove TODO-99 from the depends_on list.

Error: Status marker "[->]" is not recognized.
Remediation: Use one of: [ ] pending, [→] in progress, [x] done, [~] on hold.
```

### Error & Rescue Map

| Error | Root Cause | Remediation |
|-------|-----------|-------------|
| TODOS.md not found | First-run on new project | Run `todos-manager --init` |
| Title is empty or too short | Invalid input | Re-enter title (10–200 characters) |
| **What:** is empty | Missing required field | Re-enter What description |
| **Why:** is too short or too long | Invalid input | Re-enter Why (10–200 characters) |
| **Decisions:** is missing | Missing required field | Provide key decisions with backtick-delimited values |
| Dependency TODO-<n> does not exist | Invalid reference | Verify TODO-<n> exists in TODOS.md or archive |
| Invalid status marker | Typo in marker | Use one of: [ ], [→], [x], [~] |
```

- [ ] **Step 3: Update `## Acceptance Scenarios` section**

Replace the current scenarios with updated versions using the new field names:

```markdown
## Acceptance Scenarios

### Scenario A1: Happy path — `--add` with all fields

**Setup:**
- TODOS.md exists with entries TODO-1 through TODO-5.
- TODOS-archive.md does not exist yet.
- No uncommitted changes in the repo.

**Walkthrough:**
1. User invokes `todos-manager --add`.
2. Skill computes next ID: `TODO-6`.
3. Skill prompts for title; user enters "Implement rate-limiting in API server".
4. Skill prompts for summary; user enters "Prevent API overload under load".
5. Skill prompts for **What:** — user enters description.
6. Skill prompts for **Why:** — user enters "Critical for production stability".
7. Skill guides through Decisions: P1, M, Phase 4, Branch, Test 필요, Security 불필요.
8. Skill prompts for optional fields (Pros, Cons, Context, Depends on, Assumptions).
9. **Skill shows preview gate; user types `y`.**
10. Skill inserts entry at end of TODOS.md.
11. Skill prints "✓ Entry added as TODO-6."

**Expected outcome:**
- TODOS.md contains the new entry formatted per schema.
- Entry uses new field names (What, Why, Decisions, etc.).

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

### Scenario A3: `--convert` on existing TODOS.md without preamble

**Setup:**
- TODOS.md exists with entries but no preamble blockquote.

**Walkthrough:**
1. User invokes `todos-manager --convert`.
2. Skill detects missing preamble, inserts it after `# TODOS` header.
3. Skill validates each entry against schema.
4. Skill outputs audit report listing any missing required fields.
5. Skill does not rewrite entry bodies.

**Expected outcome:**
- TODOS.md now has preamble blockquote.
- Entry bodies unchanged.
- Report surfaces any schema violations.

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
```

- [ ] **Step 4: Commit**

```bash
git add skills/todos-manager/SKILL.md
git commit -m "skill(todos-manager): update purpose, error messages, and scenarios for v2"
```

---

### Task 4: Add Preamble to TODOS.md

**Files:**
- Modify: `TODOS.md` — Replace current header with preamble blockquote

**Interfaces:**
- Consumes: Preamble template from SKILL.md (Task 2)
- Produces: Updated TODOS.md with enforcement preamble

- [ ] **Step 1: Replace TODOS.md header**

Replace the current header (lines 1-3):

```markdown
# TODOS

gstack-format work queue for `todo-pipeline-orchestrator`. Each entry keeps the required fields: What/Why/Pros/Cons/Context/Depends on/Decisions. Status markers: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold. See `docs/gstack/hyonchoi-main-design-20260610-195349.md` ("TODOS Manager Skill") for the full schema and `TODO-<n>` ID assignment rules.
```

With:

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

- [ ] **Step 2: Verify TODOS.md structure**

Check that the preamble is followed by a blank line, then the first TODO entry. No section headers (`##`) between preamble and entries.

- [ ] **Step 3: Commit**

```bash
git add TODOS.md
git commit -m "docs(TODOS.md): add format enforcement preamble blockquote"
```

---

### Task 5: Create Install Script

**Files:**
- Create: `scripts/install-todos-manager.sh` — Symlink skill to user-level skill directories

**Interfaces:**
- Consumes: `skills/todos-manager/SKILL.md` (project source)
- Produces: Symlinks in `~/.claude/skills/todos-manager/` and/or `~/.agents/skills/todos-manager/`

- [ ] **Step 1: Create install script**

Write `scripts/install-todos-manager.sh`:

```bash
#!/usr/bin/env bash
# Install todos-manager skill from project source to user-level skill directories.
# Usage: scripts/install-todos-manager.sh
#
# Detects which skill directories exist (~/.claude/skills, ~/.agents/skills)
# and creates symlinks so the agent client can discover the skill.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE_SKILL="$PROJECT_ROOT/skills/todos-manager/SKILL.md"

# Verify source exists
if [[ ! -f "$SOURCE_SKILL" ]]; then
  echo "Error: Source skill not found at $SOURCE_SKILL"
  echo "Remediation: Ensure skills/todos-manager/SKILL.md exists in the project root."
  exit 1
fi

INSTALLED=0

install_to() {
  local target_dir="$1"
  local target_link="$target_dir/todos-manager/SKILL.md"

  # Skip if directory doesn't exist
  if [[ ! -d "$target_dir" ]]; then
    echo "  Skip $target_dir (not found)"
    return 0
  fi

  # Remove existing file or symlink
  if [[ -e "$target_link" || -L "$target_link" ]]; then
    echo "  Updating existing link: $target_link"
    rm -f "$target_link"
  fi

  # Create symlink
  ln -sf "$SOURCE_SKILL" "$target_link"
  echo "  ✓ Linked: $target_link → $SOURCE_SKILL"
  INSTALLED=$((INSTALLED + 1))
}

echo "Installing todos-manager skill..."
echo "  Source: $SOURCE_SKILL"
echo ""

# Claude Code user skills
install_to "$HOME/.claude/skills"

# Agents user skills
install_to "$HOME/.agents/skills"

echo ""
if [[ $INSTALLED -gt 0 ]]; then
  echo "✓ Installed to $INSTALLED location(s). Restart your agent client to discover the skill."
else
  echo "⚠ No skill directories found. Create ~/.claude/skills/ or ~/.agents/skills/ and re-run."
fi
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/install-todos-manager.sh
```

- [ ] **Step 3: Verify script runs**

```bash
bash scripts/install-todos-manager.sh
# Expected output: symlinks created, confirmation message
```

- [ ] **Step 4: Verify symlinks**

```bash
ls -la ~/.claude/skills/todos-manager/SKILL.md 2>/dev/null
ls -la ~/.agents/skills/todos-manager/SKILL.md 2>/dev/null
# Expected: both point to skills/todos-manager/SKILL.md
```

- [ ] **Step 5: Commit**

```bash
git add scripts/install-todos-manager.sh
git commit -m "scripts: add install-todos-manager.sh for user-level skill installation"
```

---

### Task 6: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` — Add todos-manager reference section with install note

**Interfaces:**
- Consumes: SKILL.md updates from Tasks 1-3, install script from Task 5
- Produces: Updated CLAUDE.md with todos-manager enforcement reference

- [ ] **Step 1: Add todos-manager section to CLAUDE.md**

Append after the existing `## Tooling` section:

```markdown
## TODOS.md management

- Use the `todos-manager` skill for all TODOS.md mutations (add, convert, audit, archive).
- TODOS.md format is enforced — see preamble blockquote in TODOS.md for schema rules.
- Skill source: `skills/todos-manager/SKILL.md`. Install via `scripts/install-todos-manager.sh` to symlink to `~/.claude/skills/todos-manager/` and/or `~/.agents/skills/todos-manager/`.
- Subcommands: `--add` (new entry), `--init` (new project), `--convert` (add preamble + validate), `--audit` (format check), `--archive` (move `[x]` to TODOS-archive.md).
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): add todos-manager skill reference with install instructions"
```

---

### Task 7: Cleanup & Final Verification

**Files:**
- Modify: `docs/superpowers/specs/2026-07-09-todos-manager-v2-design.md` — Update status
- Cleanup: Remove stale `.agents/skills/todos-manager/` if present

**Interfaces:**
- Consumes: Completed SKILL.md, TODOS.md, CLAUDE.md, install script from Tasks 0-6
- Produces: Verified installation, updated design doc status

- [ ] **Step 1: Run install script**

```bash
bash scripts/install-todos-manager.sh
```

Confirm symlinks are working (both `.claude` and `.agents` paths point to `skills/todos-manager/SKILL.md`).

- [ ] **Step 2: Verify skill discovery**

Check that the skill appears in the agent skill list:

```bash
ls ~/.claude/skills/todos-manager/SKILL.md
ls ~/.agents/skills/todos-manager/SKILL.md
```

- [ ] **Step 3: Verify git state**

```bash
git status --short
# Expected: No untracked files (except .claude/worktrees/ and docs/gstack plan)
# Expected: skills/todos-manager/SKILL.md tracked
# Expected: scripts/install-todos-manager.sh tracked
# Expected: TODOS.md tracked
# Expected: CLAUDE.md tracked
```

- [ ] **Step 4: Update design doc status**

In `docs/superpowers/specs/2026-07-09-todos-manager-v2-design.md`, update status line:
```
**Status:** Implementation plan written
```

- [ ] **Step 5: Final commit**

```bash
git add docs/superpowers/specs/2026-07-09-todos-manager-v2-design.md
git commit -m "docs: update todos-manager v2 design doc status"
```

---

## Self-Review

**Spec coverage:**
- Schema definition -> Task 1 ✓
- 5 subcommands workflow -> Task 2 ✓
- Preamble template -> Task 2 (step 2) + Task 4 (apply to TODOS.md) ✓
- Archive behavior -> Task 2 (`--archive` workflow) ✓
- Audit report format -> Task 2 (step 4) ✓
- CLAUDE.md reference -> Task 6 ✓
- Purpose/Error/Scenarios update -> Task 3 ✓
- Platform-neutral skill location -> Task 0 ✓
- Install script -> Task 5 ✓
- Design doc commit -> Task 7 ✓

**Placeholder scan:** No TBD, TODO, "implement later", or vague instructions. All code blocks contain actual content.

**Type consistency:**
- Schema field names consistent across all tasks
- Status markers consistent (`[ ]`, `[→]`, `[x]`, `[~]`)
- Preamble template matches Task 1 schema
- Error messages reference correct field names
- SKILL.md path: `skills/todos-manager/SKILL.md` used consistently in Tasks 0-7
- Install script creates correct symlinks to user-level directories
