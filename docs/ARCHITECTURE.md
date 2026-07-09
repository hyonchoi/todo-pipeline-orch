# Architecture

todo-pipeline-orchestrator is a uv-managed Python package that automates the lifecycle of TODOS.md entries through a multi-phase pipeline driven by Hermes agents, kanban task dependencies, and circuit breaker protections.

## Overview

```
Tick Loop (Hermes cron or manual)
    |
    v
[Selection] -- Hermes agent picks a TODO from TODOS.md
    |
    v
[Kanban Registration] -- Create phases as kanban tasks with --parent chains
    |
    v
Phase 2: Autoplan --> Phase 2b: Plan Gate --> Phase 3: Writing Plan --> Phase 4: Development
    |
    v
Phase 5: Code Review (gstack /review, v0.4+)
    |
    v
Phase 6.1: CSO Security Review
    |
    v
Phase 7: Document Release --> Phase 8: Finish Branch --> Phase 9: Ship Gate
```

## Lane Structure

The package is organized into lanes — loosely coupled subsystems with well-defined interfaces.

```
hermes_pipeline/
├── cli.py                    # CLI entry point (argparse subcommands)
├── config.py                 # Configuration loading (env vars + TOML overlay)
├── circuit.py                # Circuit breaker (no-progress tracking, Slack alerts)
├── counter.py                # TODO ID counter management
├── hermes_adapter.py         # Hermes CLI wrapper (replaces direct Anthropic SDK)
├── kanban.py                 # Kanban adapter (hermes kanban commands)
├── kanban_tasks.py           # Task registration with --parent chains
├── merge.py                  # Phase 9 merge orchestration
├── outcomes.py               # Outcome sidecar writing/reading
├── phases.py                 # Phase definitions + hermes subprocess invocation
├── project_config.py         # Multi-project discovery & per-project config
├── review_phase.py           # Phase 5 code review lifecycle (v0.4+)
├── runner.py                 # Pipeline runner (phase progression)
├── gates.py                  # Plan gate primitives (decision sheet I/O, risk classifier, gate status)
├── ship.py                   # Phase 9 ship gate (approve, CI-green, squash merge)
├── approve_plan.py           # approve-plan CLI domain logic (approve/reject plan gates)
├── slack.py                  # Slack notification helpers
├── state.py                  # State management (locks, checkpoints, atomic writes)
├── state_migration.py        # Global-to-per-project state migration
├── status.py                 # Pending records table
├── tick.py                   # Tick orchestration (scan loop, lock acquisition)
├── watcher.py                # Project discovery & change detection
├── decision/                 # LLM-driven TODO selection (Lane A)
│   ├── agent.py              # Hermes agent invocation
│   ├── context.py            # Selection context builder
│   ├── schema.py             # Selection schema
│   └── store.py              # Immutable decision store
└── logging_setup.py          # Python logging configuration
```

### Lane A: Hermes-Agent Selection
`decision/` — LLM-driven TODO pick via `hermes chat -q`. SHA-pinned prompt, immutable decision records, outcome sidecars. The deterministic `selection.py` was retired in v0.2.

### Lane B: State Management
`state.py` — Locks, checkpoints, ready-for-review records, atomic tmp+rename writes. All state files are written atomically to prevent partial reads.

### Lane C: Kanban Integration
`kanban.py`, `kanban_tasks.py` — Phases as kanban tasks with `--parent` dependency chains. Kanban status queries drive the tick loop.

### Lane D: Runner & Phases
`phases.py`, `tick.py` — Phase execution via Hermes subprocess. Atomic-mkdir tick lock prevents duplicate ticks.

### Lane D.5: Code Review Phase (v0.4+)
`review_phase.py` — Code-owned lifecycle for `phase_5_review`: pre-review snapshot, hermes `/review` subprocess, post-review pytest + deterministic commit/restore, machine-verified outcomes.

### Lane E: Merge Orchestration
`merge.py`, `ship.py` — Phase 9: confirm, version bump, CI-green gate, squash merge to main.

### Lane F: CLI, Watcher, Status
`cli.py`, `watcher.py`, `status.py`, `project_config.py`, `state_migration.py` — User-facing commands, multi-project scanning, per-project state migration.

### Lane G: Hermes Adapter
`hermes_adapter.py` — Wraps `hermes chat -q` for all LLM calls. Replaces direct Anthropic SDK usage.

## Phase Execution Flow

```
invoke _invoke_hermes(phase, todo_id, ...)
    |
    +-- phase_key == "phase_5_review"?
    |       |
    |       +-- Yes --> _invoke_review_phase()
    |       |   capture_pre_review_state() -- snapshot HEAD, save diff
    |       |       |
    |       |       +-- diff_is_empty? --> skip hermes, write artifacts, commit
    |       |       |
    |       |       +-- _run_hermes_subprocess() -- gstack /review skill
    |       |       |
    |       |       +-- finalize_review()
    |       |           run_pytest()
    |       |               |
    |       |               +-- pass --> commit_all() --> review_clean
    |       |               |
    |       |               +-- fail --> restore_worktree() --> review_reverted_test_failure
    |       |               |
    |       |               +-- timeout --> restore_worktree() --> review_timeout
    |       |
    |       +-- No --> generic _invoke_hermes() -- render prompt, hermes chat, observe
```

## Data Flow

### State Files
All pipeline state lives under `<project>/.hermes/`:

```
<project>/.hermes/
├── decisions/                 # Immutable selection decisions (write-once)
├── outcomes/                  # Phase completion/failure sidecars
├── ready_for_review/          # TODOs ready for human review
├── phase_started/             # In-flight phase markers
├── tick.lock/                 # Global tick lock (atomic mkdir)
├── todo_id_counter            # Monotonic TODO ID counter
├── config.toml                # Per-project config overlay
├── project.toml               # Project marker (enabled/slack_channel)
└── circuit.json               # Circuit breaker state
```

### Decision Immutability
`.hermes/decisions/<tick_id>.json` is written exactly once. Outcomes attach via sidecars; the decision file is never edited.

### Outcome Types
| Status | Outcome Written |
|--------|----------------|
| `done` | `phase_complete` |
| `failed` | `failed_at_phase_<key>` |
| `archived` | `failed_at_phase_<key>` with `kanban_status: "archived"` |
| phase_5_review pass | `review_clean` |
| phase_5_review fail | `review_reverted_test_failure` |
| phase_5_review timeout | `review_timeout` |
| phase_5_review no-diff | `review_skipped_no_diff` |

## Circuit Breaker

- Tracks consecutive no-progress ticks (selection returns `picked=None`)
- After threshold (default: 3), fires Slack alert
- Alert dedup: one alert per `alert_dedup_hours` (default: 24)
- Gateway service manages tick scheduling and cron backoff

## Key Design Decisions

1. **Kanban as scheduler** — Phases are kanban tasks with `--parent` chains. The orchestrator doesn't manage phase ordering.
2. **Atomic state writes** — All state files use tmp+rename to prevent partial reads.
3. **Code-owned review lifecycle** — Phase 5 (v0.4+) owns PRE/POST logic in code, not prompt. The prompt only instructs `/review`.
4. **Hermes as sole LLM surface** — All LLM traffic routes through `hermes chat -q`, not direct SDK calls.
5. **Multi-project scan** — Single global lock, per-project selection under one tick execution.

## See Also
- [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) — How kanban drives phase state
- [Pipeline State Machine](hermes-state-machine.md) — Full tick lifecycle transitions
- [Modularization Plan](pipeline-modularization-plan.md) — Design history and rationale
- [Multi-Project Scan](explanation-multi-project-scan.md) — Why single global lock, state migration decisions