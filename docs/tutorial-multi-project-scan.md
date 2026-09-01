# Set up multi-project scanning

In this tutorial, you'll create two test projects and run a single `tpo tick` that
selects a TODO in each project. You'll see how the scan loop discovers projects,
configures per-project Slack channels, and archives projects without deleting them.

**Time: ~15 minutes**

## What you'll need

- `todo-pipeline-orchestrator` installed (see [Getting Started](tutorial-getting-started.md#step-1-install-and-verify-tpo))
- Python 3.12+ and uv package manager
- The Hermes CLI on `PATH`, authenticated with `hermes login`, with an agent runtime/profile available when pipeline phases run
- Hermes kanban configured for your project
- `gh` >= 2.44 authenticated; each test project needs a github.com `origin`
  remote with at least one open issue labelled `tpo:todo` + `ready-for-agent`
  (see [issue tracker](agents/issue-tracker.md#tpo-backlog-items) and
  [ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md))

---

## Step 1: Create two test projects

Create a directory for your projects and clone two GitHub repositories into it.
Discovery keys on `.hermes/pipeline.toml`, so write the contract in each:

```bash
mkdir -p ~/my-projects
tpo config set projects_dir ~/my-projects

cd ~/my-projects
git clone https://github.com/<you>/demo-app.git
git clone https://github.com/<you>/second-app.git

tpo init demo-app
tpo init second-app
```

Then give each repository one selectable TODO: file an issue through the
"TPO TODO" form (`gh issue create --web --template "TPO TODO"` from inside the
clone) and label it `ready-for-agent`. The issue number becomes the TODO ID
(`TODO-<issue-number>`).

A directory that has `.hermes/` or a legacy `TODOS.md` but no
`.hermes/pipeline.toml` is skipped with a WARNING suggesting `tpo init`.

You now have two projects that tpo can discover.

---

## Step 2: Run the scan loop

Run a single tick without specifying a project:

```bash
uv run tpo tick
```

You'll see output like:

```
discovered 2 active projects
project demo-app: selection result: picked=TODO-12
project demo-app: registered 4 kanban tasks for TODO-12
project second-app: selection result: picked=TODO-3
project second-app: registered 4 kanban tasks for TODO-3
```

One tick, two projects, and a lock scoped to each project. No cron per project
needed.

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
back to the global `slack_channel` config value or `#alert`.

Run another tick to verify:

```bash
uv run tpo tick
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
uv run tpo tick
```

You should see:

```
discovered 1 active projects
project demo-app: selection result: picked=...
```

Second-app was skipped — no need to delete its contract or close its issues.
To re-enable it, set `enabled = true` or delete the `project.toml` file.

---

## Step 5: Check per-project state

Each project now has its own state directory:

```bash
ls ~/my-projects/demo-app/.hermes/
# pipeline.toml  current_tick_id.txt  circuit.json  decisions/  outcomes/  runs/  phase_started/

ls ~/my-projects/second-app/.hermes/
# pipeline.toml  project.toml
```

Notice: demo-app has tick state because it ran selection. Second-app is archived,
so it has no tick state — only the contract and `project.toml`.

---

## What you built

You now have a multi-project setup that:

- Discovers active projects automatically via `_discover_projects`
- Runs one selection per project under a per-project lock
- Uses per-project Slack channels for notifications
- Archives projects without deleting their contract
- Shares one cron entry (`hermes cron set pipeline-tick */5 * * * *`)

### Next steps

**Set up production cron:**

```bash
hermes cron set pipeline-tick '*/5 * * * *'
```

See [Pipeline state machine](hermes-state-machine.md) for the tick lifecycle.

**Understand the scan loop architecture:**

- Read [How the multi-project scan loop works](explanation-multi-project-scan.md)
  for how per-project locks isolate overlapping ticks.
- Read [Issue tracker conventions](agents/issue-tracker.md) for the label
  vocabulary that makes an issue eligible for selection.

**Deep-dive:**

- [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) — how phases map to kanban tasks
- [Configure `.hermes/config.toml`](howto-config-toml.md) — tuning selection model and circuit breaker
