# How to run a manual pipeline tick

A tick is one pass of the pipeline: select a TODO via the Hermes agent,
register phases as kanban tasks, and observe the circuit breaker. The
`pipeline-watch tick` command fires a single scan-loop tick immediately so you can
iterate without waiting for the cron schedule.

By the end of this guide, you'll have run a tick, inspected the kanban
board, and verified the outcome files.

## Prerequisites

- The pipeline is installed: `uv sync` (see [Getting Started](tutorial-getting-started.md#installation)).
- Hermes is installed and authenticated: `hermes login`.
- A Hermes kanban board is configured for your project (check with `hermes kanban list`).
- The project has a `TODOS.md` with at least one TODO in `[→]` (in progress) status.
- `.hermes/config.toml` is configured with `[selection]` and `[circuit_breaker]` sections
  — see [howto-config-toml.md](howto-config-toml.md).

## Steps

### 1. Run a tick

```bash
uv run pipeline-watch tick
```

This runs the full scan-loop tick:
```
tick:
  1. Acquire global TickLock
  2. Discover active projects (scans projects_dir)
  3. For each project:
     a. Migrate per-project state (one-time)
     b. Check prior tick (per-project)
     c. Run selection (per-project)
     d. Register kanban phases or observe circuit breaker
  4. Release lock
```

### 2. Check what the selection picked

The decision is persisted to `.hermes/decisions/<tick_id>.json`. Inspect it:

```bash
jq '{picked: .picked, rationale: .rationale}' \
  .hermes/decisions/$(ls -t .hermes/decisions/ | head -1)
```

Example output when a TODO is picked:
```json
{
  "picked": "TODO-10",
  "rationale": "TODO-10 is in progress and has no in-flight phases..."
}
```

### 3. Inspect the kanban board

If the selection picked a TODO, phases are now registered as kanban tasks:

```bash
hermes kanban list --tenant demo
```

You should see the phases with statuses:
- `running` — the first phase in the chain is executing
- `ready` — subsequent phases, blocked on `--parent` completion

The `--parent` chain means phases execute sequentially through the kanban
board. When phase 2 completes, phase 4 transitions from `ready` to `running`
automatically — the orchestrator doesn't need to manage the handoff.

### 4. Verify outcomes were written

After a tick completes (or you run another tick that detects the prior tick
is done), outcomes are written to `.hermes/outcomes/`:

```bash
cat .hermes/outcomes/$(ls -t .hermes/outcomes/ | head -1) | jq .
```

You'll see JSONL entries like:
```json
{"outcome": "phase_complete", "phase_key": "phase_2_autoplan"}
{"outcome": "phase_complete", "phase_key": "phase_4_development"}
{"outcome": "all_phases_complete"}
```

See the [outcome types table](reference-kanban-as-scheduler.md#observe_outcomes)
for all possible outcomes.

### 5. Check the circuit breaker state

```bash
cat .hermes/circuit.json | jq .
```

Key fields:
- `consecutive_no_progress` — resets to 0 when `phase_complete` is observed,
  increments on `failed_at_phase_*` outcomes.
- `backed_off` — becomes `true` when the counter hits `no_progress_threshold`
  (default: 3). Cron backoff engages at this point.

## Debugging a Single Project

The scan loop runs over all active projects. To debug a specific project's
selection, temporarily set all other projects to `enabled = false` in their
`.hermes/project.toml`, or temporarily rename their `TODOS.md`.

## State Directory

Per-project state (selection decisions, outcomes, circuit breaker) now lives
at `<project>/.hermes/`. Global state (`tick.lock`, `config.toml`) remains
in `~/.hermes/`.

## Troubleshooting

**"tick already in flight, skipping".**
A prior tick's kanban tasks are still running or ready. Check the board:
`hermes kanban list --tenant demo`. If tasks are stuck in `running`,
use [pipeline-watch kill](howto-kill-stuck-phase.md) to clear them.

**"Error: tick.lock held by pid X"**.
The tick lock is held. If the PID is alive (within `max_tick_duration_min` —
default 10 min), wait for it to complete. If the process died, the stale
marker sweep will reclaim the lock on the next tick.

**`picked=None` — no TODO selected.**
All TODOs are blocked or none are in progress. Check your `TODOS.md` for
`[→]` status. The selection rationale in `.hermes/decisions/` explains why.

**Kanban task creation fails mid-registration.**
If a task fails partway through, already-created tasks are archived. The
outcome file will show `failed_at_phase_*` with `kanban_status: "archived"`.
Check the kanban board for archived tasks and investigate the error.

**Circuit breaker trips (backed_off = true).**
Three consecutive no-progress ticks triggered the backoff. The cron interval
switched from 5 minutes to `backoff_interval_min` (default: 30 min). Check
`.hermes/decisions/` for `picked=None` decisions — the rationale explains
why each tick found nothing to do. See [howto-config-toml.md](howto-config-toml.md#loosen-the-circuit-breaker-during-onboarding)
for how to temporarily loosen the threshold.

## Related

- [Kanban-as-Scheduler reference](reference-kanban-as-scheduler.md) — full API docs
- [Pipeline State Machine](hermes-state-machine.md) — full tick lifecycle
- [How to kill a stuck phase](howto-kill-stuck-phase.md)
- [How to configure the circuit breaker](howto-config-toml.md)
