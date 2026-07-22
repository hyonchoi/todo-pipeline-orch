# Checklist: Harness Test Coverage of Production Code Paths (TODO-22)

Acceptance criteria for TODO-21 (revise pipeline harness to maximally reuse
production module/code). Ground truth is `hermes_pipeline/*.py` as it exists
today, verified by reading full function bodies — not signatures, not docs.

**Scope:** one automated pipeline-watch cycle in the kanban-as-scheduler path
(`kanban_mode == "hermes"`, i.e. `harness.py::_poll_kanban_phases` and its
callees). Multi-project scan, Slack alerts, and preflight startup checks are
out of scope — see [docs/gstack design doc TODO-22] "NOT in scope" section for
rationale.

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
| 1 | Cycle starts, kanban mode selected | — | phases registered as kanban tasks | `kanban_tasks.register_todo_phases` | Each phase in `phases.yaml` has a corresponding kanban task; parent-chain (`--parent`) links each task to its predecessor in phase order; gate phases created with `--initial-status BLOCKED`, non-gate phases created with `--goal` | `tests/test_kanban_tasks.py::TestRegisterTodoPhases::test_creates_tasks_with_parent_chain`, `::test_gate_phase_registered_blocked_without_goal` |
| 2 | Registration succeeds | tasks created | expected-phase manifest persisted | `kanban_tasks._persist_expected_phases` (internal to `register_todo_phases`) | `all_phases_complete`'s completeness check (row 9) can find the persisted phase-key list for this tick | `tests/test_kanban_tasks.py::TestPersistExpectedPhases::test_writes_to_project_hermes_dir` |
| 3 | Registration fails mid-way (any `hermes kanban create` non-zero exit) | tasks partially created | tasks archived, exception raised | `kanban_tasks._archive_tasks` | Previously-created task IDs for this tick no longer appear in a subsequent `get_todo_kanban_tasks` query (archived, not active); `RuntimeError` propagates to caller | `tests/test_kanban_tasks.py::TestRegisterTodoPhases::test_mid_registration_failure_archives_created_tasks` |
| 4 | Poll tick fires | kanban tasks in any status | current status snapshot obtained | `kanban_tasks.get_todo_kanban_status` | Returned dict maps `phase_key` → status for exactly the tasks whose JSON body header `tick_id` matches this cycle's tick; tasks from other ticks/tenants excluded | `tests/test_kanban_tasks.py::TestGetTodoKanbanStatus::test_returns_status_map` |
| 5 | Status transition: `(None\|ready\|blocked)` → `running` | phase not yet observed running | phase in flight | *(kanban-external — a `hermes` agent picks up the goal-mode task; harness only observes the transition via poll)* | `phase_started` event emitted with correct `phase_key`/`todo_id`; no production function to spy here — this transition is externally driven, verify via monitor event content only | `TODO(TODO-21)` |
| 6 | Status transition: `running` → `done` | phase in flight | phase complete | none directly, but must trigger row 7 | `phase_completed` event emitted; `_auto_complete_gate_tasks` invoked with `completed_phase_key` set to this phase | `TODO(TODO-21)` |
| 7 | Phase completes, a gate task's direct predecessor matches `completed_phase_key` | gate task `BLOCKED` | gate task completed | `kanban_tasks.complete_todo_kanban_task`. Harness-local `_auto_complete_gate_tasks` retains its predecessor-map construction and eligibility filtering, but delegates the completion mechanic to this production function instead of a raw `hermes kanban complete` subprocess call (see [#19](https://github.com/hyonchoi/todo-pipeline-orch/issues/19)). | Gate task status flips from `blocked` to a completion status via `complete_todo_kanban_task`, not a harness-local subprocess call | `tests/test_harness.py::TestAutoCompleteGateTasks::test_completes_blocked_gate_tasks` (spies on `hermes_pipeline.kanban_tasks.complete_todo_kanban_task`) |
| 8 | Status transition: `running` → `failed` (or fast phase skips straight `(None\|ready\|blocked)` → `failed`) | phase in flight or not-yet-observed | phase failed | `N/A (premise corrected)` — see [#16](https://github.com/hyonchoi/todo-pipeline-orch/issues/16). Originally proposed routing this through `circuit.py::CircuitBreaker.observe` / `observe_from_outcomes`; that premise was wrong. `CircuitBreaker` is cross-tick, disk-persisted, and Slack-alerting — it protects the pipeline-watch loop across many ticks over time. The harness's `ConvergenceDetector`/`_ConvergenceMonitor` is in-memory, per-run, phase-granularity, and side-effect-free — it protects one cycle's poll loop. These solve different problems and must not be merged; the harness-local detector is the correct production path for this transition. | `phase_failed` event emitted with classified `error_class`; failure observation recorded via the harness-local `ConvergenceDetector`, which is correct as-is | N/A |
| 9 | N consecutive same-class failures reach `CircuitBreaker`'s threshold | breaker below threshold | breaker trips, cycle halts | `N/A (premise corrected)` — see [#16](https://github.com/hyonchoi/todo-pipeline-orch/issues/16). Originally proposed routing this through `circuit.py::CircuitBreaker`'s halt/trip logic; that premise was wrong for the same reason as row 8: `CircuitBreaker` is cross-tick/disk-persisted/Slack-alerting, while the harness-local `_ConvergenceMonitor.__call__` raising `ConvergenceHaltError` when `ConvergenceDetector.should_halt()` returns true is in-memory/per-run/side-effect-free. Bypassing `circuit.py` here is correct, not a gap. | Cycle halts (poll loop exits, `all_terminal=True`) only after the harness-local `ConvergenceDetector`, not `circuit.py`'s breaker, reports the halt condition met | N/A |
| 10 | All phases reach a terminal status (`done`/`failed`, cross-ref state-machine row on completion) | poll loop running | poll loop exits | (loop-internal — `TERMINAL_STATUSES` check against `kanban_tasks.get_todo_kanban_status`'s return, no separate production fn) | `all(s in TERMINAL_STATUSES for s in status_map.values())` is true; poll loop's `while not all_terminal` exits | `tests/test_harness.py::TestPollKanbanPhases::test_registers_phases_and_polls_to_completion` |
| 11 | Poll loop exits (terminal or convergence-halted) | final status known | outcomes written to decision store | `kanban_tasks.observe_outcomes` | `.hermes/outcomes/<tick_id>-phases.json` contains one line per newly-done/failed/archived phase (`phase_complete`/`failed_at_phase_<key>` outcomes), high-watermarked against prior entries; `all_phases_complete` sentinel written once all phases in `COMPLETION_STATUSES` | `tests/test_kanban_tasks.py::TestObserveOutcomes::test_writes_phase_complete_outcomes`, `tests/test_harness.py::TestPollKanbanPhases::test_convergence_halt_stops_polling` |
| 12 | Contract lookup at poll start | — | assignee resolved | `contract.load_contract` | `register_todo_phases`'s `--assignee` arg matches `PipelineContract.assignee` from `.hermes/pipeline.toml`, not a hardcoded `"default"` unless load genuinely fails (fallback path, logged as warning) | `tests/test_harness.py::TestPollKanbanPhases::test_assignee_resolved_from_contract`, `::test_assignee_defaults_when_contract_load_fails` |
| 13 | Overall cycle timeout (`--timeout` seconds elapse with poll loop still running) | poll loop running on worker thread | timeout surfaced, in-flight phase reported | *(harness-internal `_run_with_timeout` thread join — no production fn substitutes for a wrapper-level worker-thread timeout, out of scope per Constraints)* | `phase_timed_out` event emitted for the phase that was in-flight at timeout, if any | `TODO(TODO-21)` |

---

## Decisions

**Row 7 — `_auto_complete_gate_tasks` predecessor/eligibility logic placement (TODO-24 follow-up):**
Stays in `harness.py`. This logic only exists to compensate for the kanban
board not propagating unblock signals in the harness's simulated environment
— it is a harness-fixture workaround, not a pipeline-watch production
concern, so `phases.py` should not gain harness-specific compensation logic.
The completion *mechanic* (the actual status flip) already correctly
delegates to `kanban_tasks.complete_todo_kanban_task` per #19 — that part is
unchanged and unaffected by this decision.

---

## Not covered by this checklist (see design doc "NOT in scope")

- Multi-project scan coverage
- `tick.py::TickLock.acquire` — the kanban-as-scheduler path as implemented in
  `harness.py` does not itself acquire a tick lock (that's a pipeline-watch
  tick-runner concern, upstream of what the harness fixture drives). If TODO-21
  changes this, add a row here.
- Multi-project scan, Slack alert delivery, preflight startup checks.
- `_dispatch_phase` / `phases.run` / marker lifecycle (`.hermes/phase_started/`)
  — these only fire in the non-kanban path, out of scope (see design doc
  Constraints correction).
