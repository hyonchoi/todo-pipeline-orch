# Issue 103 recovery evidence — 2026-09-10

Recovery remains blocked; no existing card, registration, Plan, branch, or worktree was changed. No external implementation, provider call, push, merge, or PR operation ran.

## Registered identity and current Git evidence

- Registration: `.hermes/runs/01M24Z37A0GXR6C104K5Q75T4X/registration.json`, schema 4, TODO-103, native-sdd, Claude client.
- Registered phase: `phase_4_development` (the registration contains exactly this step key). A descriptive “phase 3” label is not authoritative.
- Worktree: `.worktrees/todo-103-split-harness-py-into-a-package-by-concern`; branch: `refactor/harness-package-split`.
- Registered base: `2fb37e4bafb970c5285b7d2e864f83a7354762f9`.
- Observed HEAD: `1075df3e3927201f59aaf94ad6f3da2054dfd8c5`.
- `git status --short`: empty. No MERGE_HEAD, CHERRY_PICK_HEAD, REVERT_HEAD, or index.lock was present.
- Pinned Plan bytes SHA-256: `b3333dd6d08067c392236a577d6835cf315e21d587390ed48c953a71e5558ef9`, matching registration.
- The existing `load_validated_registration` validator passed against the original registration and repository identity, returning the registered branch, phase, Plan hash, and nine manifest tasks. This validates offline authority, not current card provenance.
- SHA-256 of `git diff --binary BASE HEAD --`: `72e396801af4702e31912d970b4ad61b60cf4ca070f7a33b8975cc32af35cdca`.

Nine commits follow the registered base in the original manifest order:

| Task | Commit | Subject |
| --- | --- | --- |
| task-01 | 441aeafc4677 | Convert harness.py into a package |
| task-02 | 8f174e9bf032 | Move errors and sandbox git operations |
| task-03 | 180e9ac47908 | Move issue, PR, provenance, and remote cleanup |
| task-04 | 0de3d779eadc | Move convergence and Kanban polling |
| task-05 | 8d06e5a74ed0 | Move run_tick and registration recovery |
| task-06 | 839b946bfeeb | Move the tick drive loop |
| task-07 | d927008a7341 | Move shutdown and quiescence |
| task-08 | 9b7c3442dac5 | Move profile setup and run_harness |
| task-09 | 1075df3e3927 | Split harness tests by submodule |

Commit order and matching subjects establish a candidate mapping, not accepted checkpoints or completion. Static AST comparison found none of the 465 original test-function names missing from the candidate split test files; this does not establish equal collected tests or equivalent assertions.

## Blocking evidence and verification gaps

The original task-03 acceptance criterion requires `hermes_pipeline/harness/github.py` to be under 1000 lines. The current committed file has 1162 lines. Recovery-only validation therefore cannot certify the approved Plan as complete. This inspection did not modify the implementation or the Plan to resolve the discrepancy.

Supported `hermes kanban show <id> --json` and `hermes kanban runs <id> --json` were attempted for both `t_af1f2863` and `t_ddcc0e11`. All returned empty stdout. Inspection of the implementation-card show diagnostic found that Hermes attempted to initialize `~/.hermes/kanban.db.init.lock`, which the execution sandbox disallowed as a read-only filesystem. The command misleadingly exited zero. No direct database fallback was used. Current card status, card-to-registration provenance, worker runs, manual blocks, and monitor ownership remain unverified.

A Git archive of the observed HEAD was extracted into a task-owned scratch directory so checks could not modify the registered worktree. Two initial focused pytest attempts were interrupted without results. A subsequent full run completed using the existing development environment, a workspace-local uv cache, and `PYTHONPATH` pointing to the archive:

- `rtk proxy uv run --no-sync pytest -vv`: **2950 passed, 10 skipped, 10 failed** in 273.08 seconds. Six failures attempt writes beneath the sandbox's read-only `~/.hermes`; the other four correspond to failures also observed in the supervisor branch's unchanged baseline (two Git error-text expectations, one Git-failure classification test, and one empty-project classification test). These results do not establish a passing full gate.
- `rtk uv run --no-sync ruff check .`: **passed**.
- `rtk uv run --no-sync python scripts/release_changesets.py check`: **passed**, version 1.0.2.

These are provider-free checks; no live Hermes agent or provider ran. Historical red-first evidence and durable client exit collection were not established. The monitor's historical exit remains unknown.

An independent read-only review rejected fresh completion evidence. In addition to the line-count failure, it found a runtime regression: the new `harness/github.py::_read_json_file` lets corrupt anchor JSON raise `JSONDecodeError`, whereas the original helper returned `None` so `create_run_anchor` could raise the controlled `HarnessPreflightError`. A provider-free comparison reproduced that difference. It also found that `_run_with_timeout` remains in `poll.py` rather than the Plan's required `drive.py`, and `_validate_profile_prerequisites` remains in `github.py` rather than `run.py`; the layout tests omit these respective assertions.

The review confirmed preservation of all 465 original harness test definitions and all 39 original `real_git` markers. Function-local relative imports remain despite the Plan's literal prohibition. Task-09's blanket test-file size criterion also covers unrelated pre-existing files, conflicting with its harness-only scope; no unrelated tests were changed to satisfy that ambiguity.

## Supported next steps after blockers are resolved

First obtain supported fresh `show` and `runs` snapshots for both card IDs in an environment where Hermes can acquire its normal initialization lock. Verify board, TODO, tick, phase, registered worktree, current statuses, and current/manual block provenance before proposing a transition.

`hermes kanban complete --help` confirms supported `complete <task_id> --result ... --summary ... --metadata <JSON>` handling. This is a possible completion transport only after a separately admitted recovery-only attempt establishes current verification and review evidence and the existing TPO result validator accepts identity and result metadata. It must not assert a successful historical monitor exit. No concrete completion payload can safely be prepared from the currently unavailable card/run snapshots and the failed Plan acceptance criterion.

Do not unblock either candidate automatically, rerun implementation, change unrelated blocks, or complete based on these nine commits alone. The original worktree and journals remain the recovery source; repeat HEAD, dirty-state, and digest checks before any later operational action.
