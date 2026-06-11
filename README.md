# todo-pipeline-orchestrator

Pipeline watcher and TODOS manager orchestration toolkit, packaged as a uv-managed Python project.

## Status

Fully modularized `hermes-pipeline` package with CLI, watcher, status, and merge orchestration.

See [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md) for the modularization plan and architectural design.

## Features

- **Auto-tick discovery**: Scan all projects for TODOS.md changes and automatically select eligible TODOs
- **CLI subcommands**: `auto`, `merge`, `status` for pipeline management
- **Pending records table**: Display ready-for-review records with status and age
- **Phase 9 merge orchestration**: Confirm, version bump, and git merge to main
- **Cron registration**: 5-minute automated tick via `install-cron.sh`

## Requirements

- Python 3.9+
- [uv](https://docs.astral.sh/uv/install/) package manager

Install uv:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Getting Started

👉 **New to pipeline-watch?** Start with the [getting-started tutorial](docs/tutorial-getting-started.md) — walk through discovery, review, and merge in ~15 minutes.

### Installation

```bash
uv sync
```

## Run

### CLI Commands

Display pipeline status:
```bash
uv run pipeline-watch status
```

Run one auto-tick (discover projects, detect changes, select eligible TODOs):
```bash
uv run pipeline-watch auto
```

Merge a ready TODO to main:
```bash
uv run pipeline-watch merge <project> <todo_id>
# Or with --abandon flag to skip confirmation
uv run pipeline-watch merge <project> <todo_id> --abandon
```

### Automated Ticks

Register a 5-minute cron job:
```bash
bash hermes-pipeline/scripts/install-cron.sh
```

This will register a crontab entry to run `pipeline-watch auto` every 5 minutes and log output to `~/.hermes/cron.log`.

## Configuration

Set these environment variables to customize behavior:

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPELINE_LOCK_DIR` | `~/.hermes/locks` | Directory for merge operation locks |
| `PIPELINE_PROJECTS_DIR` | (required) | Path to scan for `TODOS.md` files |
| `PIPELINE_CLAUDE_CMD` | `claude` | Command to invoke Claude Code |
| `PIPELINE_KANBAN_ADAPTER` | `null` | Kanban adapter: `hermes` or `null` |

Example:
```bash
export PIPELINE_PROJECTS_DIR=~/my-projects
export PIPELINE_LOCK_DIR=~/.hermes/locks
uv run pipeline-watch auto
```

## Troubleshooting

**"command not found: uv"**
- Uv is not installed or not in PATH
- **Fix:** Run the installation command from [Requirements](#requirements)

**"No pending records"**
- No TODOs are ready for review yet
- Check `PIPELINE_PROJECTS_DIR` is set and contains `TODOS.md` files
- Run `uv run pipeline-watch auto` to trigger discovery

**"error: argument todo_id: invalid int value"**
- `todo_id` must be a number, e.g., `123` (not `ABC` or `some-id`)
- **Fix:** Run `uv run pipeline-watch merge --help` to see usage

**Merge operation hangs**
- Check if another merge is already in progress (lock file in `PIPELINE_LOCK_DIR`)
- Verify git repository is accessible and has write permissions

## Architecture

The package is organized into lanes:

- **Lane A**: Selection (TODOS.md parsing, cycle detection, eligibility)
- **Lane B**: State management (locks, checkpoints, ready-for-review records)
- **Lane C**: Kanban integration (active tasks, outbox, sync)
- **Lane D**: Runner and phases
- **Lane E**: Merge orchestration (Phase 9)
- **Lane F**: CLI, watcher, status, and installation (this lane)

See `docs/gstack/hermes-pipeline/design-plan.md` for the full design specification.

## Contributing

Found a bug? Have a feature request? [Open an issue on GitHub](https://github.com/hyonchoi/todo-pipeline-orchestrator/issues).

## License

See LICENSE for details.
