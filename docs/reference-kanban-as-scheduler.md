# Kanban-as-Scheduler

`tpo tick` uses the Hermes kanban board as the source of truth for
pipeline phase state. Instead of writing internal state files tracking which
phase is active, phases are registered as one kanban task chain with
`--parent` dependencies. Gate phases, when a profile defines them, stay in that
chain but are created unassigned and explicitly receive a sticky `needs_input`
block. Kanban status queries (`get_todo_kanban_status`,
`all_phases_complete`) drive the tick loop: selection, lock release, and
circuit breaker observation.

For `native-sdd`, the chain is compiled from the tracked `tpo-plan` manifest:

```text
plan worker -> controller gate -> plan worker -> controller gate
            -> independent review -> review acceptance gate
            -> finish -> TODO closeout -> human merge gate
```

Controller gates are unassigned and are completed only by TPO after validating
the closing worker's `metadata.tpo_result` and independent Git facts. Stable
keys use the tick and step identity, including `plan:<task-id>`,
`validate:<task-id>`, `review:<round>`, `review-fix:<round>`,
`fix-validation:<round>`, and `re-review:<round>`. A legacy Plan without a
manifest retains the static single-development/review/finish/human chain for
compatibility. Retry still validates the registered repository, worktree,
branch, pinned base TODO, and Plan hashes, but bypasses manifest-only dynamic
reconcilers.

Hermes >= 0.19.0 is required for the Kanban and closing-result contracts.

For the deprecated `gstack` profile, completion of the terminal Phase 8 task means
the branch was handed to a PR, not merged. `tpo tick` reads
`.hermes/pipeline_branch.txt` and skips new selection while that PR is open,
closed without merge, or temporarily unverifiable. Once GitHub reports the PR as
`MERGED`, the next tick may select new work.

## Operator execution deadlines

The selected profile's `phases.yaml` owns each executable phase deadline.
The deadline follows this path:

```
Phase.timeout
  -> external Codex/Claude deadline
  -> PreparedPhaseTask.timeout
  -> hermes kanban create --max-runtime <timeout + 60> --max-retries 1
```

Delegated clients run as tracked background processes so Hermes's 600-second
foreground terminal cap cannot shorten a phase configured for longer work.
The final minute is cleanup-only: after the client deadline, the dispatcher
terminates the external process tree and confirms it is no longer running.
On timeout or non-zero exit, it writes known external-agent failure metadata
through `kanban_comment`, then uses the supported
`kanban_block(kind="needs_input", reason=...)` transition. It does not inspect,
implement, or commit partial work.

Use `hermes kanban show <task-id> --json` to inspect task state and
`hermes kanban log <task-id>` to inspect the worker audit trail.

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

`prompt_client` selects phase-task vocabulary and external-client delegation
guidance. `claude` renders `Claude Code` with slash invocations such as
`/review` and `/ship` plus `claude -p` delegation instructions; `codex` renders
`Codex` with dollar invocations such as `$review` and `$ship` plus `codex exec`
delegation instructions. It does not choose the Hermes assignee, profile, or
model.

### `PreparedPhaseTask`

```python
@dataclass(frozen=True)
class PreparedPhaseTask:
    phase_key: str
    name: str
    body: str
    turns: int
    gate: bool
    timeout: int = 1800
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
[reconcile pending-task-create marker]
    |
    +-- unresolved, malformed, or cleanup not confirmed
    |       |
    |       v
    |   log "project <slug>: unresolved Hermes task creation; skipping"
    |       |
    |       v
    |   skip this project tick
    |
    +-- no marker or recovery archive confirmed
            |
            v
[process prior tick] -- read prior ID; observe completed outcomes
    |
    +-- prior phases still in-flight
    |       |
    |       v
    |   log "project <slug>: prior tick <id> still in-flight, skipping"
    |       |
    |       v
    |   skip this project tick
    |
    +-- PR handoff pending
    |       |
    |       v
    |   log "project <slug>: prior tick <id> is waiting on PR handoff, skipping"
    |       |
    |       v
    |   skip this project tick
    |
    +-- no prior tick, or completed/resolved prior tick
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
[create_prepared_todo_phases]
    persist pending-task-create marker
    -> create unassigned, non-spawnable registration barrier
    -> persist known cleanup IDs
    |
    +-- for each phase:
    |       persist current create intent + known cleanup IDs
    |       -> create task parented to barrier/previous phase
    |          and, for a gate, leave it unassigned and apply needs_input block
    |       -> persist the returned ID first in child-first cleanup order
    |
    v
[persist expected-phases sentinel] -- records the complete durable chain
    |
    v
[complete registration barrier] -- only now may work become runnable:
    phase_2_autoplan  <--parent--  phase_4_development  <--parent--  phase_5_review  <--parent--  phase_6_1_cso
    (ready)                    (todo)                   (todo)                   (todo)
    |
    v
tick ends; a later tick begins above and observes the prior chain. Hermes parent
dependencies allow each later executable phase to run when its predecessor ends.
```

**Why kanban instead of internal state?**
- The kanban board is already the operator's UI. Phase transitions are visible
  from the board, not hidden in `.hermes/phase_started/` files.
- The `--parent` dependency chain means kanban preserves the configured phase
  order — the orchestrator doesn't need to manage phase ordering.
- Gate phases stay in the parent chain, while their sticky `needs_input` block
  keeps them nonspawnable until the gate is explicitly resolved. The default
  `gstack` profile has no gate phase.
- `todo` means an executable task is still waiting on its parent. `ready` means
  it is runnable and queued for dispatch.

### Durable registration and uncertain-create recovery

Before any phase is created, the orchestrator creates an unassigned registration
barrier. The first phase is parented to the barrier; every later phase is
parented to the preceding phase.
Before every remote `hermes kanban create`, including the barrier, the
orchestrator atomically writes the per-project pending marker at
`<project>/.hermes/outcomes/pending-task-create.json`. The marker records the
tenant, tick ID, current create intent, and known cleanup IDs. After Hermes
returns a validated ID, the marker is rewritten immediately with that ID first,
so cleanup is always child-first and the barrier is last.

After every task is known, the orchestrator atomically writes
`<project>/.hermes/outcomes/expected-phases.json`. This sentinel lists the full
expected chain and lets completion checks reject a partial board snapshot. It
then replaces the cleanup marker with a barrier-commit-pending marker and
completes the registration barrier. The marker spans that remote mutation and
is cleared only after success. Completing the barrier satisfies the first
phase's parent: an executable first phase moves from `todo` to `ready`, while a
gate first phase remains sticky-blocked. Later tasks wait for their preceding
phase. Gate tasks are unassigned and receive
`hermes kanban block --kind needs_input`, so their block remains explicit while
the parent chain preserves phase order.

An inconclusive create is fail-closed. The registration call retries the same
idempotency key and takes a snapshot; if the task is still not visible, the
pending marker remains without modifying any known parent. At the start of every
later project tick, before reading the prior tick, selection, or creating work,
the orchestrator reads that marker. If the current task becomes visible, its ID
is prepended to the cleanup list; cleanup then archives tasks child-first. If it
is still invisible, the known parents remain untouched and the project stays
skipped. An `OSError` while creating records only IDs known to exist in
cleanup-only mode. For barrier-commit-pending state, reconciliation accepts a
snapshot-confirmed `done` barrier, retries completion from `ready` or `todo`,
and otherwise fails closed.

Archive command output is not treated as proof. Recovery refreshes a tenant
snapshot with archived tasks included and removes the marker only when every
listed ID has status `archived`. A malformed or unreadable marker, an unresolved
create, or unconfirmed cleanup keeps the project skipped.

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
    phases_path: str | Path | None = None,
    prompt_client: PromptClient = "claude",
) -> list[PreparedPhaseTask]
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `todo_id` | `str` | — | TODO ID (e.g., "TODO-10"). Embedded in task body JSON header. |
| `tick_id` | `str` | — | ULID tick ID embedded in each task body header. |
| `board_slug` | `str` | — | Project slug embedded in each task body header. |
| `phases_path` | `str \| Path \| None` | `None` | Profile `phases.yaml`. The low-level default is the packaged legacy implicit profile (`contract.LEGACY_IMPLICIT_PROFILE`, i.e. `gstack`), never the `tpo init` default; production resolves `contract.profile` and passes its path explicitly. |
| `prompt_client` | `PromptClient` | `"claude"` | Renders the fixed product label, verified skill invocation vocabulary, and external-client delegation guidance. |

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

Creates already-prepared tasks behind a registration barrier. Every phase follows
the previous phase with `--parent`; gate phases remain nonspawnable by omitting
worker assignees and goals, and receive a sticky `needs_input` block.

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

**Returns:** Created phase-task IDs in phase order. The internal registration
barrier ID is not returned.

**Raises:** `RuntimeError` if Hermes creation, gate blocking, sentinel
persistence, barrier completion, or confirmed cleanup fails. Known tasks remain
in durable cleanup or barrier-commit-pending state until recovery is confirmed.

The function first creates an unassigned, non-goal registration barrier. For
each prepared phase, it then runs `hermes kanban create` with:

- `--tenant <board_slug>` — target board
- `--workspace dir:<project_dir>` — project context
- `--idempotency-key <tick_id>:<phase_key>` — dedup key (e.g.,
  `01HA6PH2V0ZJ7GK0S39D243TQX:phase_2_autoplan`)
- `--parent <prev_task_id>` — dependency chain for every phase; the first phase
  uses the registration barrier and each later phase uses its predecessor
- `--assignee -` for the barrier and gates; executable tasks use `assignee`
- `--body <json_header>\n<phase_prompt>` — task body with JSON header on first
  line
- `--max-runtime <timeout + 60>` and `--max-retries 1` for executable tasks —
  the selected phase deadline plus cleanup-only grace and a terminal single
  attempt; gate tasks omit both flags

After creating a gate, it runs
`hermes kanban block --kind needs_input <gate-task-id>`. After the
expected-phase sentinel is durable, it completes the barrier.

**Mid-registration failure:** The kanban-as-scheduler design requires all phases
to exist in order. If the second of four phases fails to register, the first
cannot run because its barrier remains incomplete. Known tasks are archived
child-first, with the barrier last. If the uncertain current task is invisible,
known parents are not archived until that task is resolved. If cleanup cannot be
confirmed from an archived-inclusive snapshot, the durable pending marker
remains and later ticks skip the project until cleanup finishes.

### Preparation and creation are always split

There is no combined prepare-and-create wrapper. Production `_tick_project`
calls the two functions separately so its exact sequence is:

1. prepare every rendered body (a render failure, including a malformed later
   phase, happens here, before any task exists);
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

**Kanban statuses:** `todo`, `running`, `ready`, `blocked`, `done`, `failed`,
`archived`.
- `todo` — executable phase is waiting for its `--parent`
- `running` — phase is actively executing
- `ready` — executable phase is runnable and queued
- `blocked` — human gate phase has a sticky `needs_input` block
- `done` — phase completed successfully
- `failed` — phase execution failed
- `archived` — phase was archived mid-registration (abandoned)

### `all_phases_complete`

Checks if every kanban task for a tick is in a terminal status.

```python
all_phases_complete(
    tenant: str,
    tick_id: str,
    *,
    state_dir: str | Path | None = None,
) -> bool
```

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `tenant` | `str` | — | Kanban tenant (project slug) |
| `tick_id` | `str` | — | ULID tick ID |
| `state_dir` | `str \| Path \| None` | `None` | State directory used to inspect the no-work outcome when no tasks exist. |

**Returns:** `True` if every task is in a completion status (`done` or
`failed`). `False` if any task is still in-flight (`todo`, `running`, `ready`,
`blocked`), archived, or if the kanban CLI fails.

**Completion statuses:** `done` and `failed`. Archived is not a completion
status — it indicates the tick didn't finish cleanly.

**No-task and failure behavior:** If no tasks exist and `state_dir` contains a
`picked_none` outcome for the tick, returns `True` because the tick completed
without work. Otherwise an empty snapshot or failed `hermes kanban list` call
returns `False`, preventing accidental lock release on an incomplete tick or
transient kanban failure.

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
| `todo`, `running`, `ready`, `blocked` | (skipped) | In-flight phases are not written |
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
