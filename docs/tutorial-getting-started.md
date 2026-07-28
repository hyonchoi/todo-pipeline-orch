# Getting Started with tpo

In this tutorial, you'll set up your first pipeline-watched project and run the core workflows: triggering a tick, reviewing TODOs, and approving one to ship.

**Time: ~10 minutes**

## What you'll need

- Python 3.12+
- `uv`
- `tpo` installed as a uv tool, or a source checkout with `uv sync`
- The Hermes CLI on `PATH`, authenticated with `hermes login`, with an agent runtime/profile available when you run pipeline phases
- A Hermes kanban board configured for your project
- Write permissions on the git repositories you want `tpo` to scan

Provider authentication depends on the model/runtime configured for your Hermes profile. It is not required for installing `tpo`, but it is required before `tpo tick` can run pipeline phases.

If you don't have a test project yet, the setup section below will guide you through creating one.

## Step 1: Install and verify `tpo`

If `uv` is missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install the CLI from a source checkout:

```bash
git clone https://github.com/hyonchoi/todo-pipeline-orch.git
cd todo-pipeline-orch
uv tool install .
tpo --version
```

For contributor work in that checkout:

```bash
uv sync
uv run tpo --version
```

Use direct `tpo ...` commands for the rest of this tutorial.

## Step 2: Install the pipeline profile and TODO skill

Install the pipeline profile when you want `tpo` to register unattended phase tasks:

```bash
tpo install-profile
```

Install `todos-manager` for the agent that will edit TODOs:

```bash
tpo skills install --target all
```

`codex` installs to the `.agents/skills` convention, `claude` installs to the `.claude/skills` convention, and `all` installs both.

## Step 3: Create or adopt a project

Create a demo project:

```bash
mkdir -p ~/my-projects/demo-app
cd ~/my-projects/demo-app
git init
```

For a new project with no `TODOS.md`, ask your agent to invoke the installed `todos-manager` skill with `--init`, then `--add`:

```text
todos-manager --init
todos-manager --add
```

Commit the generated TODO files and first entry:

```bash
git add TODOS.md TODOS-archive.md
git commit -m "init: create canonical TODOs"
```

For an existing project with a hand-written `TODOS.md`, invoke the `todos-manager` skill with `--convert`, then `--audit`:

```text
todos-manager --convert
todos-manager --audit
```

Invoke `todos-manager --revise` when a specific entry needs stronger required or optional fields.

For project-local Codex/agents setup, install the skill from the project root after the project exists:

```bash
tpo skills install --scope project --target codex
```

## Step 4: Configure project discovery

Tell `tpo` where to find projects:

```bash
tpo config init
tpo config set projects_dir ~/my-projects
tpo config get projects_dir
```

## Step 5: Write and verify the pipeline contract

`tpo init <project>` writes `.hermes/pipeline.toml`. It does not create `TODOS.md`.

```bash
tpo init demo-app
tpo doctor demo-app
```

Expected doctor output starts with:

```text
OK: schema_version=1
```

## Step 6: Run a manual tick

Before the first tick, mark the TODO you want the pipeline to select as in progress by changing its status marker to `[→]` in `TODOS.md`.

```bash
tpo tick demo-app
```

The tick checks project state, selects eligible TODOs, and registers phase tasks. If nothing is ready, it reports that no TODO was picked.

To scan every active project under `projects_dir`:

```bash
tpo tick
```

See [Run a manual tick](howto-pipeline-tick.md) for detailed tick behavior and recovery.

## Step 7: Inspect phase progress

```bash
hermes kanban list --tenant demo-app
```

See [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) for how phase tasks are chained.

## Step 8: Inspect PR handoff

The default `gstack` profile finishes at Phase 8. That phase runs `/ship`, pushes all intended branch changes, and opens or updates a PR without merging it. Inspect the PR in GitHub or from the project worktree:

```bash
gh pr status
```

No automatic merge is performed by `tpo tick`.

## Step 9: Automate ticks later

Manual ticks are enough for first setup. For scheduled operation, see [Run a manual tick](howto-pipeline-tick.md) and the Hermes cron guidance in the operations docs.

## What you built

You now have:

- `tpo` installed and verified
- `todos-manager` installed for your agent target
- a canonical `TODOS.md`
- a configured `projects_dir`
- a pipeline contract in `.hermes/pipeline.toml`
- a manual tick path

## Next steps

- [CLI reference](reference-cli.md)
- [How to manage TODOS.md with todos-manager](howto-todos-manager.md)
- [How to run a manual tick](howto-pipeline-tick.md)
- [Pipeline contract setup](howto-pipeline-contract.md)
- [Architecture overview](ARCHITECTURE.md)
