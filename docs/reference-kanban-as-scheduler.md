# Kanban-as-Scheduler

`tpo tick` uses the Hermes kanban board as the source of truth for
pipeline phase state. Instead of writing internal state files tracking which
phase is active, executable phases are registered as kanban tasks with
`--parent` dependency chains. Gate phases, when a profile defines them, are
created blocked and detached from the parent chain. Kanban status queries (`get_todo_kanban_status`,
`all_phases_complete`) drive the tick loop: selection, lock release, and
circuit breaker observation.

For the default `gstack` profile, completion of the terminal Phase 8 task means
the branch was handed to a PR, not merged. `tpo tick` reads
`.hermes/pipeline_branch.txt` and skips new selection while that PR is open,
closed without merge, or temporarily unverifiable. Once GitHub reports the PR as
`MERGED`, the next tick may select new work.

## Types

### `PhaseStatus`

```python
PhaseStatus = Literal["running", "done", "failed", "ready_for_review"]
```

Status values for kanban task state transitions during phase execution.

### `KanbanOutcome`

```python
KanbanOutcome = Literal["merged", "rejected", "abandoned"]
```

Terminal outcomes passed to `clear_active_task`. Each maps to a specific kanban CLI action:

| Outcome | Kanban action | When used |
|---------|---------------|-----------|
| `merged` | `hermes kanban complete <task_id>` | TODO completed successfully |
| `rejected` | `hermes kanban archive <task_id>` | TODO explicitly rejected by operator |
| `abandoned` | `hermes kanban archive <task_id>` | Pipeline failed mid-execution (phase failure, convergence halt, timeout) |

Both `rejected` and `abandoned` archive the card — they differ in semantics: `rejected` is an operator decision, `abandoned` is a pipeline failure signal.

### `PromptClient`

```python
PromptClient = Literal["claude", "codex"]
```

`prompt_client` selects phase-task vocabulary only. `claude` renders
`Claude Code` with slash invocations such as `/review` and `/ship`; `codex`
renders `Codex` with dollar invocations such as `$review` and `$ship`. It does
not choose a worker executable, Hermes assignee, profile, or model.

### `PreparedPhaseTask`

```python
@dataclass(frozen=True)
class PreparedPhaseTask:
    phase_key: str
    name: str
    body: str
    tools: str
    turns: int
    gate: bool
```

An immutable, fully rendered task ready for Hermes creation. Preparation
finishes for the entire profile before production persists an active tick or
creates any task.

## KanbanClient Protocol

The `KanbanClient` protocol defines the interface for kanban sync operations. All methods are non-blocking and return `SyncResult`.

### `SyncResult`

```python
@dataclass(frozen=True)
class SyncResult:
    ok: bool
    task_id: str | None = None
    error: str | None = None
```

### `set_active_task(project, *, todo_id, title, phase, metadata=None) -> SyncResult`

Set the active task for a project. Called when a TODO moves into Phase 1 (Development).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `project` | `str` | — | Project slug (used as kanban tenant) |
| `todo_id` | `int` | — | TODO ID number |
| `title` | `str` | — | TODO title |
| `phase` | `str` | — | First phase name |
| `metadata` | `dict[str, str] \| None` | `None` | Optional key/value context appended to card body for debug tracing (e.g. `tick_id`, `fixture_name`, `state_dir`) |

**Returns:** `SyncResult` — `ok=True` with `task_id` on success, `ok=False` with `error` on failure (operation queued to outbox).

### `update_phase(project, *, phase, status) -> SyncResult`

Update the phase status for the active task. Called as pipeline progresses.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `project` | `str` | — | Project slug |
| `phase` | `str` | — | Phase name |
| `status` | `PhaseStatus` | — | New status |

**Returns:** `SyncResult`

### `clear_active_task(project, *, outcome) -> SyncResult`

Clear the active task. Called after the terminal phase or on pipeline failure.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `project` | `str` | — | Project slug |
| `outcome` | `KanbanOutcome` | — | Terminal outcome: `merged`, `rejected`, or `abandoned` |

**Returns:** `SyncResult`

## Implementations

### `HermesKanbanAdapter`

Real adapter using `hermes kanban` CLI commands. Requires `KanbanOutbox` and `ActiveTasksStore` for persistence.

| Command | Operation |
|---------|-----------|
| `hermes kanban create --tenant <project> <title> --body <body> --json` | `set_active_task` — creates task card |
| `hermes kanban comment <task_id> "<phase> — <status>"` | `update_phase` — posts phase status comment |
| `hermes kanban complete <task_id>` | `clear_active_task` with `outcome="merged"` |
| `hermes kanban archive <task_id>` | `clear_active_task` with `outcome="rejected"` or `"abandoned"` |

On CLI failure, operations are queued to the `KanbanOutbox` for retry via `drain_outbox()`.

## ActiveTasksStore

Atomic JSON store mapping project → task_id. Uses tmp+rename pattern for writes.

| Method | Description |
|---|---|
| `get(project)` | Returns task_id or `None` |
| `set(project, task_id)` | Creates or updates atomically |
| `drop(project)` | Removes project from store |

File location: `state_dir/active_tasks.json` (path configurable via constructor).

## KanbanOutbox

JSONL-based outbox for queued kanban operations with create-preserving collapse.

| Rule | Description |
|---|---|
| Create-preserving | If a non-create op is enqueued while a pending create exists for the same project, fold the new op's params into the create and keep only the create |
| Replace | Otherwise, replace the existing entry for that project |
| Cap | 500 entries maximum; drop oldest first on overflow |
| File location | `state_dir/kanban_outbox.jsonl` (path configurable via constructor) |

### `drain_outbox(adapter, outbox)`

Retries all queued operations. Dequeues on success, leaves on failure so it can be retried again.

## Architecture

```
tick starts
    |
    v
[run_selection] -- picks TODO-10 or picked=None
    |
    v
[prepare_todo_phases] -- renders every selected-profile body
    |
    v
[persist current_tick_id.txt + tick_started outcome]
    |
    v
[create_prepared_todo_phases] -- for each phase:
    persist pending-task-create marker
    -> create blocked kanban task
    -> validate task ID and clear its marker
    |
    v
[persist expected-phases sentinel] -- records the complete durable chain
    |
    v
[promote first executable task] -- only now may work become runnable:
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
[PR handoff check] -- skips selection while recorded branch PR is not merged
    |
    v
tick lock released (if complete) / skip (if in-flight)
```

**Why kanban instead of internal state?**
- The kanban board is already the operator's UI. Phase transitions are visible
  from the board, not hidden in `.hermes/phase_started/` files.
- The `--parent` dependency chain means kanban enforces sequential executable
  phase execution — the orchestrator doesn't need to manage phase ordering.
- Gate phases are not parented because parent completion would otherwise
  auto-unblock the gate. The default `gstack` profile has no gate phase.
- `ready` status on the board means "blocked on parent" without the
  orchestrator needing to track inter-phase dependencies.

### Durable registration and uncertain-create recovery

Every phase, including executable phases, is created with initial status
`blocked`. Before each remote `hermes kanban create`, the orchestrator atomically
writes the per-project pending marker at
`<project>/.hermes/outcomes/pending-task-create.json`. The marker records the
tenant, tick ID, phase key, and IDs already registered in the same chain. It is
removed only after Hermes returns a validated task ID for that exact create.

After every task is known, the orchestrator atomically writes
`<project>/.hermes/outcomes/expected-phases.json`. This sentinel lists the full
expected chain and lets completion checks reject a partial board snapshot. Only
after that write succeeds does it run `hermes kanban promote <first-executable-id>`.
Gate tasks remain blocked and are never promoted.

An inconclusive create is fail-closed. The registration call retries the same
idempotency key and takes a snapshot; if the task is still not visible, the
pending marker remains. At the start of every later project tick, before reading
the prior tick, selection, or creating work, the orchestrator reads that marker.
It searches the tenant snapshot for the recorded tick and phase, archives the
late-visible task together with all known predecessors, and removes the marker
only after those archives succeed. A marker that is malformed, unreadable,
unresolved, or cannot be removed keeps the project skipped for that tick.

The same file can temporarily carry a cleanup marker when archive confirmation
failed after a later registration or promotion error. The next tick archives its
listed task IDs before removing it, using the same fail-closed gate.

Operators can follow this recovery in the tick logs: an unresolved marker emits
`project <slug>: unresolved Hermes task creation; skipping`; successful cleanup
logs `archived kanban task <task_id>` for each archived card. The on-disk marker
and the expected-phase sentinel are the durable evidence to inspect between
ticks.

## API

### `prepare_todo_phases`

Loads one phase profile and renders every body without calling Hermes or
writing tick state.

```python
prepare_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
    prompt_client: PromptClient = "claude",
) -> list[PreparedPhaseTask]
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `todo_id` | `str` | — | TODO ID (e.g., "TODO-10"). Embedded in task body JSON header. |
| `tick_id` | `str` | — | ULID tick ID embedded in each task body header. |
| `board_slug` | `str` | — | Project slug embedded in each task body header. |
| `project_dir` | `str \| Path` | — | Project workspace associated with the prepared registration. |
| `phases_path` | `str \| Path \| None` | `None` | Profile `phases.yaml`. The low-level default is packaged `gstack`; production resolves `contract.profile` and passes its path explicitly. |
| `prompt_client` | `PromptClient` | `"claude"` | Renders the fixed product label and verified skill invocation vocabulary. |

**Returns:** All `PreparedPhaseTask` values in profile order.

**Raises:** `ValueError` for an invalid TODO ID, or `PhasePromptRenderError`
when any prompt has malformed or unresolved template syntax. Failure is atomic
with respect to Hermes: no task is created.

**Behavior:**
1. Reads `phases.yaml` with `hermes_pipeline.phases.load_phases`.
2. Strictly renders every prompt with TODO, tick, project, and client
   vocabulary.
3. Builds the JSON body header:
   `{"phase_key":"phase_2_autoplan","project_slug":"demo","tick_id":"01HA","todo_id":"TODO-10"}`.
4. Returns only after every body is valid.

### `create_prepared_todo_phases`

Creates already-prepared tasks. Executable phases use `--parent` dependency
chains; gate phases are detached and blocked.

```python
create_prepared_todo_phases(
    *,
    prepared: list[PreparedPhaseTask],
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    assignee: str = "default",
) -> list[str]
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `prepared` | `list[PreparedPhaseTask]` | — | Fully rendered tasks in registration order. |
| `tick_id` | `str` | — | ULID used in each idempotency key. |
| `board_slug` | `str` | — | Passed as the Hermes kanban tenant. |
| `project_dir` | `str \| Path` | — | Passed as `--workspace dir:<project_dir>`. |
| `assignee` | `str` | `"default"` | Assigned to executable tasks; gate tasks always use `-`. |

**Returns:** Created task IDs in phase order.

**Raises:** `RuntimeError` if Hermes creation fails. Tasks created earlier in
the same call are archived before the error is raised.

For each prepared phase, this function runs `hermes kanban create` with:

- `--tenant <board_slug>` — target board
- `--workspace dir:<project_dir>` — project context
- `--idempotency-key <tick_id>:<phase_key>` — dedup key (e.g.,
  `01HA6PH2V0ZJ7GK0S39D243TQX:phase_2_autoplan`)
- `--parent <prev_task_id>` — dependency chain for executable phases only
  (gate phases omit it so they stay manually blocked)
- `--body <json_header>\n<phase_prompt>` — task body with JSON header on first
  line

**Mid-registration failure:** The kanban-as-scheduler design requires all phases
to exist in order. If the second of four phases fails to register, the first is
archived — it cannot run because every phase remains `blocked` until the complete
chain and expected-phase sentinel are durable. If cleanup cannot be confirmed,
the durable pending marker remains and later ticks skip the project until cleanup
finishes.

### `register_todo_phases`

Backward-compatible convenience wrapper for callers that do not need to
persist state between preparation and task creation. It calls
`prepare_todo_phases`, then `create_prepared_todo_phases`.

```python
register_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
    assignee: str = "default",
    prompt_client: PromptClient = "claude",
) -> list[str]
```

The wrapper accepts the union of the preparation and creation parameters and
returns the created task IDs. A render failure occurs before the creation call,
including when a later phase is malformed. Production `_tick_project` uses the
two functions separately so its exact sequence is:

1. prepare every rendered body;
2. persist `current_tick_id.txt` and the `tick_started` outcome;
3. create the prepared Hermes tasks.

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
- `ready` — executable phase is queued (blocked on `--parent` completion)
- `blocked` — human gate phase is waiting for manual approval
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

- [How to run tpo tick](howto-pipeline-tick.md) — practical guide
- [Pipeline State Machine](hermes-state-machine.md) — full tick lifecycle
- [How to configure via .hermes/config.toml](howto-config-toml.md) — circuit breaker settings
