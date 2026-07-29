# Hermes Uncertain Create Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an inconclusive Hermes create from leaving a runnable orphan task and reconcile every unresolved idempotency key before a later project tick proceeds.

**Architecture:** Register every executable phase as blocked, persist a per-project pending-create marker before each remote mutation, and clear it only after a validated task ID is known. Persist the full expected-phase sentinel before promoting the first executable task; on the next tick, reconcile any surviving marker by snapshot lookup and archive the late task before continuing.

**Tech Stack:** Python 3.12+, `pathlib`, JSON state files, Hermes Kanban CLI, pytest, pytest-mock, uv, Ruff.

## Global Constraints

- Use `uv run --locked` for Python verification.
- Prefix searches, file reads, tests, and linters with `rtk`.
- Preserve gate phases as blocked, parentless, non-executable markers.
- Do not change prompt-client selection, prompt rendering, profile metadata, or public configuration.
- Write state atomically through `hermes_pipeline.state._atomic_write_text`.

---

## File Map

- `hermes_pipeline/kanban_tasks.py`: owns pending-create state, immediate and deferred reconciliation, blocked registration, expected-phase persistence, and first-task activation.
- `hermes_pipeline/cli.py`: invokes deferred reconciliation before prior-tick handling and fails closed while the remote create remains unresolved.
- `tests/test_kanban_tasks.py`: covers marker lifecycle, late snapshot visibility, blocked task creation, sentinel failure, and activation ordering.
- `tests/test_tick_contract.py`: covers the project-level fail-closed reconciliation gate.
- `docs/reference-kanban-as-scheduler.md`: documents blocked-until-committed registration and deferred cleanup.

---

### Task 1: Durable pending-create reconciliation

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py:65-178`
- Test: `tests/test_kanban_tasks.py:242-425`

**Interfaces:**
- Produces: `PendingTaskCreate(tenant: str, tick_id: str, phase_key: str, known_task_ids: tuple[str, ...])`
- Produces: `reconcile_pending_task_create(project_dir: str | Path) -> bool`
- Consumes: `_find_task_id_in_snapshot(tenant=..., tick_id=..., phase_key=...) -> str | None`

- [ ] **Step 1: Write the failing late-visibility test**

Add a test that writes a pending marker, returns an empty snapshot on the first reconciliation, then returns `t_deadbeef` on the second. Assert the first call returns `False` and retains the marker; assert the second archives `t_deadbeef`, returns `True`, and removes the marker.

```python
def test_reconcile_pending_create_waits_for_late_visible_task(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import (
        PendingTaskCreate,
        _persist_pending_task_create,
        reconcile_pending_task_create,
    )

    _persist_pending_task_create(
        tmp_path,
        PendingTaskCreate("demo", "01CLIENT", "phase_1", ()),
    )
    find = mocker.patch(
        "hermes_pipeline.kanban_tasks._find_task_id_in_snapshot",
        side_effect=[None, "t_deadbeef"],
    )
    archive = mocker.patch("hermes_pipeline.kanban_tasks._archive_tasks")

    assert reconcile_pending_task_create(tmp_path) is False
    assert reconcile_pending_task_create(tmp_path) is True
    archive.assert_called_once_with(["t_deadbeef"])
    assert find.call_count == 2
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
rtk uv run --locked pytest tests/test_kanban_tasks.py::test_reconcile_pending_create_waits_for_late_visible_task -q
```

Expected: FAIL because the pending-create interfaces do not exist.

- [ ] **Step 3: Implement atomic marker persistence and reconciliation**

Add a frozen dataclass, JSON serialization validation, an atomic marker writer under `<project>/.hermes/outcomes/pending-task-create.json`, and a reconciler that:

```python
task_id = _find_task_id_in_snapshot(
    tenant=pending.tenant,
    tick_id=pending.tick_id,
    phase_key=pending.phase_key,
)
if task_id is None:
    return False
_archive_tasks([*pending.known_task_ids, task_id])
marker.unlink()
return True
```

Malformed or unreadable marker data must return `False` and remain in place so the project fails closed.

- [ ] **Step 4: Run focused reconciliation tests and verify GREEN**

Run:

```bash
rtk uv run --locked pytest tests/test_kanban_tasks.py -k "pending_create or late_visible" -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
rtk git commit -m "fix: persist uncertain Hermes task creation"
```

---

### Task 2: Block registration until the chain is durable

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py:217-414`
- Test: `tests/test_kanban_tasks.py:150-425`

**Interfaces:**
- Consumes: `_persist_pending_task_create(project_dir, pending) -> None`
- Produces: `_promote_task(task_id: str) -> None`
- Changes: `_persist_expected_phases(...) -> None` raises `RuntimeError` on write failure.

- [ ] **Step 1: Write failing ordering and cleanup tests**

Add tests asserting:

1. Every executable create command includes `--initial-status blocked`.
2. The pending marker is written before each `hermes kanban create`.
3. The expected-phase sentinel is persisted before `hermes kanban promote <first_id>`.
4. A sentinel write failure archives all known tasks and never promotes.
5. Two timed-out creates plus an empty snapshot leave the pending marker in place.

Use a shared event list in mocked persistence, subprocess, and promotion functions to assert:

```python
assert events == [
    "pending:phase_1",
    "create:phase_1",
    "clear:phase_1",
    "pending:phase_2",
    "create:phase_2",
    "clear:phase_2",
    "persist-expected",
    "promote:t_00000001",
]
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
rtk uv run --locked pytest tests/test_kanban_tasks.py -k "blocked_until_registered or activation_order or pending_marker" -q
```

Expected: FAIL because executable tasks are currently created runnable and no promotion step exists.

- [ ] **Step 3: Implement minimal blocked registration**

Before each create, persist:

```python
PendingTaskCreate(
    tenant=board_slug,
    tick_id=tick_id,
    phase_key=phase.phase_key,
    known_task_ids=tuple(task_ids),
)
```

Append `["--initial-status", BLOCKED]` for every task. Keep gate tasks parentless and without `--goal`; executable tasks retain their parent and goal arguments. After validating a task ID, clear only the matching pending marker.

After all creates:

```python
_persist_expected_phases(prepared, project_dir=project_dir)
first_executable = next(
    task_id for task_id, phase in zip(task_ids, prepared, strict=True)
    if not phase.gate
)
_promote_task(first_executable)
```

If sentinel persistence or promotion fails, archive all tasks and raise `RuntimeError`.

- [ ] **Step 4: Run the full kanban-task test module**

Run:

```bash
rtk uv run --locked pytest tests/test_kanban_tasks.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
rtk git commit -m "fix: activate Hermes tasks after durable registration"
```

---

### Task 3: Fail closed before later project ticks

**Files:**
- Modify: `hermes_pipeline/cli.py:1051-1063`
- Test: `tests/test_tick_contract.py`

**Interfaces:**
- Consumes: `reconcile_pending_task_create(project_dir) -> bool`
- Preserves: `_tick_project(...) -> None`

- [ ] **Step 1: Write the failing tick-gate tests**

Add one test where `reconcile_pending_task_create(project_dir)` returns `False`. Assert `_tick_project` returns before `run_selection`, `all_phases_complete`, or new Hermes creates.

Add one test where reconciliation returns `True`. Assert normal prior-tick handling proceeds.

```python
reconcile = mocker.patch(
    "hermes_pipeline.kanban_tasks.reconcile_pending_task_create",
    return_value=False,
)
run_selection = mocker.patch("hermes_pipeline.cli.run_selection")

_tick_project(...)

reconcile.assert_called_once_with(project_dir)
run_selection.assert_not_called()
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
rtk uv run --locked pytest tests/test_tick_contract.py -k "pending_task_create" -q
```

Expected: FAIL because `_tick_project` does not reconcile pending creation state.

- [ ] **Step 3: Add the project-level reconciliation gate**

Immediately before reading the prior tick:

```python
from .kanban_tasks import reconcile_pending_task_create

if not reconcile_pending_task_create(project_dir):
    log.warning(
        "project %s: unresolved Hermes task creation; skipping",
        project_slug,
    )
    return
```

The reconciler returns `True` when no marker exists, so projects without uncertain state keep their existing behavior.

- [ ] **Step 4: Run focused and integration tests**

Run:

```bash
rtk uv run --locked pytest tests/test_tick_contract.py tests/test_kanban_tasks.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
rtk git add hermes_pipeline/cli.py tests/test_tick_contract.py
rtk git commit -m "fix: reconcile uncertain tasks before ticking"
```

---

### Task 4: Document and verify the recovered lifecycle

**Files:**
- Modify: `docs/reference-kanban-as-scheduler.md`
- Modify: `docs/superpowers/2026-07-29-hermes-uncertain-create-reconciliation-design.md`
- Modify: `docs/superpowers/plans/2026-07-29-hermes-uncertain-create-reconciliation.md`

**Interfaces:**
- Documents: blocked create → durable chain → first-task promotion.
- Documents: pending marker → later snapshot → archive → marker removal.

- [ ] **Step 1: Update the scheduler reference**

Document the pending marker path, fail-closed tick behavior, blocked registration, sentinel persistence, promotion ordering, and operator-visible recovery log.

- [ ] **Step 2: Run documentation and metadata checks**

Run:

```bash
rtk uv run --locked pytest tests/test_docs_links.py tests/test_tick_contract.py tests/test_kanban_tasks.py -q
rtk uv run --locked ruff check .
```

Expected: PASS.

- [ ] **Step 3: Run complete fresh verification**

Run:

```bash
rtk uv run --locked pytest --cov=hermes_pipeline --cov-report=term-missing
rtk uv run --locked ruff check .
rtk uv build
```

Expected: all tests pass, Ruff reports no findings, and sdist/wheel build succeeds.

- [ ] **Step 4: Commit documentation**

```bash
rtk git add docs/reference-kanban-as-scheduler.md docs/superpowers/2026-07-29-hermes-uncertain-create-reconciliation-design.md docs/superpowers/plans/2026-07-29-hermes-uncertain-create-reconciliation.md
rtk git commit -m "docs: explain uncertain Hermes create recovery"
```

- [ ] **Step 5: Resume `/ship`**

Restart the ship workflow from tests and pre-landing review. If the review applies another code fix, stop and rerun `/ship` as required.
