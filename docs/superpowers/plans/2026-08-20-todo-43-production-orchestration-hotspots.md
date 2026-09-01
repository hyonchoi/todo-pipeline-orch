# TODO-43 Production Orchestration Hotspot Refactor

## Goal

Refactor the oversized production orchestration functions into cohesive,
private, typed boundaries without changing public behavior, CLI contracts,
subprocess arguments, cancellation and recovery semantics, durable-state
ordering, logs, outcomes, or workspace retention.

Deliver one pull request with six sequential red-green-refactor tasks. Keep the
new boundaries in their existing modules. Do not add dependencies, migrations,
configuration, provider behavior, or fixes for the separate TODO-23 defects.

## Global constraints

- Preserve the signatures of `cli._tick_project`,
  `harness._poll_kanban_phases`, `harness.run_harness`, and
  `kanban_tasks.create_prepared_todo_phases`.
- Preserve patch seams for `hermes_pipeline.cli.run_selection`,
  `_persist_tick_id`, `_make_circuit_breaker`, source-module Kanban functions,
  `run_registration.register_pinned_run`, harness polling, reporting, and
  status functions.
- Keep unknown-status hangs, gate-only behavior, thread/join races, and gate
  flapping unchanged because TODO-23 owns those defects.
- Implement in one dedicated linked worktree. Complete each task through a
  failing focused test, the smallest passing change, refactoring, independent
  review, and one atomic commit before beginning the next task.
- Use provider-free tests as the implementation authority. Do not claim live
  Hermes validation unless it is separately run and reported.

## Task 01: Extract durable Kanban registration boundaries

**Red:** Add direct tests importing `_RegistrationTaskResult`,
`_create_registration_barrier`, `_create_prepared_phase_task`, and
`_commit_prepared_phase_chain`. Run the focused command and confirm collection
fails because the boundaries do not exist.

**Green:** Add the frozen result dataclass and private same-module helpers.
Leave `_run_durable_task_create` as the sole remote-create and durable-recovery
boundary. Reduce `create_prepared_todo_phases` to ordered coordination and keep
its returned phase IDs free of the barrier ID.

**Acceptance criteria:**

- Barrier, worker, and gate Hermes commands keep identical argument order and
  values.
- The exact operation order remains barrier durable create; each phase durable
  create; post-create cancellation; gate block; pre-commit cancellation;
  expected-phase persistence; pending barrier-commit persistence; cancellation;
  barrier completion; pending-state clear.
- Cancellation and partial failure retain child-first durable cleanup.
- Existing registration-barrier and uncertain-create tests remain green.

**Verification:**
`rtk uv run pytest tests/test_kanban_registration_barrier.py tests/test_kanban_tasks.py -q`

**Commit:** `Refactor durable Kanban registration boundaries`

## Task 02: Extract the harness poll transition model

**Red:** Add direct table tests importing `_PhaseTransition`,
`_classify_phase_transition`, `_is_terminal_phase_status`, and
`_apply_phase_transition`. Run the focused command and confirm collection fails
because the boundaries do not exist.

**Green:** Add a frozen transition result, a pure classifier, a terminal-status
helper, and a side-effect application helper. Keep registration, polling,
cancellation waits, backoff state, status retrieval, terminal looping, final
outcome observation, and return calculation in `_poll_kanban_phases`.

**Acceptance criteria:**

- The initial snapshot remains log-only and `previous_status` starts empty.
- Unknown statuses emit no transition and remain nonterminal.
- Unexpected extra phase entries still participate in terminal and final
  success checks.
- Cancellation waits, 1.5x bounded backoff, reset-on-change, convergence halt,
  final observation, and gate-completion timing remain unchanged.
- Gate-only, unknown-status hang, thread-race, and flapping behavior tracked by
  TODO-23 are not altered.

**Verification:** `rtk uv run pytest tests/test_harness.py -q`

**Commit:** `Extract harness phase transition model`

## Task 03: Extract the harness execution lifecycle

**Red:** Add direct tests importing `_HarnessProfileSelection`,
`_resolve_harness_profile`, `_HarnessPollResult`, `_run_harness_poll`, and
`_finalize_harness_report`. Run the focused command and confirm collection
fails because the boundaries do not exist.

**Green:** Add frozen profile and poll results plus private helpers for profile
resolution, poll execution with cleanup, and report assembly. `run_harness`
retains workspace allocation, `isolate_config` ownership, its public signature,
`HarnessResult` construction, and sole final workspace cleanup.

**Acceptance criteria:**

- `registration_event` is set before phase registration.
- Pre-registration failure performs no remote cleanup.
- Post-registration failure with confirmed cleanup re-raises the original
  exception and permits workspace removal.
- Unconfirmed cleanup raises sanitized `HarnessCleanupError` and retains the
  workspace.
- `PollCancellationError` starts no remote cleanup while the worker remains
  active and retains the workspace.
- Timeout inspection, cleanup confirmation, `phase_timed_out`, reporting, exit
  behavior, `keep_dir`, pruning, loop snapshots, printed output, and
  `HarnessResult` fields remain unchanged.

**Verification:** `rtk uv run pytest tests/test_harness.py -q`

**Commit:** `Refactor harness execution lifecycle`

## Task 04: Extract tick profile and prior-run reconciliation

**Red:** Add direct tests importing `_ResolvedTickProfile`,
`_resolve_tick_profile`, and `_reconcile_prior_tick`. Run the focused command
and confirm collection fails because the boundaries do not exist.

**Green:** Add the frozen profile result and private profile/prior-run helpers.
This task owns only contract, profile, and prerequisite resolution plus
prior-tick reconciliation. `_tick_project` retains capability validation,
pending-create reconciliation before the prerequisite gate, Slack and circuit
setup, and a boolean continue-or-stop delegation for prior-tick work.

**Acceptance criteria:**

- Missing-contract fallback, capability checks, and prerequisite failures keep
  their messages and failure behavior.
- Pending task-create reconciliation still precedes prerequisite validation and
  every prior-run check.
- Legacy ship handling, Plan-result reconciliation, review reconciliation,
  TODO completion, phase completion, PR handoff, and outcome observation keep
  their exact order.
- Every early return and fail-closed circuit observation remains unchanged.
- Task 05 selection and registration code is not moved in this commit.

**Verification:**
`rtk uv run pytest tests/test_tick_contract.py tests/test_tick_subcommand.py tests/test_tick_subcommand_edge.py -q`

**Commit:** `Extract tick prior-run reconciliation`

## Task 05: Extract tick selection and spawn boundaries

**Red:** Add direct tests importing `_TickSelection`,
`_select_todo_for_tick`, `_PreparedSelectedRun`, `_prepare_selected_run`, and
`_register_prepared_run`. Run the focused command and confirm collection fails
because the boundaries do not exist.

**Green:** Add frozen selection and prepared-run results plus private helpers
covering context and configuration construction, bounded selection,
no-candidate handling, picked-none persistence, Plan resolution, prompt
preparation, pinned-run registration, tick persistence, remote Kanban creation,
and final circuit observation. Keep `_tick_project` as coordinator and preserve
all existing module patch seams.

**Acceptance criteria:**

- No-candidate and picked-none paths preserve circuit observation and
  sentinel-before-tick-ID ordering.
- Selected work keeps the order prepare prompts; register pinned run; persist
  tick; create Hermes tasks; record circuit success.
- Plan, prompt, pinned-run, and Kanban failures preserve their outcome reason,
  sanitized logging, handled return, or `RuntimeError` propagation.
- Pinned worktree selection and assignee propagation remain unchanged.
- The Task 04 profile and prior-run boundary is not reopened.

**Verification:**
`rtk uv run pytest tests/test_tick_contract.py tests/test_tick_subcommand.py tests/test_tick_subcommand_edge.py -q`

**Commit:** `Extract tick selection and spawn boundaries`

## Task 06: Record empty release intent

**Red:** Run
`rtk uv run python scripts/release_changesets.py status --since origin/main`
and confirm it fails because the pull request has no changeset fragment.

**Green:** Run
`rtk uv run python scripts/release_changesets.py add --empty` and retain only
the generated empty fragment.

**Acceptance criteria:**

- The pull request contains exactly one task-owned empty changeset.
- Release status and consistency checks pass.
- README, `docs/ARCHITECTURE.md`, migration guidance, compatibility notes, and
  release metadata are audited. Do not publish private helper names in user
  documentation when the stable orchestration contract remains unchanged.
- No unrelated generated or temporary files are included.

**Verification:**

- `rtk uv run python scripts/release_changesets.py status --since origin/main`
- `rtk uv run python scripts/release_changesets.py check`
- `rtk git diff --check`

**Commit:** `Add empty changeset for orchestration refactor`

## Final verification and rollback

After the six independently reviewed task commits, inspect the complete
merge-base-to-HEAD diff plus staged, unstaged, and untracked state. Run:

- `rtk uv run pytest`
- `rtk uv run ruff check .`
- `rtk uv run python scripts/release_changesets.py check`
- `rtk uv run python scripts/release_changesets.py status --since origin/main`
- `rtk git diff --check`

Obtain one independent whole-change review. Every actionable P0-P3 finding
blocks completion. Roll back by reverting task commits in reverse order. No
migration, dependency, infrastructure, or external-service rollback is needed.

```json tpo-plan
{
  "schema_version": 1,
  "todo_id": "TODO-68",
  "tasks": [
    {
      "id": "task-01",
      "title": "Extract durable Kanban registration boundaries",
      "instructions": "Add direct failing tests for the new private registration helpers, then extract a frozen registration result, barrier creation, phase creation, and chain-commit helpers in kanban_tasks.py. Preserve _run_durable_task_create as the remote mutation and durable recovery boundary, exact Hermes arguments, cancellation points, cleanup ordering, barrier commit ordering, and the public coordinator contract.",
      "acceptance_criteria": [
        "Barrier, worker, and gate Hermes commands preserve exact argument order and values.",
        "Phase task IDs remain ordered and exclude the registration barrier.",
        "Cancellation and partial failures retain child-first durable cleanup and barrier recovery semantics.",
        "Existing registration and uncertain-create characterization tests remain green."
      ],
      "verification": [
        "rtk uv run pytest tests/test_kanban_registration_barrier.py tests/test_kanban_tasks.py -q"
      ],
      "commit_message": "Refactor durable Kanban registration boundaries"
    },
    {
      "id": "task-02",
      "title": "Extract the harness poll transition model",
      "instructions": "Add direct failing table tests for a frozen phase transition result, pure transition classifier, terminal-status helper, and transition application helper, then extract them within harness.py. Keep registration, polling, cancellation waits, backoff, status retrieval, terminal looping, final observation, and return calculation in _poll_kanban_phases, without fixing TODO-23 behavior.",
      "acceptance_criteria": [
        "The initial snapshot remains log-only and previous_status starts empty.",
        "Unknown statuses emit no event and remain nonterminal, while unexpected extra phases still affect terminal and success checks.",
        "Cancellation waits, bounded backoff, reset-on-change, convergence halt, final observation, and gate completion timing remain unchanged.",
        "Known TODO-23 gate, hang, race, and flapping defects are not altered."
      ],
      "verification": [
        "rtk uv run pytest tests/test_harness.py -q"
      ],
      "commit_message": "Extract harness phase transition model"
    },
    {
      "id": "task-03",
      "title": "Extract the harness execution lifecycle",
      "instructions": "Add direct failing tests for private typed profile resolution, poll execution, and report finalization boundaries, then extract those helpers in harness.py. Keep run_harness responsible for workspace allocation, isolate_config, its public signature, HarnessResult construction, and final workspace cleanup while preserving the complete cleanup and retention matrix.",
      "acceptance_criteria": [
        "Registration is signaled before mutation and pre-registration failures perform no remote cleanup.",
        "Confirmed post-registration cleanup re-raises the original error, while unconfirmed cleanup raises sanitized HarnessCleanupError and retains the workspace.",
        "Poll cancellation starts no unsafe remote cleanup, and timeout cleanup and phase_timed_out behavior remain unchanged.",
        "keep_dir, pruning, loop snapshots, printed output, reporting, exit codes, and HarnessResult fields remain compatible."
      ],
      "verification": [
        "rtk uv run pytest tests/test_harness.py -q"
      ],
      "commit_message": "Refactor harness execution lifecycle"
    },
    {
      "id": "task-04",
      "title": "Extract tick profile and prior-run reconciliation",
      "instructions": "Add direct failing tests for a frozen resolved-profile result plus profile resolution and prior-run reconciliation helpers, then extract only those responsibilities in cli.py. Keep capability checks, pending-create reconciliation before prerequisites, Slack and circuit setup, legacy ship compatibility, downstream reconciliation order, early returns, and fail-closed observations unchanged.",
      "acceptance_criteria": [
        "Contract fallback, capability checks, and prerequisite failures preserve messages and behavior.",
        "Pending-create, Plan-result, review, TODO completion, phase completion, PR handoff, and outcome checks preserve their exact order.",
        "Early returns and circuit-breaker observations remain fail closed.",
        "Selection and registration remain untouched until task-05."
      ],
      "verification": [
        "rtk uv run pytest tests/test_tick_contract.py tests/test_tick_subcommand.py tests/test_tick_subcommand_edge.py -q"
      ],
      "commit_message": "Extract tick prior-run reconciliation"
    },
    {
      "id": "task-05",
      "title": "Extract tick selection and spawn boundaries",
      "instructions": "Add direct failing tests for frozen selection and prepared-run results plus selection, preparation, and registration helpers, then extract those boundaries in cli.py. Preserve all patch seams, no-candidate and picked-none handling, Plan validation, prompt preparation, pinned-run registration, persistence-before-Hermes ordering, registration failure behavior, and final circuit observation.",
      "acceptance_criteria": [
        "No-candidate and picked-none paths preserve circuit and sentinel-before-tick-ID behavior.",
        "Selected work preserves prepare, pin, persist, create, then circuit-success ordering.",
        "Plan, prompt, pinned-run, and Kanban failures preserve outcomes, sanitized logs, returns, and propagation.",
        "Pinned worktree and assignee propagation remain unchanged without reopening task-04."
      ],
      "verification": [
        "rtk uv run pytest tests/test_tick_contract.py tests/test_tick_subcommand.py tests/test_tick_subcommand_edge.py -q"
      ],
      "commit_message": "Extract tick selection and spawn boundaries"
    },
    {
      "id": "task-06",
      "title": "Record empty release intent",
      "instructions": "Establish the missing-changeset failure, generate one empty fragment with scripts/release_changesets.py, audit current documentation and compatibility surfaces, and keep only the task-owned release-intent artifact. Do not document private helper names when the stable user-visible orchestration contract remains unchanged.",
      "acceptance_criteria": [
        "Exactly one empty changeset fragment records the internal refactor.",
        "Release status, release consistency, and diff checks pass.",
        "Documentation, migration, compatibility, and release surfaces are audited with no unnecessary edits.",
        "No unrelated or temporary artifacts are included."
      ],
      "verification": [
        "rtk uv run python scripts/release_changesets.py status --since origin/main",
        "rtk uv run python scripts/release_changesets.py check",
        "rtk git diff --check"
      ],
      "commit_message": "Add empty changeset for orchestration refactor"
    }
  ]
}
```
