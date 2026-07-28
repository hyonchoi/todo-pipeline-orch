# Getting Started with tpo

In this tutorial, you'll set up your first pipeline-watched project and run the core workflows: triggering a tick, reviewing TODOs, and approving one to ship.

**Time: ~10 minutes**

## What you'll need

- Python 3.12+
- `uv`
- `tpo` installed as a uv tool, or a source checkout with `uv sync`
- A Hermes agent runtime/profile available when you run pipeline phases
- Write permissions on the git repositories you want `tpo` to scan

Provider authentication depends on the model/runtime configured for your Hermes profile. It is not required for every `tpo` installation path.

If you don't have a test project yet, the setup section below will guide you through creating one.

## Step 1: Install and verify `tpo`

If `uv` is missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install the CLI:

```bash
uv tool install .
tpo --version
```

From a source checkout:

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

For project-local Codex/agents setup, run this from the project root instead:

```bash
tpo skills install --scope project --target codex
```

`codex` installs to the `.agents/skills` convention, `claude` installs to the `.claude/skills` convention, and `all` installs both.

## Step 3: Create or adopt a project

Create a demo project:

```bash
mkdir -p ~/my-projects/demo-app
cd ~/my-projects/demo-app
git init
```

For a new project with no `TODOS.md`, initialize the canonical TODO files:

```bash
todos-manager --init
todos-manager --add
```

Commit the generated TODO files and first entry:

```bash
git add TODOS.md TODOS-archive.md
git commit -m "init: create canonical TODOs"
```

For an existing project with a hand-written `TODOS.md`, use this path instead:

```bash
todos-manager --convert
todos-manager --audit
```

Use `todos-manager --revise` when a specific entry needs stronger required or optional fields.

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

## Step 8: Approve and ship

When all phases are complete and the TODO is ready to ship:

```bash
tpo approve demo-app --todo TODO-1
```

See [Approve and ship a TODO](howto-approve-and-ship.md) for guards, exit codes, and recovery.

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
