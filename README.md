# todo-pipeline-orchestrator

Pipeline watcher and TODOS manager orchestration toolkit, packaged as a uv-managed Python project.

## Status

Fully modularized `hermes-pipeline` package with CLI, watcher, status, and merge orchestration.

See [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md) for the modularization plan and architectural design.

## Features

- **Auto-tick discovery**: Scan all projects for TODOS.md changes and automatically select eligible TODOs
- **Hermes-agent selection** (v0.2): LLM-driven TODO selection via Anthropic API with SHA-pinned prompt, immutable decision records, and outcome sidecars
- **CLI subcommands**: `auto`, `merge`, `status`, `kill` for pipeline management
- **Pending records table**: Display ready-for-review records with status and age
- **Phase 9 merge orchestration**: Confirm, version bump, and git merge to main
- **Circuit breaker**: no-progress counter, cron backoff, and Slack alert dedup to stop runaway ticks
- **Cron registration**: 5-minute automated tick via `install-cron.sh`

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/install/) package manager
- **Anthropic API key** (v0.2+): selection is now LLM-driven and calls the Anthropic API on every tick. Export `ANTHROPIC_API_KEY` before running `pipeline-watch auto`.

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

Kill an in-flight phase (writes a `killed_by_operator` outcome sidecar and releases the tick lock if held by the killed tick):
```bash
uv run pipeline-watch kill --todo TODO-N
# Or kill every in-flight phase
uv run pipeline-watch kill --all
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
export ANTHROPIC_API_KEY=sk-ant-...
uv run pipeline-watch auto
```

### TOML overlay (`.hermes/config.toml`)

Selection model and circuit-breaker thresholds are tunable via an optional TOML
overlay at `.hermes/config.toml`. Unset keys fall back to defaults in
`hermes_pipeline.config`.

```toml
[selection]
model = "claude-opus-4-7"          # pinned model id
max_tokens = 4000
auto_execute = false                # false = shadow mode (decide but don't run)
prompt_path = ".hermes/prompts/selection.md"
expected_prompt_sha = "abc123..."  # if set, mismatch aborts the tick + alerts

[circuit_breaker]
no_progress_threshold = 3           # consecutive picked=None ticks before backoff
backoff_interval_min = 30
alert_dedup_hours = 24
max_phase_timeout_min = 120
max_tick_duration_min = 10
```

See [docs/hermes-state-machine.md](docs/hermes-state-machine.md) for the
state transitions these settings gate, and the docstrings in
`hermes-pipeline/src/hermes_pipeline/config.py` for the authoritative field
list.

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

- **Lane A**: Hermes-agent selection (`decision/` — LLM-driven TODO pick via Anthropic API, SHA-pinned prompt, immutable decision records + outcome sidecars). The deterministic `selection.py` was retired in v0.2.
- **Lane B**: State management (locks, checkpoints, ready-for-review records, atomic tmp+rename writes)
- **Lane C**: Kanban integration (active tasks, outbox, sync)
- **Lane D**: Runner and phases (`phases.py`, `tick.py` atomic-mkdir tick lock)
- **Lane E**: Merge orchestration (Phase 9)
- **Lane F**: CLI, watcher, status, and installation (this lane)

State transitions and the file layout under `.hermes/` (decisions, outcomes, phase_started, tick.lock, ready_for_review) are documented in [docs/hermes-state-machine.md](docs/hermes-state-machine.md). The selection seat contract lives in [hermes-pipeline/src/hermes_pipeline/decision/README.md](hermes-pipeline/src/hermes_pipeline/decision/README.md). See `docs/gstack/hermes-pipeline/design-plan.md` for the full design specification.

## Contributing

Found a bug? Have a feature request? [Open an issue on GitHub](https://github.com/hyonchoi/todo-pipeline-orchestrator/issues).

## License

See LICENSE for details.
