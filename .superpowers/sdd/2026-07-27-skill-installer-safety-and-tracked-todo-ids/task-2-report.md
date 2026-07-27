# Task 2 Report: Reinstall Replacement and Multi-Target Preflight Rollback

## Status

Complete. Task 2 implementation is committed.

## Commits

- `9d76e41` - `feat(TODO-35): preflight skill reinstall targets`

## Tests

- Red phase: the new multi-target rollback test failed because the Claude target was replaced before the invalid Codex target was rejected.
- Focused Task 2 tests: `2 passed`.
- Installer test file: `18 passed`.
- Non-eval suite: `797 passed, 5 skipped, 1 failed`.
- Ruff: `All checks passed!`
- Diff check: passed.

## Files Changed

- `hermes_pipeline/cli.py`: added an all-target reinstall preflight that reports every invalid replacement and returns before any installation or deletion occurs.
- `tests/test_skills_install.py`: aligned the existing replacement test name with the brief and added multi-target rollback coverage.

## Self-Review Notes

- The preflight runs before the destructive installation loop when `--reinstall` is set.
- Existing Task 1 staged replacement and rollback behavior remains unchanged.
- The implementation uses `--reinstall` only; no `--force` alias was added.
- The new test verifies that an earlier valid target remains untouched when a later target cannot be replaced.
- `git diff --check` and Ruff passed.

## Concerns

- The non-eval suite has one unrelated failure in `tests/test_config_from_env.py::test_from_env_pipeline_projects_dir_compat_alias`: `Config.from_env()` returned `/Users/hyonchoi/projects` instead of the test temporary path. The failure reproduces in isolation and does not involve the changed files.
- `uv` reports that the inherited `VIRTUAL_ENV` points at the base checkout and ignores it in favor of this worktree's `.venv`; tests still execute successfully from the worktree environment.

## Review Fix Report

### Status

Complete. The Critical and Important review findings are addressed.

### Findings Addressed

- Critical: reinstall preflight now calls `_preflight_skill_replacement` for every selected target, including targets whose `todos-manager` destination does not yet exist. Missing install parents are therefore validated before any selected target is replaced.
- Important: added coverage for an existing Claude destination and a missing Codex destination whose install parent rejects creation. The test asserts Claude remains unchanged.
- Minor: retained the per-target helper call as a defensive race check and added a comment clarifying its purpose after the global preflight.

### Verification

- Red phase: the new missing-parent regression failed because Claude was replaced before the Codex parent failure was detected.
- Focused installer suite after the fix: `19 passed`.
- Ruff on changed source and tests: `All checks passed!`
- `git diff --check`: passed.

### Files Changed

- `hermes_pipeline/cli.py`
- `tests/test_skills_install.py`
- This report file.

### Remaining Concern

- The previously reported unrelated non-eval failure remains: `tests/test_config_from_env.py::test_from_env_pipeline_projects_dir_compat_alias` resolves `/Users/hyonchoi/projects` instead of the test temporary path under the ambient configuration. It is outside the amended files.
