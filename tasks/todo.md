# Tasks: TODO-24 / Issue #21 — Close remaining harness checklist rows

- [ ] Task: Verify candidate test links for rows 1, 2, 3, 4, 10, 11 actually
      assert the stated condition (read test bodies, not just names)
  - Acceptance: each candidate test from SPEC.md's row mapping is confirmed
    (or replaced) as a correct link; any mismatch is flagged and resolved
  - Verify: `uv run pytest tests/test_kanban_tasks.py tests/test_harness.py -v`
    passes for all candidate tests
  - Files: none (read-only verification)

- [ ] Task: Confirm row 12 (contract/assignee resolution) genuinely has no
      existing test coverage
  - Acceptance: grep/read confirms no test in `tests/test_harness.py` or
    `tests/test_kanban_tasks.py` asserts `register_todo_phases`'s `assignee`
    kwarg is sourced from `contract.load_contract`
  - Verify: `grep -n "load_contract\|assignee" tests/test_harness.py tests/test_kanban_tasks.py`
  - Files: none (read-only verification)

- [ ] Task: Write spy test(s) for row 12 assignee resolution
  - Acceptance: new test(s) in `tests/test_harness.py` assert (a) assignee
    comes from `contract.load_contract(state_dir).assignee` when it
    succeeds, and (b) falls back to `"default"` when `load_contract` raises
  - Verify: `uv run pytest tests/test_harness.py -v -k assignee` passes
  - Files: `tests/test_harness.py`

- [ ] Task: Update checklist doc Test link column for rows 1, 2, 3, 4, 10,
      11, 12
  - Acceptance: each row's Test link cell has the real `tests/...::path`
    (or paths) confirmed in the prior tasks, replacing `TODO(TODO-21)`
  - Verify: `grep -n "TODO(TODO-21)" docs/checklist-harness-production-coverage.md`
    only matches rows outside {1,2,3,4,10,11,12} (if any remain)
  - Files: `docs/checklist-harness-production-coverage.md`

- [ ] Task: Record gate-logic-placement decision in checklist doc
  - Acceptance: a note near row 7 (or a new "Decisions" subsection) states
    `_auto_complete_gate_tasks`'s predecessor/eligibility logic stays in
    `harness.py`, with the harness-fixture-workaround rationale from SPEC.md
  - Verify: manual read
  - Files: `docs/checklist-harness-production-coverage.md`

- [ ] Task: Update TODOS.md TODO-24 entry and close issue #21
  - Acceptance: TODOS.md TODO-24 marked `[x]` with a `Resolved design`/
    completion note pointing at the updated checklist doc; issue #21 closed
    via `gh issue close 21` with a summary comment
  - Verify: `grep -n "TODO-24" TODOS.md` shows `[x]`; `gh issue view 21` shows
    `CLOSED`
  - Files: `TODOS.md`

- [ ] Task: Full regression check
  - Acceptance: full harness/kanban test suite passes with no regressions
  - Verify: `uv run pytest tests/test_harness.py tests/test_kanban_tasks.py`
  - Files: none
