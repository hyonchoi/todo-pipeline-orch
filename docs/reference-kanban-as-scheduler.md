# Kanban-as-Scheduler

`pipeline-watch tick` uses the Hermes kanban board as the source of truth for
pipeline phase state. Instead of writing internal state files tracking which
phase is active, phases are registered as kanban tasks with `--parent`
dependency chains. Kanban status queries (`get_todo_kanban_status`,
`all_phases_complete`) drive the tick loop: selection, lock release, and
circuit breaker observation.

## Architecture

```
tick starts
    |
    v
[run_selection] -- picks TODO-10 or picked=None
    |
    v
[register_todo_phases] -- creates kanban tasks:
    phase_2_autoplan  <--parent--  phase_4_development  <--parent--  phase_5_review  <--parent--  phase_6_1_cso
    (running)                  (ready)                  (ready)                  (ready)
    |
    v  (phase_2 completes -> phase_4 transitions to running)
[observe_outcomes] -- reads kanban status map, writes JSONL to .hermes/outcomes/
    |
    v
[CircuitBreaker.observe_from_outcomes] -- reads JSONL, updates no-progress counter
    |
    v
[all_phases_complete] -- checks if all kanban tasks are done/failed
    |
    v
tick lock released (if complete) / skip (if in-flight)
```

**Why kanban instead of internal state?**
- The kanban board is already the operator's UI. Phase transitions are visible
  from the board, not hidden in `.hermes/phase_started/` files.
- The `--parent` dependency chain means kanban enforces sequential phase
  execution — the orchestrator doesn't need to manage phase ordering.
- `ready` status on the board means "blocked on parent" without the
  orchestrator needing to track inter-phase dependencies.

## API

### `register_todo_phases`

Registers phases as kanban tasks with `--parent` dependency chains and
`--idempotency-key` for dedup.

```python
register_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
) -> list[str]
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `todo_id` | `str` | — | TODO ID (e.g., "TODO-10"). Embedded in task body JSON header. |
| `tick_id` | `str` | — | ULID tick ID. Used as part of `--idempotency-key` for dedup. |
| `board_slug` | `str` | — | Kanban board slug (project slug). Passed to `hermes kanban` commands. |
| `project_dir` | `str \| Path` | — | Project directory. Passed as `--workspace` to `hermes kanban`. |
| `phases_path` | `str \| Path \| None` | Repo default | Path to `phases.yaml`. Defaults to `configs/phases.yaml` relative to the package. |

**Returns:** List of created task IDs in phase order.

**Raises:** `RuntimeError` if task creation fails — already-created tasks are
archived before raising.

**Behavior:**
1. Reads `phases.yaml` (loaded via `hermes_pipeline.phases.load_phases`).
2. For each phase in order, runs `hermes kanban create` with:
   - `--tenant <board_slug>` — target board
   - `--workspace <project_dir>` — project context
   - `--idempotency-key <tick_id>:<phase_key>` — dedup key (e.g., `01HA6PH2V0ZJ7GK0S39D243TQX:phase_2_autoplan`)
   - `--parent <prev_task_id>` — dependency chain (second phase depends on first, etc.)
   - `--body <json_header>\n<phase_prompt>` — task body with JSON header on first line
3. The JSON header: `{"phase_key":"phase_2_autoplan","project_slug":"demo","tick_id":"01HA","todo_id":"TODO-10"}`
4. If a task creation fails mid-registration, already-created tasks are archived
   via `hermes kanban archive <task_id>` before raising.

**Mid-registration failure:** The kanban-as-scheduler design requires all phases
to exist in order. If the second of four phases fails to register, the first is
archived — it can't run without its successor, and leaving it in `running`
blocks the next tick from selecting the same TODO.

### `get_todo_kanban_status`

Queries kanban for all tasks of a tick, returns a `{phase_key: status}` map.

```python
get_todo_kanban_status(board_slug: str, tick_id: str) -> dict[str, str]
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `board_slug` | `str` | — | Kanban board slug |
| `tick_id` | `str` | — | ULID tick ID to filter tasks by |

**Returns:** Dict mapping `phase_key` to status (e.g., `{"phase_2_autoplan": "done", "phase_4_development": "running"}`). Empty dict if no tasks match.

**Kanban statuses:** `running`, `ready`, `done`, `failed`, `archived`.
- `running` — phase is actively executing
- `ready` — phase is queued (blocked on `--parent` completion)
- `done` — phase completed successfully
- `failed` — phase execution failed
- `archived` — phase was archived mid-registration (abandoned)

### `all_phases_complete`

Checks if every kanban task for a tick is in a terminal status.

```python
all_phases_complete(board_slug: str, tick_id: str) -> bool
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `board_slug` | `str` | — | Kanban board slug |
| `tick_id` | `str` | — | ULID tick ID |

**Returns:** `True` if every task is in a completion status (`done` or `failed`). `False` if any task is still in-flight (`running`, `ready`), archived, or if the kanban CLI fails.

**Completion statuses:** `done` and `failed`. Archived is not a completion
status — it indicates the tick didn't finish cleanly.

**Conservative on failure:** If the `hermes kanban list` CLI call fails or
returns no tasks, returns `False`. This prevents accidentally releasing the
tick lock on transient kanban failures.

### `observe_outcomes`

Writes phase completion/failure outcomes from kanban status to JSONL sidecars.

```python
observe_outcomes(
    *,
    state_dir: Path,
    tick_id: str,
    status_map: dict[str, str],
) -> None
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `state_dir` | `Path` | — | State directory (e.g., `~/.hermes`) |
| `tick_id` | `str` | — | ULID tick ID |
| `status_map` | `dict[str, str]` | — | `{phase_key: status}` from `get_todo_kanban_status` |

**Writes to:** `state_dir/outcomes/<tick_id>-phases.json` (JSONL, append-only, file-locked).

**Outcome types:**

| Status | Outcome written | Example |
|---|---|---|
| `done` | `phase_complete` | `{"outcome": "phase_complete", "phase_key": "phase_2_autoplan"}` |
| `failed` | `failed_at_phase_<key>` | `{"outcome": "failed_at_phase_phase_4_development", "detail": {"kanban_status": "failed"}}` |
| `archived` | `failed_at_phase_<key>` | `{"outcome": "failed_at_phase_phase_2_autoplan", "detail": {"kanban_status": "archived"}}` |
| `running`, `ready` | (skipped) | In-flight phases are not written |
| all `done` | `all_phases_complete` | `{"outcome": "all_phases_complete"}` |

**High-watermark dedup:** If an outcome for a phase_key already exists in the
file, it is not written again. Running `observe_outcomes` twice with the same
`status_map` does not duplicate entries.

**File locking:** Uses `fcntl.flock(LOCK_EX)` on the file descriptor for
atomic append — safe for concurrent tick access.

### `CircuitBreaker.observe_from_outcomes`

Reads the JSONL outcome file and derives the no-progress judgment for the
circuit breaker.

```python
cb = CircuitBreaker(
    state_path=state_dir / "circuit.json",
    no_progress_threshold=3,
    backoff_interval_min=30,
    alert_dedup_hours=24,
    slack_channel="#alerts",
)
cb.observe_from_outcomes(
    state_dir=state_dir,
    prior_tick_id=prior_tick_id,
)
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `state_dir` | `Path` | — | State directory containing `outcomes/` |
| `prior_tick_id` | `str` | — | ULID of the previous tick |

**Reads:** `state_dir/outcomes/<prior_tick_id>-phases.json`

**Decision logic:**

| Outcome detected | Effect on circuit breaker |
|---|---|
| `phase_complete` or `all_phases_complete` | Reset `consecutive_no_progress` to 0, cancel backoff |
| `failed_at_phase_*` | Increment `consecutive_no_progress` counter |
| `picked_none` | No change — pipeline is idle, not stalled |
| No outcomes (file missing or empty) | No change — tick still in-flight |

## Related

- [How to run pipeline-watch tick](howto-pipeline-tick.md) — practical guide
- [Pipeline State Machine](hermes-state-machine.md) — full tick lifecycle
- [How to configure via .hermes/config.toml](howto-config-toml.md) — circuit breaker settings
