# Spec: TODO-24 / Issue #21 — Close remaining harness checklist rows

## Objective

Close out `docs/checklist-harness-production-coverage.md` rows 1, 2, 3, 4, 10, 11, 12
by giving each a real "Test link" (existing test or a new spy test), and record a
decision on where `_auto_complete_gate_tasks`'s predecessor/eligibility logic should
live.

This is bookkeeping + one small test addition, not a refactor. A re-read of
`harness.py` after #19 merged shows the production-function wiring for these rows
already exists; what's missing is (a) the checklist doc still says
`TODO(TODO-21)` for rows that are in fact covered, and (b) row 12
(contract/assignee resolution) has production wiring but no dedicated test.

Success: every row 1-13 in the checklist has a correct test link (or an explicit
N/A with rationale, as rows 5/8/9/13 already have), and the gate-logic placement
question is closed with a documented rationale — no more open follow-ups from
TODO-21/TODO-24.

## Decisions (pre-resolved, not open questions)

- **Gate-logic placement:** `_auto_complete_gate_tasks`'s predecessor-map
  construction and eligibility filtering **stay in `harness.py`**. This logic
  exists only to compensate for the kanban board not propagating unblock
  signals in the harness's simulated environment — it is a harness-fixture
  workaround, not a pipeline-watch production concern. `phases.py` should not
  gain harness-specific compensation logic. The completion *mechanic* itself
  (the actual status flip) already correctly delegates to
  `kanban_tasks.complete_todo_kanban_task` per #19 — that part is unchanged.
- **Row 12 test:** write a new spy test. Assert `register_todo_phases`'s
  `--assignee` argument is populated from
  `contract.load_contract(state_dir).assignee`, plus the fallback-to-`"default"`
  path when `load_contract` raises (already implemented in
  `_poll_kanban_phases`, lines ~284-288 of `hermes_pipeline/harness.py`).

## Scope

In scope:
- `docs/checklist-harness-production-coverage.md` — update Test link column,
  rows 1, 2, 3, 4, 10, 11, 12.
- `tests/test_harness.py` — add one new test class/test for row 12
  (contract/assignee resolution), if no equivalent exists after verification.
- Confirm (do not re-derive) that rows 1, 2, 3, 4, 10, 11 already have adequate
  test links from `tests/test_kanban_tasks.py` / `tests/test_harness.py`; link
  them by exact test path.

Out of scope:
- Any change to `harness.py` behavior beyond what's needed to make row 12
  testable (expect none — the assignee-resolution code path already exists).
- Rows 5, 6, 7, 8, 9, 13 — already correctly marked (7 has a real link, 5/8/9/13
  are N/A with rationale, 6 has no independent production fn per the doc).
- Moving any logic into `phases.py` (see Decisions above — explicitly rejected).

## Row-by-row mapping (to verify and link)

| # | Transition | Candidate test | Action |
|---|---|---|---|
| 1 | `register_todo_phases` creates tasks w/ parent chain, gate BLOCKED | `tests/test_kanban_tasks.py::TestRegisterTodoPhases::test_creates_tasks_with_parent_chain`, `::test_gate_phase_registered_blocked_without_goal` | Link both |
| 2 | `_persist_expected_phases` (internal) | `tests/test_kanban_tasks.py::TestPersistExpectedPhases::test_writes_to_project_hermes_dir` | Link |
| 3 | `_archive_tasks` on mid-registration failure | `tests/test_kanban_tasks.py::TestRegisterTodoPhases::test_mid_registration_failure_archives_created_tasks` | Link |
| 4 | `get_todo_kanban_status` snapshot | `tests/test_kanban_tasks.py::TestGetTodoKanbanStatus::test_returns_status_map`, `::test_returns_empty_for_no_matching_tick` | Link both |
| 10 | Terminal-status loop exit | `tests/test_harness.py::TestPollKanbanPhases::test_registers_phases_and_polls_to_completion` (asserts `result is True` after both phases hit `done`) | Link |
| 11 | `observe_outcomes` on loop exit | `tests/test_kanban_tasks.py::TestObserveOutcomes::test_writes_phase_complete_outcomes` + `tests/test_harness.py::TestPollKanbanPhases::test_convergence_halt_stops_polling` (asserts `observe_outcomes` called once even on halt) | Link both |
| 12 | `contract.load_contract` → assignee | **none found** | Write new test |

Verify each candidate still passes and actually asserts the row's stated
condition before linking it — don't link on file/name match alone.

## Commands

```
Test:  uv run pytest tests/test_harness.py tests/test_kanban_tasks.py -v
Lint:  uv run ruff check hermes_pipeline/ tests/
Full test suite: uv run pytest
```

## Project Structure

```
hermes_pipeline/harness.py            → harness under change (read-only for this task, except if row 12 needs it — expected: no change)
hermes_pipeline/kanban_tasks.py       → production functions rows reference
tests/test_harness.py                 → add row 12 test here, under a new or existing class
tests/test_kanban_tasks.py            → existing coverage for rows 1-4, 11
docs/checklist-harness-production-coverage.md → the doc being updated
```

## Code Style

New test follows the existing `mocker.patch(...)` spy style used throughout
`tests/test_harness.py`'s `TestPollKanbanPhases` class — patch
`hermes_pipeline.contract.load_contract`, call `_poll_kanban_phases`, assert
the mock for `register_todo_phases` received the expected `assignee` kwarg.

```python
def test_assignee_resolved_from_contract(self, tmp_path, mocker):
    from hermes_pipeline.harness import (
        _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
    )
    mock_register = mocker.patch(
        "hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"]
    )
    mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
    mocker.patch("time.sleep")
    mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
    mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                  return_value={"phase_2_autoplan": "done"})
    mock_contract = mocker.Mock(assignee="alice")
    mocker.patch("hermes_pipeline.contract.load_contract", return_value=mock_contract)

    events_log = tmp_path / "events.jsonl"
    monitor = _ConvergenceMonitor(HarnessMonitor(events_log), ConvergenceDetector(threshold=3), {})

    _poll_kanban_phases(
        project_slug="demo", tick_id="01TICK", state_dir=tmp_path / ".hermes",
        todo_id="TODO-1", project_dir=tmp_path, phases_path=None,
        monitor=monitor, detector=ConvergenceDetector(threshold=3), poll_interval=0.1,
    )

    assert mock_register.call_args.kwargs["assignee"] == "alice"
```

Plus a fallback-path test where `load_contract` raises and `assignee` defaults
to `"default"` (mirroring the existing `log.warning` fallback at
`harness.py:287-288`).

## Testing Strategy

- Framework: pytest + `pytest-mock` (`mocker` fixture), consistent with the rest
  of `tests/test_harness.py`.
- Level: unit/spy tests on `_poll_kanban_phases`, not integration — matches
  existing `TestPollKanbanPhases` conventions.
- No coverage threshold beyond: every checklist row 1-13 has a linked,
  passing, assertion-verified test or documented N/A.

## Boundaries

- **Always:** run `uv run pytest tests/test_harness.py tests/test_kanban_tasks.py`
  before considering a row "linked"; verify the linked test's assertions
  actually match the row's stated condition, not just naming similarity.
- **Ask first:** any change to `harness.py` or `kanban_tasks.py` production
  code beyond adding the row-12 test (none currently expected).
- **Never:** move `_auto_complete_gate_tasks` logic into `phases.py` (decided
  against, see Decisions); mark a row "done" in the checklist without a
  verified test link or explicit N/A rationale.

## Success Criteria

- [ ] `docs/checklist-harness-production-coverage.md` rows 1, 2, 3, 4, 10, 11, 12
      each have a real test link, no `TODO(TODO-21)` remaining except where
      legitimately still open.
- [ ] New spy test(s) for row 12 exist in `tests/test_harness.py`, pass, and
      assert `register_todo_phases`'s `assignee` kwarg is sourced from
      `contract.load_contract(...).assignee` with fallback to `"default"`.
- [ ] Decision on `_auto_complete_gate_tasks` logic placement is recorded in
      the checklist doc (row 7 note) and/or TODOS.md: stays in harness.py,
      with rationale.
- [ ] `uv run pytest tests/test_harness.py tests/test_kanban_tasks.py` passes.
- [ ] Issue #21 acceptance criteria satisfied; TODOS.md TODO-24 entry updated/closed.

## Open Questions

None — both prior open questions (gate-logic placement, row-12 test) were
resolved above before writing this spec.
