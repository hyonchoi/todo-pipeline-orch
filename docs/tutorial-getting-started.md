# Getting Started with tpo

In this tutorial, you'll set up your first pipeline-watched project and run the core workflows: triggering a tick, reviewing TODOs, and approving one to ship.

**Time: ~10 minutes**

## What you'll need

- Python 3.12+
- `uv`
- `tpo` installed as a uv tool, or a source checkout with `uv sync`
- The Hermes CLI on `PATH`, authenticated with `hermes login`, with an agent runtime/profile available when you run pipeline phases
- A Hermes kanban board configured for your project
- Write permissions on the git repositories you want `tpo` to scan, each hosted on github.com
- `gh` >= 2.44 on `PATH`, authenticated with `gh auth login`

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

## Step 2: Install the pipeline profile

Install the pipeline profile when you want `tpo` to register unattended phase tasks:

```bash
tpo install-profile
```

## Step 3: Create or adopt a project

`tpo` selects work from GitHub Issues, so the project must be a clone of a
github.com repository (see [ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md)):

```bash
mkdir -p ~/my-projects
cd ~/my-projects
git clone https://github.com/<you>/demo-app.git
cd demo-app
```

The label vocabulary is created in Step 5, after the project is registered
under `projects_dir`. See [Manage TODOs as GitHub Issues](howto-github-issues-todos.md)
for the full backlog workflow.

## Step 4: Configure project discovery

Tell `tpo` where to find projects:

```bash
tpo config init
tpo config set projects_dir ~/my-projects
tpo config get projects_dir
tpo config get prompt_client
```

`prompt_client` defaults to `claude`. Keep it for Claude Code prompts such as
`/review` and `/ship`, or select Codex vocabulary (`$review` and `$ship`) for
every project under this `projects_dir`:

```bash
tpo config set prompt_client codex
```

The setting changes rendered task instructions only. It does not select a
Hermes assignee or install the profile's external skills.

## Step 5: Write and verify the pipeline contract

`tpo init <project>` writes `.hermes/pipeline.toml`. Discovery keys on this
file: a directory under `projects_dir` without it is skipped with a WARNING
suggesting `tpo init`.

```bash
tpo init demo-app
tpo todos labels sync demo-app
tpo doctor demo-app
```

`tpo todos labels sync` creates the pipeline label vocabulary once per
repository; `doctor` reports any label still missing.

For the supported `gstack` profile, doctor first prints the selected prompt
client and prerequisite diagnostics. Successful output ends with:

```text
OK: schema_version=2
```

## Step 6: File and triage a TODO

The "TPO TODO" form is repository-local: copy `.github/ISSUE_TEMPLATE/tpo-todo.yml`
from the `todo-pipeline-orchestrator` repository into
`demo-app/.github/ISSUE_TEMPLATE/` and commit it (or use the scripted
`render_issue_body` + `--body-file` path). `tpo todos audit` reads the project's
copy for the allowed Phase options. Then file the first TODO through the form:

```bash
gh issue create --web --template "TPO TODO"
```

The form applies `tpo:todo` + `needs-triage`. The issue number is the TODO ID
(`TODO-<issue-number>`). When it is ready for the pipeline, triage it:

```bash
gh issue edit <N> --remove-label needs-triage --add-label ready-for-agent
```

Only issues carrying both `tpo:todo` and `ready-for-agent` are selectable; see
[issue tracker conventions](agents/issue-tracker.md#tpo-backlog-items) for the
full label vocabulary and body contract.

## Step 7: Run a manual tick

```bash
tpo tick demo-app
```

The tick checks project state and selects eligible TODOs. When a TODO is picked,
it renders every phase body first, persists `current_tick_id.txt` plus the
`tick_started` outcome only after all bodies are valid, and then creates the
prepared Hermes tasks. If nothing is ready, it reports that no TODO was picked.

To scan every active project under `projects_dir`:

```bash
tpo tick
```

See [Run a manual tick](howto-pipeline-tick.md) for detailed tick behavior and recovery.

## Step 8: Inspect phase progress

```bash
hermes kanban list --tenant demo-app
```

See [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) for how phase tasks are chained.

## Step 9: Inspect PR handoff

The default `gstack` profile finishes at Phase 8. That phase runs `/ship` in
Claude Code or `$ship` in Codex, pushes all intended branch changes, and opens
or updates a PR without merging it. Inspect the PR in GitHub or from the
project worktree:

```bash
gh pr status
```

No automatic merge is performed by `tpo tick`.
Later ticks leave the project idle while that PR is open, closed without merge,
or temporarily unverifiable. After the PR is merged, closeout closes the issue
and the next tick can select new TODO work.

## Step 10: Automate ticks later

Manual ticks are enough for first setup. For scheduled operation, see [Run a manual tick](howto-pipeline-tick.md) and the Hermes cron guidance in the operations docs.

## What you built

You now have:

- `tpo` installed and verified
- a GitHub repository with the `tpo:todo` label vocabulary and one triaged TODO issue
- a configured `projects_dir`
- a pipeline contract in `.hermes/pipeline.toml`
- a manual tick path

## Next steps

- [CLI reference](reference-cli.md)
- [Issue tracker conventions](agents/issue-tracker.md)
- [How to run a manual tick](howto-pipeline-tick.md)
- [Pipeline contract setup](howto-pipeline-contract.md)
- [Architecture overview](ARCHITECTURE.md)
