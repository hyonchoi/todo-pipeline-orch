# Getting Started with pipeline-watch

In this tutorial, you'll set up your first pipeline-watched project and run the core workflows: triggering a tick, reviewing TODOs, and merging one to main. By the end, you'll have a working pipeline — with an optional cron schedule for production.

**Time: ~10 minutes**

## What you'll need

- `todo-pipeline-orchestrator` installed (see [README prerequisites](../README.md#requirements))
- Python 3.12+ and uv package manager
- Hermes CLI installed and authenticated (`hermes login`)
- Hermes kanban configured for your project
- Write permissions on your git repositories

If you don't have a test project yet, the setup section below will guide you through creating one.

---

## Step 1: Verify installation

First, confirm that `pipeline-watch` is installed and working:

```bash
uv run pipeline-watch --version
```

Expected output:
```
pipeline-watch 0.3.3
```

If you see "command not found," run:
```bash
uv sync
```

This installs the CLI into your current uv environment.

---

## Step 2: Set up your first project

The pipeline watches directories for `TODOS.md` files. Create a test project with the required structure:

```bash
# Create a test directory structure
mkdir -p ~/my-projects/demo-app
cd ~/my-projects/demo-app
git init
mkdir .hermes
echo "0" > .hermes/todo_id_counter
```

Now create a minimal `TODOS.md` file:

```bash
cat > TODOS.md << 'EOF'
# TODOS

## TODO-1: Add user authentication

**What:** Implement basic JWT authentication.

**Why:** Users need to log in securely.

**Status:** `[ ]` (pending)
EOF
```

Commit this to git:

```bash
git add .
git commit -m "init: create project with TODOS"
```

You now have a project that pipeline-watch can discover. Next, tell pipeline-watch where to find it.

---

## Step 3: Configure pipeline-watch

Pipeline-watch discovers projects by scanning a directory you specify via `PIPELINE_PROJECTS_DIR`. The default is `~/projects`. Tell it where your projects are:

```bash
export PIPELINE_PROJECTS_DIR=~/my-projects
```

(In production, you'd add this to your shell profile or systemd environment file.)

Verify the configuration:

```bash
uv run pipeline-watch status
```

Expected output:
```
No pending records
```

This is correct — no TODOs are ready for review yet. The status command shows TODOs that are eligible to merge.

---

## Step 4: Run a manual tick

The `tick` command runs one pipeline tick immediately: it scans all active projects in your `projects_dir`, checks for in-flight work from a previous tick, observes outcomes, acquires a tick lock, runs selection via the Hermes agent, and registers phases as kanban tasks. This is the fastest way to see the pipeline in action.

```bash
uv run pipeline-watch tick
```

You'll see log output as the tick runs through each active project. The Hermes agent evaluates TODOS.md files and picks a TODO (or returns `picked=None` if nothing is ready yet).

Check the decision record:

```bash
jq '{picked: .picked, rationale: .rationale}' \
  .hermes/decisions/$(ls -t .hermes/decisions/ | head -1)
```

If you see `picked=None`, mark TODO-1 as in progress and run the tick again:

```bash
# Edit TODOS.md: change the status from `[ ]` to `[→]`
cd ~/my-projects/demo-app
# ... edit TODOS.md to set Status: `[→]` ...
git add TODOS.md
git commit -m "TODO-1: mark in progress"

uv run pipeline-watch tick
```

Run the tick a second time. This tick scans all active projects, observes outcomes from the prior tick (if any), and then runs selection again.

### Inspect the kanban board

If a TODO was picked, phases are now registered as kanban tasks with `--parent` dependency chains. Check the board:

```bash
hermes kanban list --tenant demo-app
```

You should see phases like `phase_2_autoplan` (running) and `phase_4_development` (ready — blocked on its parent).

See [reference-kanban-as-scheduler.md](reference-kanban-as-scheduler.md) for how the kanban-as-scheduler flow works.

---

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

---

## Step 5: Check pipeline status

Once a TODO has been selected and is progressing through phases, check the pending records:

```bash
uv run pipeline-watch status
```

When TODOs are ready for review, you'll see a table like:

```
PROJECT    | TODO | BRANCH               | PR | STATUS | AGE
-----------|------|----------------------|----|--------|-----
demo-app   | 1    | feature/todo-1-...   |    | pending| 5m
```

---

## Step 6: Merge a TODO

Now merge TODO-1 to main. The merge command runs Phase 9 of the pipeline: it confirms the merge, bumps the version, and commits to main.

```bash
uv run pipeline-watch merge demo-app 1
```

The command will:
1. Ask for confirmation (type the TODO ID to confirm)
2. Bump the project's version (if a VERSION file exists)
3. Execute `git merge --ff-only <branch> -m "Merge TODO-1 ..."`
4. Record the merge in the pipeline state

If you don't have a VERSION file or git main branch set up, the merge will fail gracefully with a helpful error. For a full end-to-end test, set up a main branch:

```bash
cd ~/my-projects/demo-app
git checkout -b main
# Merge will now succeed
```

To skip confirmation (useful in automation):

```bash
uv run pipeline-watch merge demo-app 1 --abandon
```

---

## Step 7: Automate with Hermes cron (optional)

So far you've been running `pipeline-watch tick` manually. For production, set up the Hermes cron schedule:

```bash
hermes cron set pipeline-tick '*/5 * * * *'
```

Verify the schedule is active:

```bash
hermes cron list
```

You should see an entry for `pipeline-tick` with the `*/5` schedule. The circuit breaker adjusts the interval automatically — 5-minute ticks normally, backing off to 30 minutes after repeated no-progress ticks.

---

## What you built

You now have a working pipeline-watch setup that:

✅ Discovers projects with TODOS.md files via Hermes agent  
✅ Selects TODOs and registers phases as kanban tasks with dependency chains  
✅ Displays pending records in a table  
✅ Merges TODOs to main with version bumping  
✅ Runs automatically every 5 minutes via Hermes cron (optional)  

### Next steps

**Explore the full feature set:**
- Read [Configuration](../README.md#configuration) to customize `PIPELINE_LOCK_DIR`, `PIPELINE_STATE_DIR`, etc.
- See [Troubleshooting](../README.md#troubleshooting) for common issues and fixes

**Run ticks iteratively during development:**
- See [How to run a manual tick](howto-pipeline-tick.md) for the full tick flow with verification and troubleshooting

**Understand the architecture:**
- Read [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) to understand how phases map to kanban tasks
- Read [Architecture](../README.md#architecture) to see how pipeline-watch orchestrates the phases and merges
- Check [docs/pipeline-modularization-plan.md](pipeline-modularization-plan.md) for the full design

**When things break:**
- Check [Troubleshooting](../README.md#troubleshooting) for solutions to "command not found: uv", merge hangs, and other issues
- Run `pipeline-watch kill` to stop stuck phases — see [docs/howto-kill-stuck-phase.md](howto-kill-stuck-phase.md)
- Check Hermes logs in `~/.hermes/`
