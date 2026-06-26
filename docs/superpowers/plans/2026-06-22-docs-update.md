# Multi-project Documentation Update

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update the tutorial, howto, and README to reflect the new `pipeline-watch tick` (no project argument) and `pipeline-watch kill` (optional project) commands, plus document `project.toml`.

**Architecture:** Three document updates plus a new howto for multi-project setup. No code changes.

**Tech Stack:** Markdown documentation.

## Global Constraints

- One TODO in flight per project (existing constraint)
- Single global lock for the scan — no two ticks overlap
- Hermes-only for LLM queries (TODO-6)
- Filesystem-based config — no database or network calls before selection
- Breaking change: `tick <project>` becomes `tick` (no argument)

---

### Context: Existing Docs

Existing docs to update:
- `docs/tutorial-getting-started.md` — first-time setup tutorial
- `docs/howto-pipeline-tick.md` — howto for the tick subcommand
- `docs/howto-kill-stuck-phase.md` — howto for kill
- `docs/howto-config-toml.md` — config.toml documentation
- `README.md` — project overview

Existing tutorials show `pipeline-watch tick myproject` with the project argument — all need updating.

---

### Task 1: Update tutorial-getting-started.md

**Files:**
- Modify: `docs/tutorial-getting-started.md`
- No new tests — documentation only.

**Interfaces:**
- Consumes: None
- Produces: Updated tutorial showing `pipeline-watch tick` (no project arg)

- [ ] **Step 1: Update tick references**

In `docs/tutorial-getting-started.md`:
- Replace all occurrences of `pipeline-watch tick <project>` or `pipeline-watch tick myproject` with `pipeline-watch tick`.
- Update the section that explains how the tick works to mention the scan loop (scans all active projects in `projects_dir`).
- Add a brief note about the `projects_dir` config and the default `~/projects`.

- [ ] **Step 2: Add project.toml section**

Add a new section after the tick explanation:

```markdown
## Project Configuration

Each project can have a `.hermes/project.toml` file to configure project-specific settings:

```toml
[active]
enabled = true  # default; set to false to archive a project

[notifications]
slack_channel = "project__my-slug"  # per-project alert channel
```

If the file doesn't exist, the project is active by default. To archive a project (stop scanning it without deleting TODOS.md), create the file with `enabled = false`.

**Slack channel resolution (priority):**
1. `project.toml`'s `slack_channel`
2. `PIPELINE_SLACK_CHANNEL` environment variable
3. `#alert` (hardcoded fallback)
```

- [ ] **Step 3: Update cron setup section**

Replace the cron entry example from `pipeline-watch tick myproject` to `pipeline-watch tick`.

- [ ] **Step 4: Commit**

```bash
git add docs/tutorial-getting-started.md
git commit -m "docs: update tutorial for multi-project tick scan

Update tick references to show pipeline-watch tick (no project arg).
Add project.toml configuration section. Update cron entry."
```

### Task 2: Update howto-pipeline-tick.md

**Files:**
- Modify: `docs/howto-pipeline-tick.md`
- No new tests — documentation only.

**Interfaces:**
- Consumes: None
- Produces: Updated howto with scan loop explanation

- [ ] **Step 1: Update command syntax**

Replace all references to `pipeline-watch tick <project>` with `pipeline-watch tick`. Update the flow diagram to show:

```
tick:
  1. Acquire global TickLock
  2. Discover active projects (scans projects_dir)
  3. For each project:
     a. Migrate per-project state (one-time)
     b. Check prior tick (per-project)
     c. Run selection (per-project)
     d. Register kanban phases or observe circuit breaker
  4. Release lock
```

- [ ] **Step 2: Add debugging section**

Add a note about debugging a single project's tick locally:

```markdown
## Debugging a Single Project

The scan loop runs over all active projects. To debug a specific project's
selection, temporarily set all other projects to `enabled = false` in their
`.hermes/project.toml`, or temporarily rename their `TODOS.md`.
```

- [ ] **Step 3: Update state directory section**

Update any references to `~/.hermes/` for tick state to mention that per-project state now lives at `<project>/.hermes/`. Global state (`tick.lock`, `config.toml`) remains in `~/.hermes/`.

- [ ] **Step 4: Commit**

```bash
git add docs/howto-pipeline-tick.md
git commit -m "docs: update howto-pipeline-tick for scan loop

Update command syntax, add scan loop flow diagram, add debugging section."
```

### Task 3: Update howto-kill-stuck-phase.md

**Files:**
- Modify: `docs/howto-kill-stuck-phase.md`
- No new tests — documentation only.

**Interfaces:**
- Consumes: None
- Produces: Updated kill howto with optional project arg

- [ ] **Step 1: Update kill command syntax**

Update references to `pipeline-watch kill` to show the new optional project argument:

```bash
# Kill all in-flight phases across all projects
pipeline-watch kill --all

# Kill a specific TODO across all projects
pipeline-watch kill --todo TODO-3
```

- [ ] **Step 2: Add multi-project context**

Add a note explaining that without a project argument, kill scans all projects for in-flight phases (reads `<project>/.hermes/phase_started/` markers).

- [ ] **Step 3: Commit**

```bash
git add docs/howto-kill-stuck-phase.md
git commit -m "docs: update kill howto for multi-project scanning

Add optional project argument and multi-project kill context."
```

### Task 4: Create howto-multi-project-setup.md

**Files:**
- Create: `docs/howto-multi-project-setup.md`
- No new tests — documentation only.

**Interfaces:**
- Consumes: None
- Produces: New howto document

- [ ] **Step 1: Write the howto document**

Create `docs/howto-multi-project-setup.md`:

```markdown
# Multi-Project Setup

`pipeline-watch tick` scans your projects directory and runs selection for
every active project in one cron execution. This howto covers setting up
multiple projects for the scan loop.

## Prerequisites

- All projects live under a single directory (default: `~/projects`).
- Each project has a `TODOS.md` file.
- Project directory names are valid slugs (alphanumeric, dot, dash, underscore; no leading dash or dot).

## Configuration

### Setting the Projects Directory

If your projects live outside `~/projects`, set the environment variable:

```bash
export PIPELINE_PROJECTS_DIR=/path/to/your/projects
```

Or set it in `~/.hermes/config.toml`:

```toml
projects_dir = "/path/to/your/projects"
```

### Per-Project Configuration

Create `.hermes/project.toml` in a project directory:

```bash
mkdir -p ~/projects/myproject/.hermes
cat > ~/projects/myproject/.hermes/project.toml << 'EOF'
[active]
enabled = true

[notifications]
slack_channel = "project__myproject"
EOF
```

### Archiving a Project

To pause selection for a project without deleting `TODOS.md`:

```bash
mkdir -p ~/projects/myproject/.hermes
cat > ~/projects/myproject/.hermes/project.toml << 'EOF'
[active]
enabled = false
EOF
```

The next tick will skip this project.

### Slack Channel Resolution

Alerts for each project go to the Slack channel determined by:
1. `project.toml`'s `[notifications] slack_channel`
2. `PIPELINE_SLACK_CHANNEL` environment variable
3. `#alert` (hardcoded fallback)

## Cron Setup

Replace the per-project cron entry with a single global entry:

```bash
# Before: one cron per project
# 0 * * * * pipeline-watch tick project-a
# 0 * * * * pipeline-watch tick project-b

# After: one cron for all projects
0 * * * * pipeline-watch tick
```

If using `install-cron.sh`, the script fires `pipeline-watch tick` (no project argument).

## State Migration

On the first run of `pipeline-watch tick` (no project argument), any existing
state files in `~/.hermes/` (`current_tick_id.txt`, `circuit.json`, `outcomes/`)
are automatically migrated to `<project>/.hermes/`. This is a one-time operation.

## Debugging

To debug a specific project's selection:
1. Set all other projects to `enabled = false` in their `.hermes/project.toml`
2. Run `pipeline-watch tick --debug`
3. Restore other projects' `enabled = true`

## Error Isolation

If one project's `TODOS.md` is malformed or an error occurs during selection,
the error is logged and the scan continues to the next project. One project's
failure does not block the others.
```

- [ ] **Step 2: Commit**

```bash
git add docs/howto-multi-project-setup.md
git commit -m "docs: add multi-project setup howto

New howto covering project discovery, project.toml, archiving,
slack channel resolution, cron setup, state migration, and debugging."
```

### Task 5: Update README.md

**Files:**
- Modify: `README.md`
- No new tests — documentation only.

**Interfaces:**
- Consumes: None
- Produces: Updated README with multi-project references

- [ ] **Step 1: Update README**

In `README.md`:
- Replace `pipeline-watch tick <project>` with `pipeline-watch tick`.
- Add a brief mention of multi-project scanning.
- Update any examples showing the project argument.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: update README for multi-project tick scan"
```

---
