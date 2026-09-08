# How to run a manual pipeline tick

A tick is one pass of the pipeline: select a TODO via the Hermes agent,
register phases as kanban tasks, and observe the circuit breaker. The
`tpo tick` command fires a single scan-loop tick immediately so you can
iterate without waiting for the cron schedule.

By the end of this guide, you'll have run a tick, inspected the kanban
board, and verified the outcome files.

## Prerequisites

- The pipeline is installed: `uv sync` (see [Getting Started](tutorial-getting-started.md#step-1-install-and-verify-tpo)).
- Hermes is installed and authenticated: `hermes login`.
- A Hermes kanban board is configured for your project (check with `hermes kanban list`).
- The project has `.hermes/pipeline.toml` (`tpo init <project>`). A scan
  (`tpo tick` with no argument) skips directories without it with a WARNING;
  `tpo tick <project>` warns and falls back to the default contract.
- The project has a github.com `origin` remote (`tpo tick <project>` exits 2
  otherwise).
- At least one open issue carries `tpo:todo` + `ready-for-agent`. The backlog
  itself lives in GitHub Issues (`tpo:todo` label; canonical ID
  `TODO-<issue-number>`) — see
  [issue tracker](agents/issue-tracker.md#tpo-backlog-items) and
  [ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md).
- Every external skill required by the selected profile is provisioned in the
  worker client's discovery root. Check the selected client with
  `tpo config get prompt_client` and its prerequisites with
  `tpo doctor <project>`.
- `.hermes/config.toml` is configured with `[selection]` and `[circuit_breaker]` sections
  — see [howto-config-toml.md](howto-config-toml.md).

## Steps

### 1. Run a tick

```bash
uv run tpo tick
```

This runs the full scan-loop tick:
```
tick:
  1. Acquire global TickLock
  2. Discover active projects (scans projects_dir)
  3. For each project:
     a. Check prior tick (per-project)
     b. Fetch eligible `tpo:todo` issues via `gh` and run selection (per-project)
     c. Validate the selected TODO's Plan when the profile requires one
     d. Render every phase body from the selected profile for prompt_client
     e. Persist current_tick_id.txt and the tick_started outcome
     f. Create the prepared Hermes kanban tasks
  4. Release lock
```

Steps 3c–3f form the production registration boundary. If Plan validation or
any body render fails, the tick records `failed_to_spawn` without persisting
the current tick or creating a Hermes task. Persistence occurs only after all
bodies are valid and immediately before the first task creation.

### 2. Check what the selection picked

In a multi-project setup, decisions are persisted per-project at `<project>/.hermes/decisions/<tick_id>.json`. Inspect the latest (substitute `<project>` with your project name):

```bash
jq '{picked: .picked, rationale: .rationale}' \
  ~/projects/<project>/.hermes/decisions/$(ls -t ~/projects/<project>/.hermes/decisions/ | head -1)
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
- `todo` — subsequent executable phases are waiting on `--parent` completion
- `ready` — an executable phase is runnable and queued for dispatch
- `blocked` — human gate phases, which stay blocked until manual approval

The `--parent` chain means phases execute sequentially through the kanban
board. When phase 2 completes, phase 4 transitions from `todo` through `ready`
to `running`
automatically — the orchestrator doesn't need to manage the handoff. When
phase 4 completes, phase 5 (`phase_5_review`) transitions to `running`, and
when phase 5 completes, phase 6.1 (CSO) transitions to `running`. Human gate
gate phases remain in the parent chain but are unassigned, have no goal, and
carry a sticky `needs_input` block, so a worker cannot auto-run them.

Client-dependent gstack prompts use Claude Code slash syntax (for example,
`/review` and `/ship`) when `prompt_client=claude`, and Codex dollar syntax
(`$review` and `$ship`) when `prompt_client=codex`. This setting changes task
body vocabulary and prepends external-client delegation instructions to each
executable kanban task; Hermes still dispatches the task, but the task body
requires the Hermes worker to invoke the selected client (`claude -p` or
`codex exec`) through the `ai-coding-agents` skill.

See [reference-kanban-as-scheduler.md](reference-kanban-as-scheduler.md) for how the kanban-as-scheduler flow works.

After a tick completes (or you run another tick that detects the prior tick
is done), outcomes are written to `<project>/.hermes/outcomes/`:

```bash
cat ~/projects/<project>/.hermes/outcomes/$(ls -t ~/projects/<project>/.hermes/outcomes/ | head -1) | jq .
```

You'll see JSONL entries like:
```json
{"outcome": "phase_complete", "phase_key": "phase_2_autoplan"}
{"outcome": "phase_complete", "phase_key": "phase_4_development"}
{"outcome": "phase_complete", "phase_key": "phase_5_review"}
{"outcome": "all_phases_complete"}
```

See the [outcome types table](reference-kanban-as-scheduler.md#observe_outcomes)
for all possible outcomes, including the new review outcomes: `review_clean`, `review_reverted_test_failure`, `review_timeout`, and `review_skipped_no_diff`.

### 4. Inspect PR handoff

The deprecated `gstack` profile ends at Phase 8. Phase 8 runs `/ship` in Claude
Code or `$ship` in Codex, pushes the branch, opens or updates a PR, and does not
merge it. The pipeline records the branch in
`<project>/.hermes/pipeline_branch.txt`; later ticks check that PR and skip new
selection while it is open, closed without merge, or temporarily unverifiable.

### 5. Check the circuit breaker state

Circuit breaker state is per-project:

```bash
cat ~/projects/<project>/.hermes/circuit.json | jq .
```

Key fields:
- `consecutive_no_progress` — resets to 0 when a TODO is selected,
  increments on no-progress ticks. When it hits `no_progress_threshold`
  (default: 3), a Slack alert is sent.
- `last_alert_at` — ISO timestamp of the last Slack alert; used for dedup
  (one alert per `alert_dedup_hours`, default: 24).

## Debugging a Single Project

The scan loop runs over all active projects. To debug a specific project's
selection, temporarily set all other projects to `enabled = false` in their
`.hermes/project.toml`.

## State Directory

Per-project state (selection decisions, outcomes, circuit breaker) now lives
at `<project>/.hermes/`, including that project's `tick.lock`. Global
configuration remains in the configured `state_dir` (normally `~/.hermes/`),
but there is deliberately no single global tick lock.

## Troubleshooting

**"tick already in flight, skipping".**
A prior tick's kanban tasks are still running or ready, or a completed Phase 8
handoff is waiting on a PR that is open, closed without merge, or temporarily
unverifiable. Check the board with
`hermes kanban list --tenant demo` and check the PR named by
`.hermes/pipeline_branch.txt`. If tasks are stuck in `running`, manually clear
them via `hermes kanban complete <task_id>`.

**"Error: tick.lock held by pid X"**.
The tick lock is held. If the PID is alive (within `max_tick_duration_min` —
default 10 min), wait for it to complete. If the process died, the stale
marker sweep will reclaim the lock on the next tick.

**`picked=None` — no TODO selected.**
No open issue carries both `tpo:todo` and `ready-for-agent`, or every candidate
is blocked (`tpo:on-hold`, `tpo:in-progress`, open dependencies, invalid Plan).
The selection rationale in `.hermes/decisions/` explains why. A rationale of
`tracker_error: <code>` (for example `gh_missing`, `gh_auth`,
`origin_identity_invalid`) means `gh` or the `origin` remote needs fixing, not
the backlog.

**Kanban task creation fails mid-registration.**
Registration stays behind a non-spawnable barrier. Known tasks are archived
child-first only after any uncertain child becomes visible; otherwise
`pending-task-create.json` remains under `.hermes/outcomes/` and later ticks
skip the project. If interruption happens while completing the barrier, later
ticks inspect its status and either accept the completed chain or retry the
commit. Check the marker, tick logs, and archived-inclusive kanban snapshot;
see [durable registration and uncertain-create recovery](reference-kanban-as-scheduler.md#durable-registration-and-uncertain-create-recovery).

**Circuit breaker trips (consecutive_no_progress >= 3).**
Three consecutive no-progress ticks triggered a Slack alert. The gateway
service manages tick scheduling and backoff — the circuit breaker no longer
adjusts the cron interval. Check `<project>/.hermes/decisions/` for
`picked=None` decisions — the rationale explains why each tick found nothing
to do. See [howto-config-toml.md](howto-config-toml.md#loosen-the-circuit-breaker-during-onboarding)
for how to temporarily loosen the threshold.

## Related

- [Kanban-as-Scheduler reference](reference-kanban-as-scheduler.md) — full API docs
- [Pipeline State Machine](hermes-state-machine.md) — full tick lifecycle
- [How to configure the circuit breaker](howto-config-toml.md)
