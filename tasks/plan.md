# Plan: TODO-24 / Issue #21 — Close remaining harness checklist rows

Source: `SPEC.md`. This is a low-risk, mostly-documentation task with one new
test. No production code changes are expected.

## Components

1. **Verification pass** — confirm each candidate test in the spec's row
   mapping actually exists, passes, and asserts the row's stated condition
   (not just a name/file match).
2. **Row 12 test** — write the missing spy test(s) for contract/assignee
   resolution in `_poll_kanban_phases`.
3. **Checklist doc update** — fill in the Test link column for rows 1, 2, 3,
   4, 10, 11, 12; add the gate-logic-placement decision note near row 7.
4. **TODOS.md / issue close-out** — update the TODO-24 entry status and close
   issue #21 with a summary comment.

## Dependency order

Steps are sequential — each later step depends on the prior one's output:

1. Verification pass must happen first: it determines which rows are already
   linkable and confirms row 12 genuinely has no coverage (spec assumes this
   but must be checked against current `main`, not the state read during
   spec-writing).
2. Row 12 test only gets written if verification confirms the gap.
3. Checklist doc update depends on final test link locations (verification +
   new test paths).
4. TODOS.md/issue close-out is last — depends on the checklist doc being
   accurate.

Nothing here can run in parallel usefully; it's a short serial chain and
splitting it would just add coordination overhead.

## Verification checkpoints

- After step 1 (verification pass): confirm the row-to-test mapping table in
  SPEC.md is accurate. If any candidate test doesn't actually assert what the
  row claims, flag it — may need a different test or a new one (scope
  change, would require updating SPEC.md).
- After step 2 (row 12 test): `uv run pytest tests/test_harness.py -v -k assignee` passes.
- After step 3 (doc update): re-read `docs/checklist-harness-production-coverage.md`
  and confirm no row among 1-4, 10-12 still says `TODO(TODO-21)`.
- Final: `uv run pytest tests/test_harness.py tests/test_kanban_tasks.py` passes
  in full.

## Risks

- **Candidate test doesn't actually assert the row's condition.** Mitigation:
  read each candidate test's body (not just its name) during step 1 before
  linking it.
- **Row 12 fallback path (`load_contract` raising → `assignee="default"`) is
  hard to trigger via mocker.** Mitigation: patch
  `hermes_pipeline.contract.load_contract` with `side_effect=Exception(...)`
  — same pattern already used for `get_todo_kanban_status` failures elsewhere
  in the file.
- **Scope creep into refactoring `_auto_complete_gate_tasks`.** Explicitly
  out of scope per spec Decisions — resist during implementation.

## Out of scope reminder

No changes to `harness.py` or `kanban_tasks.py` production logic. If step 1
verification surfaces a genuine gap beyond row 12, stop and revisit the spec
rather than expanding scope silently.
