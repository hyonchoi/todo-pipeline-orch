# Getting Started with pipeline-watch

In this tutorial, you'll set up your first pipeline-watched project and run all three core workflows: discovering TODOs, reviewing them, and merging one to main. By the end, you'll have a working automation loop that can run automatically every 5 minutes.

**Time: ~15 minutes**

## What you'll need

- `todo-pipeline-orchestrator` installed (see [README prerequisites](../README.md#requirements))
- A git repository with at least one project containing a `TODOS.md` file
- Python 3.9+ and uv package manager
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
pipeline-watch 0.1.0
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

## Step 4: Run auto-tick discovery

The `auto` command scans all projects in `PIPELINE_PROJECTS_DIR`, detects changes to `TODOS.md`, and determines which TODOs are eligible for review. Run it:

```bash
uv run pipeline-watch auto
```

You'll see log output indicating discovery. The command looked at your demo-app project, parsed the TODOS.md, and identified that TODO-1 exists but is not yet ready for review (status is still `[ ]`).

Mark TODO-1 as ready by updating its status in TODOS.md:

```bash
cd ~/my-projects/demo-app
# Edit TODOS.md: change TODO-1 status from [ ] to [→] (in progress)
sed -i 's/Status: `\[ \]`/Status: `[→]`/' TODOS.md
git add TODOS.md
git commit -m "TODO-1: mark in progress"
```

Now run auto-tick again:

```bash
uv run pipeline-watch auto
```

Check status:

```bash
uv run pipeline-watch status
```

Expected output (or similar):
```
Project | TODO ID | Status        | Age
--------|---------|---------------|--------
demo-app| 1       | ready-for-review | 1s
```

Excellent! The auto-tick command detected the status change and marked TODO-1 as ready for review.

---

## Step 5: Merge a TODO

Now merge TODO-1 to main. The merge command runs Phase 9 of the pipeline: it confirms the merge, bumps the version, and commits to main.

```bash
uv run pipeline-watch merge demo-app 1
```

The command will:
1. Ask for confirmation (press `y` to confirm)
2. Bump the project's version (if a VERSION file exists)
3. Execute `git merge --ff-only <branch> -m "Merge TODO-1 ..."`
4. Record the merge in the pipeline state

If you don't have a VERSION file or git main branch set up, the merge will fail gracefully with a helpful error. For a full end-to-end test, set up main branch:

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

## Step 6: Automate with cron

So far you've run commands manually. To automate, register a cron job that runs auto-tick every 5 minutes:

```bash
bash hermes-pipeline/scripts/install-cron.sh
```

This command:
1. Detects your uv environment
2. Creates a crontab entry: `*/5 * * * * cd $PROJ && uv run pipeline-watch auto >> ~/.hermes/cron.log 2>&1`
3. Logs output to `~/.hermes/cron.log`

Verify the cron job was installed:

```bash
crontab -l | grep pipeline-watch
```

You should see an entry for `pipeline-watch auto`.

Check the log to see past runs:

```bash
tail -20 ~/.hermes/cron.log
```

---

## What you built

You now have a working pipeline-watch setup that:

✅ Discovers projects with TODOS.md files  
✅ Detects status changes and marks TODOs ready for review  
✅ Displays pending records in a table  
✅ Merges TODOs to main with version bumping  
✅ Runs automatically every 5 minutes via cron  

### Next steps

**Explore the full feature set:**
- Read [Configuration](../README.md#configuration) to customize `PIPELINE_LOCK_DIR`, `PIPELINE_CLAUDE_CMD`, etc.
- See [Troubleshooting](../README.md#troubleshooting) for common issues and fixes

**Integrate into your workflow:**
- Set environment variables in your shell profile (`.bashrc`, `.zshrc`, etc.)
- Add `PIPELINE_PROJECTS_DIR` to point to your real projects directory
- Monitor cron output in `~/.hermes/cron.log`

**Understand the architecture:**
- Read [Architecture](../README.md#architecture) to see how pipeline-watch orchestrates the discovery, selection, and merge phases
- Check [docs/pipeline-modularization-plan.md](pipeline-modularization-plan.md) for the full design

**When things break:**
- Check [Troubleshooting](../README.md#troubleshooting) for solutions to "command not found: uv", merge hangs, and other issues
- Run commands with verbose output to debug (check logs in `~/.hermes/`)
