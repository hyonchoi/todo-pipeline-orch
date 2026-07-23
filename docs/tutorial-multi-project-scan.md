# Set up multi-project scanning

In this tutorial, you'll create two test projects and run a single `pipeline-watch tick` that
selects a TODO in each project. You'll see how the scan loop discovers projects,
configures per-project Slack channels, and archives projects without deleting them.

**Time: ~15 minutes**

## What you'll need

- `todo-pipeline-orchestrator` installed (see [README prerequisites](../README.md#requirements))
- Python 3.12+ and uv package manager
- Hermes CLI installed and authenticated (`hermes login`)
- Hermes kanban configured for your project

---

## Step 1: Create two test projects

Create a directory for your projects and two project directories inside it:

```bash
export PIPELINE_PROJECTS_DIR=~/my-projects
mkdir -p ~/my-projects/demo-app ~/my-projects/second-app

# Set up demo-app
cd ~/my-projects/demo-app
git init
mkdir .hermes
echo "0" > .hermes/todo_id_counter

cat > TODOS.md << 'EOF'
# TODOS

## TODO-1: Add user authentication

**What:** Implement basic JWT authentication.

**Why:** Users need to log in securely.

**Status:** `[→]` (in progress)
EOF

git add .
git commit -m "init: create project with TODOS"
```

Now set up the second project:

```bash
cd ~/my-projects/second-app
git init
mkdir .hermes
echo "0" > .hermes/todo_id_counter

cat > TODOS.md << 'EOF'
# TODOS

## TODO-1: Add database connection pool

**What:** Set up a connection pool for PostgreSQL.

**Why:** Avoid opening a new connection on every request.

**Status:** `[→]` (in progress)
EOF

git add .
git commit -m "init: create project with TODOS"
```

You now have two projects that pipeline-watch can discover.

---

## Step 2: Run the scan loop

Run a single tick without specifying a project:

```bash
uv run pipeline-watch tick
```

You'll see output like:

```
discovered 2 active projects
project demo-app: selection result: picked=TODO-1
project demo-app: registered 4 kanban tasks for TODO-1
project second-app: selection result: picked=TODO-1
project second-app: registered 4 kanban tasks for TODO-1
```

One tick, two projects, one global lock. No cron per project needed.

---

## Step 3: Configure per-project Slack channels

Create a `project.toml` in the second project to set its Slack channel:

```bash
cat > ~/my-projects/second-app/.hermes/project.toml << 'EOF'
[active]
enabled = true

[notifications]
slack_channel = "project__second-app"
EOF
```

Now alerts for second-app go to `#project__second-app`, while demo-app falls
back to the global `PIPELINE_SLACK_CHANNEL` or `#alert`.

Run another tick to verify:

```bash
uv run pipeline-watch tick
```

The scan loop should still discover both projects. The second project now uses
its own Slack channel for notifications.

---

## Step 4: Archive a project

Set the second project to inactive:

```bash
cat > ~/my-projects/second-app/.hermes/project.toml << 'EOF'
[active]
enabled = false
EOF
```

Run the tick again:

```bash
uv run pipeline-watch tick
```

You should see:

```
discovered 1 active projects
project demo-app: selection result: picked=...
```

Second-app was skipped — no need to delete its `TODOS.md`. To re-enable it,
set `enabled = true` or delete the `project.toml` file.

---

## Step 5: Check per-project state

Each project now has its own state directory:

```bash
ls ~/my-projects/demo-app/.hermes/
# current_tick_id.txt  circuit.json  decisions/  outcomes/  phase_started/

ls ~/my-projects/second-app/.hermes/
# project.toml  todo_id_counter
```

Notice: demo-app has tick state because it ran selection. Second-app is archived,
so it has no tick state — only the `project.toml` and counter.

---

## What you built

You now have a multi-project setup that:

- Discovers active projects automatically via `_discover_projects`
- Runs one selection per project under a single global lock
- Uses per-project Slack channels for notifications
- Archives projects without deleting `TODOS.md`
- Shares one cron entry (`hermes cron set pipeline-tick */5 * * * *`)

### Next steps

**Set up production cron:**

```bash
hermes cron set pipeline-tick '*/5 * * * *'
```

See [Pipeline state machine](hermes-state-machine.md) for the tick lifecycle.

**Understand the scan loop architecture:**

- Read [How the multi-project scan loop works](explanation-multi-project-scan.md) for why
  a single global lock guards the entire scan.
- Read [How to troubleshoot state migration](howto-troubleshoot-state-migration.md) for
  fixing migration issues when you have multiple projects.

**Deep-dive:**

- [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) — how phases map to kanban tasks
- [Configure `.hermes/config.toml`](howto-config-toml.md) — tuning selection model and circuit breaker
