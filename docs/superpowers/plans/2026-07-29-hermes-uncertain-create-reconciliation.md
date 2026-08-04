# Hermes-Compatible Durable Registration Implementation Plan

> Final correction: the earlier `--initial-status blocked`/promotion design was
> invalidated by live Hermes 0.18.2 behavior. This plan records the implemented
> nonspawnable-barrier and child-first reconciliation contract.

**Goal:** Prevent partial or uncertain Hermes registration from dispatching any
phase, and reconcile every recorded task in dependency-safe order before a later
project tick proceeds.

**Architecture:** Create an unassigned registration barrier, parent executable
phases to it, sticky-block detached unassigned gates, persist the expected-phase
sentinel, then complete the barrier as the release commit point. Keep atomic
pending-create, cleanup-only, or barrier-commit-pending state until cleanup or
the registration commit is confirmed.

**Tech Stack:** Python 3.12+, `pathlib`, JSON state files, Hermes Kanban CLI,
pytest, pytest-mock, uv, Ruff, Markdown.

## Verified Hermes Contract

- A parentless task created with `--initial-status blocked` recomputes to
  `ready`; executable safety cannot rely on that flag.
- A `ready` task assigned to `-` is `skipped_nonspawnable`.
- Its child remains `todo` until the parent is completed.
- Completing the parent releases the child to `ready`.
- Archiving a parent can also release a child, so cleanup is child-first.
- Archive output and exit status are insufficient for idempotency; a snapshot
  including archived tasks is the source of truth.

## Global Constraints

- Use `rtk env -u VIRTUAL_ENV uv run --locked` for Python verification.
- Prefix shell searches, reads, tests, linters, and builds with `rtk`.
- Preserve prompt-client selection, prompt rendering, profiles, models,
  assignees for executable phases, authentication, and public configuration.
- Gate tasks remain detached, non-goal, and assigned to `-`.
- Write marker state atomically through
  `hermes_pipeline.state._atomic_write_text`.
- Do not bump `VERSION`; `/ship` owns the release version decision.

## File Map

- `hermes_pipeline/kanban_tasks.py`: barrier creation, durable task creation,
  sticky gates, sentinel/commit ordering, ordered cleanup, and snapshot truth.
- `hermes_pipeline/cli.py`: deferred reconciliation gate before prior-tick
  handling and selection.
- `tests/test_kanban_registration_barrier.py`: required barrier and cleanup
  regressions.
- `tests/test_hermes_registration_contract.py`: isolated live Hermes contract.
- `tests/test_kanban_tasks.py`: marker, recovery, snapshot, and compatibility
  coverage.
- `tests/test_tick_contract.py`: later-tick fail-closed coverage.
- `docs/reference-kanban-as-scheduler.md`: operator and API reference.
- `docs/superpowers/2026-07-29-hermes-uncertain-create-reconciliation-design.md`:
  final design rationale.

---

## Task 1: Nonspawnable registration barrier

**Interfaces:**

- Produces a barrier with phase key `__registration_barrier__`.
- Consumes `PreparedPhaseTask[]`.
- Preserves `create_prepared_todo_phases(...) -> list[str]`, returning phase
  IDs only.

- [x] Add failing tests for the barrier command, first executable parent,
  executable predecessor chain, and absence of `--initial-status`.
- [x] Add failing tests for sentinel-before-barrier-completion ordering.
- [x] Create the barrier before phases with:
  - `--assignee -`
  - no `--goal`
  - no `--parent`
  - no `--initial-status`
  - stable tick-derived idempotency key
  - an infrastructure JSON header and explanatory body.
- [x] Parent phase 1 to the barrier and later executable phases to their
  executable predecessor.
- [x] Persist `expected-phases.json`, replace cleanup state with
  barrier-commit-pending state, and complete the barrier as the commit point.

## Task 2: Sticky unassigned gates

- [x] Add failing coverage that a gate is unassigned, detached, non-goal, and
  does not use `--initial-status`.
- [x] After the gate ID is known, run:

  ```text
  hermes kanban block --kind needs_input <gate-id>
  ```

- [x] Treat block failure as registration failure and use the durable ordered
  cleanup state.

The brief `ready` window before `block` is safe because `assignee=-` is
nonspawnable.

## Task 3: Ordered durable cleanup

**Durable states:**

```text
pending create:
  tenant, tick_id, phase_key, known_task_ids (creation order)

cleanup only:
  tenant, tick_id, cleanup_task_ids (child-first order)

barrier commit pending:
  tenant, tick_id, barrier_task_id, cleanup_task_ids (child-first order)
```

- [x] Persist pending-create state before each remote create.
- [x] After a validated ID, persist cleanup-only state in reverse creation
  order, leaving the barrier last.
- [x] When the current create is uncertain and invisible, retain pending-create
  state and archive no known parent.
- [x] When the current child resolves, atomically convert to cleanup-only state
  ordered as current child, earlier phases in reverse creation order, barrier.
- [x] On a local `OSError`, omit the nonexistent current create and convert
  known IDs directly to cleanup-only state.
- [x] Query `hermes kanban list --archived --json` before and after archive
  commands. Count an already-archived task as success.
- [x] Clear the marker only after every recorded ID is confirmed archived.
- [x] After the expected-phase sentinel is durable, atomically replace cleanup
  state with barrier-commit-pending state before completing the barrier.
- [x] Reconciliation clears a snapshot-confirmed `done` barrier, retries
  completion from `ready` or `todo`, and fails closed for uncertain status.

## Task 4: Fail closed before later ticks

- [x] Reconcile the marker before reading the prior tick.
- [x] Skip prior-tick handling, selection, and new registration when the marker
  is malformed, unresolved, not fully archived, or cannot be removed.
- [x] Retain the existing project-level warning:

  ```text
  project <slug>: unresolved Hermes task creation; skipping
  ```

## Task 5: Verification

Run focused tests first:

```bash
rtk env -u VIRTUAL_ENV uv run --locked pytest -q \
  tests/test_kanban_registration_barrier.py \
  tests/test_kanban_tasks.py \
  tests/test_tick_contract.py
```

Run the live contract test:

```bash
rtk env -u VIRTUAL_ENV uv run --locked pytest -q \
  tests/test_hermes_registration_contract.py -vv
```

The live test uses an isolated temporary `HERMES_HOME`. It may skip only when
the `hermes` executable is absent, and the skip reason must say so explicitly.

Run final repository verification:

```bash
rtk env -u VIRTUAL_ENV uv run --locked pytest -q
rtk env -u VIRTUAL_ENV uv run --locked ruff check .
rtk env -u VIRTUAL_ENV uv run --locked pytest -q tests/test_docs_links.py
rtk env -u VIRTUAL_ENV uv build
rtk git diff --check
```

No release version is selected or changed in this plan. Qualification evidence
is a source-snapshot candidate until `/ship` selects the final version and
finalizes a versioned artifact in the release commit.
