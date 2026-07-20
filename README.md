# todo-pipeline-orchestrator

Pipeline watcher and TODOS manager orchestration toolkit, packaged as a uv-managed Python project.

## Status

Fully modularized `hermes-pipeline` package with CLI, watcher, status, and merge orchestration.

See [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md) for the modularization plan and architectural design.

## Features

- **Hermes-agent selection** (v0.2): LLM-driven TODO selection via Hermes CLI (`hermes chat -q`) with SHA-pinned prompt, immutable decision records, and outcome sidecars
- **CLI subcommands**: `tick`, `merge`, `status`, `kill`, `recover-counter`, `approve-plan` for pipeline management
- **Multi-project scan loop**: `tick` and `kill` without a project argument scan all active projects in one execution
- **Logging flags**: `--verbose` and `--debug` global flags for detailed diagnostics (selection results, lock state, agent call summaries, circuit breaker transitions)
- **Pending records table**: Display ready-for-review records with status and age
- **Plan Gate (phase_2b)** — Human review checkpoint between Autoplan and Writing Plan. Blocks the pipeline until a human approves or rejects the plan via `pipeline-watch approve-plan`. Includes risk classifier, decision sheet schema, and override sanitization.
- **Phase 5 code review (v0.4)**: New `phase_5_review` phase runs gstack `/review` skill autonomously via `hermes chat -q`, with pre-review snapshot, post-review pytest run, deterministic commit-on-pass or restore-on-fail, and machine-verified outcomes (`review_clean`, `review_reverted_test_failure`, `review_timeout`, `review_skipped_no_diff`)
- **Circuit breaker**: no-progress counter and Slack alert dedup to stop runaway ticks (the gateway service manages tick scheduling and cron backoff)
- **Hermes cron integration**: pipeline-tick schedule managed via `hermes cron set`
- **TODOS Manager skill (v2.1)**: Seven subcommands (`--init`, `--add`, `--convert`, `--audit`, `--archive`, `--list`, `--revise`) for managing TODOS.md entries with schema enforcement, auto-research field pre-fills, stable TODO-<n> IDs, archiving to TODOS-archive.md, and AI-assisted revision of existing entries. Install via `scripts/install-todos-manager.sh`.
- **Skill test environment (Phase 1)**: `tests/skill-test-environment/` — structural unit tests for the `todos-manager` skill's deterministic logic (ID sequencing, entry parsing, format validation, archive logic), backed by golden YAML assertions and a demo-project fixture. Zero token cost, runs in under 5 seconds.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/install/) package manager
- **Hermes CLI** (v0.3+): selection and phase execution route through `hermes chat -q`. Install Hermes and run `hermes login` before running the pipeline.

Install uv:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Documentation

| Doc | Quadrant | When to read |
|---|---|---|
| [Getting-started tutorial](docs/tutorial-getting-started.md) | Tutorial | First time using `pipeline-watch` end-to-end |
| [Architecture overview](docs/ARCHITECTURE.md) | Explanation | Understanding lane structure, data flow, phase execution |
| [Pipeline state machine](docs/hermes-state-machine.md) | Explanation | Understanding `.hermes/` file layout and transitions |
| [Approve or reject a plan gate](docs/howto-approve-plan-gate.md) | How-to | Responding to plan-gate alerts, reviewing decisions, overriding recommendations |
| [Selection seat contract](hermes_pipeline/decision/README.md) | Reference | Integrating with the Hermes config repo |
| [Modularization plan](docs/pipeline-modularization-plan.md) | Explanation | Architecture and design history |
| [Kanban-as-Scheduler](docs/reference-kanban-as-scheduler.md) | Reference/Explanation | How `pipeline-watch tick` uses kanban for phase state and ordering |
| [Run a manual tick](docs/howto-pipeline-tick.md) | How-to | Running `pipeline-watch tick` for iterative development |
| [Run the eval suite](docs/howto-eval-suite.md) | How-to | Before changing the prompt, model, or `decision/agent.py` |
| [Recover from a prompt SHA mismatch](docs/howto-prompt-sha-mismatch.md) | How-to | Selection aborted with `prompt_sha_mismatch:` rationale |
| [Configure `.hermes/config.toml`](docs/howto-config-toml.md) | How-to | Tuning selection model or circuit-breaker thresholds |
| [Kill a stuck in-flight phase](docs/howto-kill-stuck-phase.md) | How-to | A phase is wedged past `max_phase_timeout_min` |
| [Set up multiple projects](docs/howto-multi-project-setup.md) | How-to | Configuring per-project settings and the scan loop |
| [Troubleshoot state migration](docs/howto-troubleshoot-state-migration.md) | How-to | Migration failed or skipped with multiple projects |
| [Multi-project scan tutorial](docs/tutorial-multi-project-scan.md) | Tutorial | Setting up two projects and running the scan loop |
| [How the scan loop works](docs/explanation-multi-project-scan.md) | Explanation | Why single global lock, state migration decisions, trade-offs |
| [Set up the pipeline profile](docs/howto-pipeline-profile.md) | How-to | Installing the dedicated pipeline Hermes profile for unattended execution |
| [Configure the pipeline contract](docs/howto-pipeline-contract.md) | How-to | Editing assignee, fixing capability drift, schema migration |
| [Use the agent-skills profile](docs/howto-agent-skills-profile.md) | How-to | Selecting a different skill-set for pipeline phases (`gstack` or `agent-skills`) |
| [Why the pipeline contract](docs/explanation-pipeline-contract.md) | Explanation | Design rationale: versioned contracts, drift detection, capability gates |
| [Use the Hermes adapter](docs/howto-hermes-adapter.md) | How-to | How `hermes chat -q` replaces Anthropic SDK calls |
| [Debug ticks and recover counters](docs/howto-debugging-and-recovery.md) | How-to | Using `--verbose`, `--debug`, and `recover-counter` |
| [Counter recovery](docs/reference-counter.md) | Reference/Explanation | How `recover_counter()` works and design rationale |
| [TODOS Manager skill](skills/todos-manager/SKILL.md) | Reference | TODOS.md schema, ID assignment, and 7 subcommands |
| [Getting started with todos-manager](docs/tutorial-todos-manager.md) | Tutorial | Step-by-step: init, add, revise, archive a completed TODO |
| [Manage TODOS.md with todos-manager](docs/howto-todos-manager.md) | How-to | Using --init, --add, --convert, --audit, --archive, --list, --revise |
| [Install TODOS Manager](scripts/install-todos-manager.sh) | How-to | Symlink skill to user-level skill directories |
| [Skill test environment](tests/skill-test-environment/README.md) | How-to | Running structural unit tests for the todos-manager skill |

| [Approve and ship a TODO](docs/howto-approve-and-ship.md) | How-to | Running `pipeline-watch approve` — full ship workflow |
| [CLI reference](docs/reference-cli.md) | Reference | All subcommands, arguments, exit codes, environment variables |
| [Circuit breaker](docs/explanation-circuit-breaker.md) | Explanation | How no-progress tracking works and why it alerts |
| [Decision module API](docs/reference-decision-api.md) | Reference | Selection schemas, outcome sidecars, plan-gate types |
| [Handle phase 5 review outcomes](docs/howto-review-outcomes.md) | How-to | Inspecting review artifacts, handling reverted or timed-out reviews |

## TODOS Manager Skill (v2)

The `todos-manager` skill provides schema-enforced TODOS.md management with seven subcommands:

| Subcommand | Purpose |
|---|---|
| `--init` | Initialize TODOS.md with preamble and create TODOS-archive.md |
| `--add` | Add new entry with schema enforcement and preview gate |
| `--convert` | Add preamble to existing TODOS.md and validate format |
| `--audit` | Audit TODOS.md for format compliance (no auto-fix) |
| `--archive` | Move all `[x]` completed entries to TODOS-archive.md |
| `--list` | List active TODO entries as a table (`--all` also shows archived) |
| `--revise` | Revise an existing entry — fill missing or weak fields with AI-pre-filled suggestions |

Install the skill from the project source to your user-level skill directories:

```bash
bash scripts/install-todos-manager.sh
```

This creates symlinks in `~/.claude/skills/todos-manager/` and `~/.agents/skills/todos-manager/` pointing to `skills/todos-manager/SKILL.md`.

The skill enforces the canonical schema (What/Why/Decisions + optional fields), stable TODO-<n> ID assignment (scanning both TODOS.md and TODOS-archive.md), and a preview/confirm gate before writing. See [skills/todos-manager/SKILL.md](skills/todos-manager/SKILL.md) for the full schema and workflows.

---

## Getting Started

👉 **New to pipeline-watch?** Start with the [getting-started tutorial](docs/tutorial-getting-started.md) — walk through discovery, review, and merge in ~15 minutes.

### Installation

```bash
uv sync
```

## Run

### CLI Commands

Run a single pipeline tick (scans all active projects, select a TODO, register kanban phases, observe the circuit breaker):
```bash
uv run pipeline-watch tick
```

Display pipeline status:
```bash
uv run pipeline-watch status
```

Merge a ready TODO to main:
```bash
uv run pipeline-watch merge <project> <todo_id>
# Or with --abandon flag to skip confirmation
uv run pipeline-watch merge <project> <todo_id> --abandon
```

Kill in-flight phases (writes a `killed_by_operator` outcome sidecar and releases the tick lock if held by the killed tick). Without a project argument, scans all active projects:
```bash
# Kill a specific TODO across all projects
uv run pipeline-watch kill --todo TODO-N
# Kill every in-flight phase across all projects
uv run pipeline-watch kill --all
```

Approve or reject a plan-gate decision sheet (unblocks the pipeline at the plan gate):
```bash
# Approve the plan for a TODO
uv run pipeline-watch approve-plan <project> --todo TODO-N --approve
# Reject with a reason
uv run pipeline-watch approve-plan <project> --todo TODO-N --reject --reason "..."
# Override individual decisions without re-running Autoplan
uv run pipeline-watch approve-plan <project> --todo TODO-N --approve --override q_id=LABEL
```

Recover the TODO ID counter by scanning TODOS.md for the highest TODO-N (useful when bootstrapping a project with hand-written TODOs but no counter file):
```bash
uv run pipeline-watch recover-counter <project>
```

Write the default pipeline execution contract for a project (idempotent — run again with `--force` to regenerate after editing `phases.yaml`). Use `--assignee` to set the Hermes profile for kanban tasks, and `--profile` to choose a pipeline skill-set (default: `gstack`):
```bash
uv run pipeline-watch init <project>
uv run pipeline-watch init <project> --force
uv run pipeline-watch init <project> --assignee pipeline
uv run pipeline-watch init <project> --profile agent-skills
```

Install the bundled pipeline Hermes profile for unattended kanban execution:
```bash
uv run pipeline-watch install-profile
uv run pipeline-watch install-profile --force  # reinstall after SOUL.md changes
```

Verify a project's pipeline execution contract against its configured profile's phases (exit 0 clean, 1 drift, 2 missing/invalid contract or profile):
```bash
uv run pipeline-watch doctor <project>
```

Global flags available on all subcommands:
```bash
uv run pipeline-watch --verbose tick   # increased log detail (selection results, lock state)
uv run pipeline-watch --debug tick     # full debug logging (agent call summaries, circuit breaker transitions)
```

### Automated Ticks

The pipeline is driven by Hermes cron, not system crontab. The Hermes CLI
manages the tick schedule; the gateway service adjusts the interval
automatically (normal 5-minute ticks, backoff after repeated
no-progress ticks):

```bash
hermes cron set pipeline-tick '*/5 * * * *'
```

See [docs/hermes-state-machine.md](docs/hermes-state-machine.md) for the
tick lifecycle.

## Configuration

Set these environment variables to customize behavior:

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPELINE_LOCK_DIR` | `~/.hermes/pipeline_locks` | Directory for merge operation locks |
| `PIPELINE_PROJECTS_DIR` | `~/projects` | Path to scan for `TODOS.md` files |
| `PIPELINE_STATE_DIR` | `~/.hermes` | Global state directory (tick lock, config) |
| `PIPELINE_SLACK_CHANNEL` | `#alert` | Default Slack channel for alerts (overridden by per-project config) |
| `PIPELINE_CLAUDE_CMD` | `claude` | Command to invoke Claude Code (deprecated in v0.3 — phases now use `hermes chat -q` instead) |
| `PIPELINE_KANBAN_ADAPTER` | `null` | Kanban adapter: `hermes` or `null` |

Example:
```bash
export PIPELINE_PROJECTS_DIR=~/my-projects
export PIPELINE_LOCK_DIR=~/.hermes/pipeline_locks
hermes login  # authenticate with your provider
hermes cron set pipeline-tick '*/5 * * * *'  # start the tick loop
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
no_progress_threshold = 3           # consecutive picked=None ticks before Slack alert
alert_dedup_hours = 24
max_phase_timeout_min = 120
max_tick_duration_min = 10
```

See [docs/hermes-state-machine.md](docs/hermes-state-machine.md) for the
state transitions these settings gate, and the docstrings in
`hermes_pipeline/config.py` for the authoritative field
list.

### Pipeline execution contract (`.hermes/pipeline.toml`)

Each project declares the assignee and tool capabilities its phases require in
a versioned contract at `.hermes/pipeline.toml`. Run `pipeline-watch init
<project>` once to write the default:

```toml
schema_version = 1
assignee = "default"
capabilities = ["Bash", "Edit", "Read", "Write"]
```

- `schema_version` — bumped whenever the contract's field set changes. A tick
  against a stale version fails closed with a remediation message instead of
  silently running with mismatched settings.
- `assignee` — passed as `--assignee` when registering each phase's kanban task.
- `capabilities` — the tool set phases are allowed to use. `pipeline-watch
  doctor <project>` cross-checks this against the `tools` each phase in
  `phases.yaml` declares and reports drift.

Projects that have never run `init` tick with the defaults above — the
contract is additive, not a migration requirement. A project's tick only
blocks when a contract *exists* but is stale or under-declares capabilities.

## Troubleshooting

**"command not found: uv"**
- Uv is not installed or not in PATH
- **Fix:** Run the installation command from [Requirements](#requirements)

**"No pending records"**
- No TODOs are ready for review yet
- Check `PIPELINE_PROJECTS_DIR` is set and contains `TODOS.md` files
- Ensure the Hermes cron tick is running: `hermes cron list`

**"error: argument todo_id: invalid int value"**
- `todo_id` must be a number, e.g., `123` (not `ABC` or `some-id`)
- **Fix:** Run `uv run pipeline-watch merge --help` to see usage

**Merge operation hangs**
- Check if another merge is already in progress (lock file in `PIPELINE_LOCK_DIR`)
- Verify git repository is accessible and has write permissions

## Architecture

The package is organized into lanes:

- **Lane A**: Hermes-agent selection (`decision/` — LLM-driven TODO pick via `hermes chat -q`, SHA-pinned prompt, immutable decision records + outcome sidecars). The deterministic `selection.py` was retired in v0.2.
- **Lane B**: State management (locks, checkpoints, ready-for-review records, atomic tmp+rename writes)
- **Lane C**: Kanban integration (kanban-as-scheduler — phases as kanban tasks with `--parent` dependency chains; see [reference-kanban-as-scheduler.md](docs/reference-kanban-as-scheduler.md))
- **Lane D**: Runner and phases (`phases.py`, `tick.py` atomic-mkdir tick lock)
- **Lane D.5**: Code review phase (`review_phase.py` — pre-review snapshot, hermes `/review` subprocess, post-review pytest + deterministic commit/restore, machine-verified outcomes)
- **Lane D.6**: Plan gate (`gates.py`, `approve_plan.py`, `decision/schema.py` — decision sheet I/O, risk classifier, gate status, approve/reject logic)
- **Lane E**: Merge orchestration (Phase 9)
- **Lane F**: CLI, watcher, status, and installation (this lane; includes `project_config.py` for multi-project scanning and `state_migration.py` for per-project state)
- **Lane G**: Hermes adapter (`hermes_adapter.py` — wraps `hermes chat -q` for all LLM calls, replaces direct Anthropic SDK usage)

State transitions and the file layout under `.hermes/` (decisions, outcomes, phase_started, tick.lock, ready_for_review) are documented in [docs/hermes-state-machine.md](docs/hermes-state-machine.md). The selection seat contract lives in [hermes_pipeline/decision/README.md](hermes_pipeline/decision/README.md). See `docs/gstack/hermes-pipeline/design-plan.md` for the full design specification.

## Contributing

Found a bug? Have a feature request? [Open an issue on GitHub](https://github.com/hyonchoi/todo-pipeline-orchestrator/issues).

## License

See LICENSE for details.
