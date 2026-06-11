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

## Setup

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

## Architecture

The package is organized into lanes:

- **Lane A**: Selection (TODOS.md parsing, cycle detection, eligibility)
- **Lane B**: State management (locks, checkpoints, ready-for-review records)
- **Lane C**: Kanban integration (active tasks, outbox, sync)
- **Lane D**: Runner and phases
- **Lane E**: Merge orchestration (Phase 9)
- **Lane F**: CLI, watcher, status, and installation (this lane)

See `docs/gstack/hermes-pipeline/design-plan.md` for the full design specification.
