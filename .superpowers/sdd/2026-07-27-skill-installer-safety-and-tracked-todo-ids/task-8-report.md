# Task 8 Report: Final Contract Audit and Verification

## Status

Complete. The current documentation, mock fixture, and recovery docstring now
describe `NEXT_TODO_ID` as the common-path ID source. Scanning active and
archived TODO IDs is documented only for reconciliation or legacy recovery.

## Commits

- `7fc77f5 fix(TODO-38): align tracked TODO metadata contracts`
- `f49efea docs(TODO-38): update tracked TODO ID references`

## Tests

- `uv run pytest tests/test_skills_install.py tests/test_counter.py tests/test_recover_counter_cli.py tests/skill-test-environment -v`: 120 passed in 0.21s.
- `uv run pytest`: 856 tests collected; command completed with exit status 0. RTK filtered the passing-summary output.
- `uv run ruff check .`: All checks passed.
- `uv run pytest tests/test_skills_install.py::test_todos_manager_skill_is_packaged_data -v`: 1 passed in 0.03s.
- `git diff --check`: passed before each commit and after the final audit.

## Files Changed

- `hermes_pipeline/counter.py`: clarified that preserving a higher cache value applies only to legacy scan recovery.
- `hermes_pipeline/harness.py`: emitted the exact `> - NEXT_TODO_ID: 2` fixture line and the tracked-ID contract.
- `README.md`, `docs/ARCHITECTURE.md`, and `docs/explanation-skill-test-harness-design.md`: removed current scan-first descriptions outside the initial four-file task list.
- `docs/reference-counter.md`: documented tracked recovery as `NEXT_TODO_ID - 1` and scan recovery as legacy fallback.
- `docs/howto-todos-manager.md`, `docs/tutorial-todos-manager.md`, and `docs/reference-skill-test-harness.md`: used the required tracked-ID and installer-replacement language.

## Self-Review Notes

- Confirmed the required preamble form is documented as `> - NEXT_TODO_ID: <n>`.
- Confirmed installer documentation says existing installs fail, replacement requires `--reinstall`, and removal uses `--yes`.
- Re-ran the Task 8 stale-contract search. Remaining matches are valid reconciliation/legacy explanations, unrelated `--force` interfaces, or historical planning records.
- No runtime behavior changed; the fixture and docstring changes align existing behavior and tests.

## Concerns

- `uv` emits a non-failing warning that the base checkout's `VIRTUAL_ENV` does not match this worktree's `.venv`; it ignored that variable and used the worktree environment successfully.

## Review Fixes

Addressed the Task 8 review's important findings:

- `docs/ARCHITECTURE.md` now distinguishes immutable assigned `TODO-<n>` IDs from the `NEXT_TODO_ID` counter, which advances after a successful add.
- `docs/howto-todos-manager.md` now consistently says that `--audit` can modify `TODOS.md` only to reconcile invalid tracked metadata.
- `docs/reference-skill-test-harness.md` now states that adding `TODO-8` advances `NEXT_TODO_ID` to `9`, matching `add_happy_path.yaml`.

### Focused Verification

- `uv run pytest tests/skill-test-environment -v`: 67 passed in 0.12s.
- `uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py::TestTrackedNextTodoId::test_assign_next_todo_id_repairs_conflict_before_returning -v`: 1 passed in 0.15s; confirms assigning `TODO-8` writes `NEXT_TODO_ID: 9`.
- `git diff --check`: passed before commit.
