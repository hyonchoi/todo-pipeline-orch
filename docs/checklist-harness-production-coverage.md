# Checklist: Harness Test Coverage of Production Code Paths (TODO-22)

Acceptance criteria for TODO-21 (revise pipeline harness to maximally reuse
production module/code). Ground truth is `hermes_pipeline/*.py` as it exists
today, verified by reading full function bodies — not signatures, not docs.

**Scope:** one automated pipeline-watch cycle in the kanban-as-scheduler path
(`kanban_mode == "hermes"`, i.e. `harness.py::_poll_kanban_phases` and its
callees). The non-kanban `PipelineRunner` + null-adapter path, multi-project
scan, Slack alerts, and preflight startup checks are out of scope — see
[docs/gstack design doc TODO-22] "NOT in scope" section for rationale.

**Cross-reference:** rows that overlap `docs/hermes-state-machine.md`'s
existing 16-row tick-level table cite the matching row instead of duplicating
it. That table describes the pipeline-tick state machine broadly; this
checklist is scoped tighter — one cycle's kanban-as-scheduler phase loop —
and adds the production-function + assertion columns that table doesn't have.

**Row format:** each row names a transition (trigger → pre-state → post-state),
the production function(s) that must fire, and the state assertion (in plain
English) that proves it happened. Where harness.py's current behavior has no
production counterpart, the row names the CORRECT target — surfacing the gap
is the point, not a reason to omit the row.

**Test link column:** all rows start as `TODO(TODO-21)`. During TODO-21, each
row's link is filled in with the spy test that encodes it, in the same PR that
makes the harness call that transition's production function.

---

## Transitions

| # | Trigger | Pre-state | Post-state | Production function(s) | State assertion | Test link |
|---|---------|-----------|------------|------------------------|------------------|-----------|
| 1 | Cycle starts, kanban mode selected | — | phases registered as kanban tasks | `kanban_tasks.register_todo_phases` | Each phase in `phases.yaml` has a corresponding kanban task; parent-chain (`--parent`) links each task to its predecessor in phase order; gate phases created with `--initial-status BLOCKED`, non-gate phases created with `--goal` | `TODO(TODO-21)` |
| 2 | Registration succeeds | tasks created | expected-phase manifest persisted | `kanban_tasks._persist_expected_phases` (internal to `register_todo_phases`) | `all_phases_complete`'s completeness check (row 9) can find the persisted phase-key list for this tick | `TODO(TODO-21)` |
| 3 | Registration fails mid-way (any `hermes kanban create` non-zero exit) | tasks partially created | tasks archived, exception raised | `kanban_tasks._archive_tasks` | Previously-created task IDs for this tick no longer appear in a subsequent `get_todo_kanban_tasks` query (archived, not active); `RuntimeError` propagates to caller | `TODO(TODO-21)` |
| 4 | Poll tick fires | kanban tasks in any status | current status snapshot obtained | `kanban_tasks.get_todo_kanban_status` | Returned dict maps `phase_key` → status for exactly the tasks whose JSON body header `tick_id` matches this cycle's tick; tasks from other ticks/tenants excluded | `TODO(TODO-21)` |
| 5 | Status transition: `(None\|ready\|blocked)` → `running` | phase not yet observed running | phase in flight | *(kanban-external — a `hermes` agent picks up the goal-mode task; harness only observes the transition via poll)* | `phase_started` event emitted with correct `phase_key`/`todo_id`; no production function to spy here — this transition is externally driven, verify via monitor event content only | `TODO(TODO-21)` |
| 6 | Status transition: `running` → `done` | phase in flight | phase complete | none directly, but must trigger row 7 | `phase_completed` event emitted; `_auto_complete_gate_tasks` invoked with `completed_phase_key` set to this phase | `TODO(TODO-21)` |
| 7 | Phase completes, a gate task's direct predecessor matches `completed_phase_key` | gate task `BLOCKED` | gate task completed | **Correct target: `gates.py` gate-completion function — none exists today.** Harness currently uses local `_auto_complete_gate_tasks` (raw `hermes kanban complete` subprocess call), which has no `gates.py` counterpart. TODO-21 must decide/build the routing target (e.g. a new `gates.py::complete_gate_task` or route through `check_gate_status`'s override path) before this row can pass. | Gate task status flips from `blocked` to a completion status via the correct production path, not a harness-local subprocess call | `TODO(TODO-21)` |
| 8 | Status transition: `running` → `failed` (or fast phase skips straight `(None\|ready\|blocked)` → `failed`) | phase in flight or not-yet-observed | phase failed | `N/A (premise corrected)` — see [#16](https://github.com/hyonchoi/todo-pipeline-orch/issues/16). Originally proposed routing this through `circuit.py::CircuitBreaker.observe` / `observe_from_outcomes`; that premise was wrong. `CircuitBreaker` is cross-tick, disk-persisted, and Slack-alerting — it protects the pipeline-watch loop across many ticks over time. The harness's `ConvergenceDetector`/`_ConvergenceMonitor` is in-memory, per-run, phase-granularity, and side-effect-free — it protects one cycle's poll loop. These solve different problems and must not be merged; the harness-local detector is the correct production path for this transition. | `phase_failed` event emitted with classified `error_class`; failure observation recorded via the harness-local `ConvergenceDetector`, which is correct as-is | N/A |
| 9 | N consecutive same-class failures reach `CircuitBreaker`'s threshold | breaker below threshold | breaker trips, cycle halts | `N/A (premise corrected)` — see [#16](https://github.com/hyonchoi/todo-pipeline-orch/issues/16). Originally proposed routing this through `circuit.py::CircuitBreaker`'s halt/trip logic; that premise was wrong for the same reason as row 8: `CircuitBreaker` is cross-tick/disk-persisted/Slack-alerting, while the harness-local `_ConvergenceMonitor.__call__` raising `ConvergenceHaltError` when `ConvergenceDetector.should_halt()` returns true is in-memory/per-run/side-effect-free. Bypassing `circuit.py` here is correct, not a gap. | Cycle halts (poll loop exits, `all_terminal=True`) only after the harness-local `ConvergenceDetector`, not `circuit.py`'s breaker, reports the halt condition met | N/A |
| 10 | All phases reach a terminal status (`done`/`failed`, cross-ref state-machine row on completion) | poll loop running | poll loop exits | (loop-internal — `TERMINAL_STATUSES` check against `kanban_tasks.get_todo_kanban_status`'s return, no separate production fn) | `all(s in TERMINAL_STATUSES for s in status_map.values())` is true; poll loop's `while not all_terminal` exits | `TODO(TODO-21)` |
| 11 | Poll loop exits (terminal or convergence-halted) | final status known | outcomes written to decision store | `kanban_tasks.observe_outcomes` | `.hermes/outcomes/<tick_id>-phases.json` contains one line per newly-done/failed/archived phase (`phase_complete`/`failed_at_phase_<key>` outcomes), high-watermarked against prior entries; `all_phases_complete` sentinel written once all phases in `COMPLETION_STATUSES` | `TODO(TODO-21)` |
| 12 | Contract lookup at poll start | — | assignee resolved | `contract.load_contract` | `register_todo_phases`'s `--assignee` arg matches `PipelineContract.assignee` from `.hermes/pipeline.toml`, not a hardcoded `"default"` unless load genuinely fails (fallback path, logged as warning) | `TODO(TODO-21)` |
| 13 | Overall cycle timeout (`--timeout` seconds elapse with poll loop still running) | poll loop running on worker thread | timeout surfaced, in-flight phase reported | *(harness-internal `_run_with_timeout` thread join — no production fn substitutes for a wrapper-level worker-thread timeout, out of scope per Constraints)* | `phase_timed_out` event emitted for the phase that was in-flight at timeout, if any | `TODO(TODO-21)` |

---

## Not covered by this checklist (see design doc "NOT in scope")

- Non-kanban `PipelineRunner` + null-adapter harness path (rows would live in
  a separate checklist if that path is ever prioritized).
- `tick.py::TickLock.acquire` — the kanban-as-scheduler path as implemented in
  `harness.py` does not itself acquire a tick lock (that's a pipeline-watch
  tick-runner concern, upstream of what the harness fixture drives). If TODO-21
  changes this, add a row here.
- Multi-project scan, Slack alert delivery, preflight startup checks.
- `_dispatch_phase` / `phases.run` / marker lifecycle (`.hermes/phase_started/`)
  — these only fire in the non-kanban path, out of scope (see design doc
  Constraints correction).
