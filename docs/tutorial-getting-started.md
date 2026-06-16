# Getting Started with pipeline-watch

In this tutorial, you'll set up your first pipeline-watched project and run the core workflows: configuring the pipeline, reviewing TODOs, and merging one to main. By the end, you'll have a working automation loop driven by Hermes cron.

**Time: ~15 minutes**

## What you'll need

- `todo-pipeline-orchestrator` installed (see [README prerequisites](../README.md#requirements))
- A git repository with at least one project containing a `TODOS.md` file
- Python 3.12+ and uv package manager
- Hermes CLI installed and authenticated (`hermes login`)
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
pipeline-watch 0.3.0
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

Pipeline-watch discovers projects by scanning a directory you specify. Tell it where your projects are:

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

## Step 4: Configure the Hermes cron tick

The pipeline is driven by Hermes cron. Hermes discovers projects, detects
TODOS.md changes, selects eligible TODOs via the Hermes agent, and runs the
phase loop. Set up the tick schedule:

```bash
hermes cron set pipeline-tick '*/5 * * * *'
```

Verify the schedule is active:

```bash
hermes cron list
```

You should see an entry for `pipeline-tick` with the `*/5` schedule.

The first tick may take up to 5 minutes to fire. While you wait, move on to
the next steps.

---

## Step 5: Check pipeline status

Once the Hermes cron tick has run, check what TODOs are pending:

```bash
uv run pipeline-watch status
```

When TODOs are ready for review, you'll see a table like:

```
Project | TODO ID | Status           | Age
--------|---------|------------------|------
demo-app| 1       | ready-for-review | 2m
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

## What you built

You now have a working pipeline-watch setup that:

✅ Discovers projects with TODOS.md files via Hermes cron  
✅ Detects status changes and selects eligible TODOs via Hermes agent  
✅ Displays pending records in a table  
✅ Merges TODOs to main with version bumping  
✅ Runs automatically every 5 minutes via Hermes cron  

### Next steps

**Explore the full feature set:**
- Read [Configuration](../README.md#configuration) to customize `PIPELINE_LOCK_DIR`, `PIPELINE_STATE_DIR`, etc.
- See [Troubleshooting](../README.md#troubleshooting) for common issues and fixes

**Integrate into your workflow:**
- Set environment variables in your shell profile (`.bashrc`, `.zshrc`, etc.)
- Add `PIPELINE_PROJECTS_DIR` to point to your real projects directory
- Monitor pipeline state in `~/.hermes/` (decisions, outcomes, phase_started)

**Understand the architecture:**
- Read [Architecture](../README.md#architecture) to see how pipeline-watch orchestrates the phases and merges
- Check [docs/pipeline-modularization-plan.md](pipeline-modularization-plan.md) for the full design

**When things break:**
- Check [Troubleshooting](../README.md#troubleshooting) for solutions to "command not found: uv", merge hangs, and other issues
- Run `pipeline-watch kill` to stop stuck phases — see [docs/howto-kill-stuck-phase.md](howto-kill-stuck-phase.md)
- Check Hermes logs in `~/.hermes/`
