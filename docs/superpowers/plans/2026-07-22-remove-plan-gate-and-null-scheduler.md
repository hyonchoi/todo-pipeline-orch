# Remove Plan-Gate + Null-Kanban-Scheduler Dead Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the entire dead plan-gate subsystem (TODO-26) and dead null-kanban-scheduler subsystem (TODO-27, plus the absorbed TODO-29 merge/status/kill scope), restoring the invariant that the harness only ever exercises the real (`--kanban hermes`) production dispatch path.

**Architecture:** One combined PR, bottom-up deletion order: CLI subcommands first (so nothing still calls into modules we're about to remove), then the null-mode harness branch + `runner.py` + `watcher.py`, then `review_phase.py`, then `phases.py`'s marker/gate machinery, then `gates.py`/`gate_state.py`/`kanban_tasks.py`'s plan-gate branches, then `kanban.py`'s `NullKanbanAdapter`, then the `decision/context.py` behavior fix (collapse `in_flight_ids()` onto the kanban-only path), then docs. Every task ends with a green test run so a broken import never survives past its own task.

**Tech Stack:** Python 3.12+, `uv`, `pytest`, `argparse` (cli.py subparsers).

## Global Constraints

- Every deletion target in this plan was grep-verified this session (via office-hours + `/plan-eng-review`) to have zero live callers outside the dead subsystem and its own dedicated tests — see the design doc's Verification section and Amended Deletion Scope.
- `HermesKanbanAdapter` (kanban.py) and its test coverage are **not** touched — only the dead `NullKanbanAdapter` sibling is removed.
- `ship.py`'s `maybe_ship_ready`/`approve_ship` (the real Phase 9 flow) is **not** touched.
- `kanban_tasks.py`'s `get_todo_kanban_status` (the real in-flight-state source of truth) is **not** touched.
- TODO-29's `phase_9_ship` → `phase_9_human_review` rename and a `cmd_kill` redesign are explicitly **out of scope** — `cmd_kill` is deleted outright, not redesigned.
- Version bump: per this repo's CLAUDE.md, VERSION + `pyproject.toml` + `uv.lock` + `CHANGELOG.md` must all be updated together in the final task, never independently.
- Every task must end with the full test suite green (`uv run pytest`) before moving to the next task — a broken import must never survive past the task that introduced it.
- CHANGELOG.md and `docs/gstack/*` are explicitly **out of scope** for content rewrites — they are point-in-time historical records, not living docs.

---

### Task 1: Delete `approve-plan`, `merge`, `status`, `kill` CLI subcommands

**Files:**
- Modify: `hermes_pipeline/cli.py`
- Test: `tests/test_cli.py` (existing — verify subcommand list), delete `tests/test_approve_cli.py`, `tests/test_cli_kill.py`, `tests/test_status.py`, `tests/test_merge.py`, `tests/test_chaos.py` wholesale in this task (they exercise only the code removed here)

**Interfaces:**
- Consumes: nothing from other tasks — this is the entrypoint of the bottom-up deletion order (subcommands must go first so nothing downstream still has a live caller).
- Produces: a `cli.py` with no `approve-plan`/`merge`/`status`/`kill` subparsers, `_cmd_approve_plan`, `_cmd_merge`, `_cmd_status`, `_cmd_kill`, `cmd_kill`, or `_kill_all_projects`. Task 3 (delete `runner.py`/`watcher.py`) and Task 5 (delete `merge.py`/`status.py`/`review_phase.py`) rely on `cli.py` no longer importing from those modules.

- [ ] **Step 1: Remove the `merge` subparser registration**

In `hermes_pipeline/cli.py`, delete the block around lines 410-421:

```python
    # merge: Phase 9 merge
    merge_parser = subparsers.add_parser(
        "merge",
        help="Execute Phase 9: merge a ready TODO to main",
    )
    merge_parser.add_argument("project", help="Project name")
    merge_parser.add_argument("todo_id", type=_parse_todo_id, help="TODO ID to merge (must be a number)")
    merge_parser.add_argument(
        "--abandon",
        action="store_true",
        help="Abandon the merge without confirmation",
    )
    merge_parser.set_defaults(func=_cmd_merge)
```

- [ ] **Step 2: Remove the `approve-plan` subparser registration**

Delete the block around lines 440-465:

```python
    # approve-plan: Phase 2b plan gate — approve/reject the decision sheet
    approve_plan_parser = subparsers.add_parser(
        "approve-plan",
        help="Approve or reject a plan-gate decision sheet for a TODO",
    )
    approve_plan_parser.add_argument("project", help="Project name")
    approve_plan_parser.add_argument(
        "--todo", required=True, type=_parse_todo_id_flag,
        help="TODO whose plan to approve/reject (e.g. TODO-5)",
    )
    ap_action = approve_plan_parser.add_mutually_exclusive_group(required=True)
    ap_action.add_argument(
        "--approve", action="store_true", help="Approve the plan",
    )
    ap_action.add_argument(
        "--reject", action="store_true", help="Reject the plan (requires --reason)",
    )
    approve_plan_parser.add_argument(
        "--override", action="append", metavar="Q_ID=LABEL", default=None,
        help="Override a recommendation (repeatable), e.g. --override q1=B. "
             "Only valid with --approve.",
    )
    approve_plan_parser.add_argument(
        "--reason", default=None,
        help="Rejection reason (required with --reject)",
    )
    approve_plan_parser.set_defaults(func=_cmd_approve_plan)
```

- [ ] **Step 3: Remove the `status` subparser registration**

Delete the block around lines 468-472:

```python
    # status: List pending records
    status_parser = subparsers.add_parser(
        "status",
        help="Display pending ready-for-review records",
    )
    status_parser.set_defaults(func=_cmd_status)
```

- [ ] **Step 4: Remove the `kill` subparser registration**

Delete the block around lines 475-483:

```python
    # kill: Kill in-flight phases
    kill_parser = subparsers.add_parser(
        "kill",
        help="Kill in-flight phase(s)",
    )
    kill_group = kill_parser.add_mutually_exclusive_group(required=True)
    kill_group.add_argument("--all", dest="all_", action="store_true", help="Kill all in-flight phases")
    kill_group.add_argument("--todo", help="Kill a specific TODO (e.g., TODO-1)")
    kill_parser.add_argument("project", nargs="?", default=None, help="Project name (optional — omit to scan all projects)")
    kill_parser.set_defaults(func=_cmd_kill)
```

- [ ] **Step 5: Remove `_cmd_approve_plan`, `_cmd_merge`, `_cmd_status`, `_cmd_kill`, `cmd_kill`, `_kill_all_projects`**

Delete the following function definitions from `hermes_pipeline/cli.py` (all bodies quoted in the design-review research, standard function-boundary deletion):
- `cmd_kill()` (~lines 172-267)
- `_kill_all_projects()` (~lines 269-340) — helper used only by `cmd_kill`
- `_cmd_approve_plan()` (~lines 616-667)
- `_cmd_merge()` (~lines 670-723)
- `_cmd_status()` (~lines 726-735)
- `_cmd_kill()` (~lines 737-761)

- [ ] **Step 6: Remove the `maybe_plan_gate_ready` tick-loop call site**

Delete the block around lines 1133-1141:

```python
    # Plan-gate: detect blocked plan-gate before all_phases_complete check.
    from . import gates
    gates.maybe_plan_gate_ready(
        project_dir=project_dir,
        project_slug=project_slug,
        prior_tick_id=prior_tick_id,
        state_dir=project_state,
        slack_channel=slack_channel,
    )
```

This call site is why `PLAN_GATE_PHASE_KEY`/`maybe_plan_gate_ready` couldn't be deleted by a naive "zero callers" grep — it runs every tick. It early-returns as a no-op once `phase_2b_plan_gate` never appears in `status_map`, but that's a behavioral fact about the callee, not a reason to leave a call to a function we're about to delete (Task 5 deletes `maybe_plan_gate_ready` itself).

- [ ] **Step 7: Remove now-dead imports in `cli.py`**

Search for and remove these import lines (only if now unused after Steps 1-6 — check no other surviving function in `cli.py` still references them):
```python
from .merge import default_confirm_fn, make_default_bump_fn, run_phase9
from .status import collect_pending, format_table
```
Also check the top-of-file `from . import approve_plan as ap` (inside `_cmd_approve_plan`, already removed with the function in Step 5) and any module-level `from .kanban import NullKanbanAdapter` import that was only used by `_cmd_merge`. `HermesKanbanAdapter` import must stay — it's still used elsewhere in `cli.py`.

- [ ] **Step 8: Delete the dedicated test files**

```bash
rm tests/test_approve_cli.py tests/test_cli_kill.py tests/test_status.py tests/test_merge.py tests/test_chaos.py
```

- [ ] **Step 9: Grep-verify no remaining references**

```bash
grep -rn "approve-plan\|_cmd_approve_plan\|_cmd_merge\|_cmd_status\|_cmd_kill\|cmd_kill\|_kill_all_projects\|maybe_plan_gate_ready" hermes_pipeline/
```
Expected: zero matches inside `hermes_pipeline/` (matches inside `docs/` are fine — cleaned up in Task 8).

- [ ] **Step 10: Run the full test suite**

```bash
uv run pytest
```
Expected: collection succeeds (no `ImportError` for `approve_plan`/`merge`/`status`/`gates.maybe_plan_gate_ready` from `cli.py`), and all remaining tests pass. `approve_plan.py`, `merge.py`, `status.py`, `gates.maybe_plan_gate_ready` themselves still exist on disk at this point (deleted in Tasks 2 and 5) — this step only confirms `cli.py` no longer needs them.

- [ ] **Step 11: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_approve_cli.py tests/test_cli_kill.py tests/test_status.py tests/test_merge.py tests/test_chaos.py
git commit -m "chore: delete approve-plan/merge/status/kill CLI subcommands"
```

---

### Task 2: Delete `approve_plan.py` module and its tests

**Files:**
- Delete: `hermes_pipeline/approve_plan.py`
- Delete: `tests/test_approve_plan.py`

**Interfaces:**
- Consumes: Task 1 removed `cli.py`'s only caller (`from . import approve_plan as ap` inside `_cmd_approve_plan`).
- Produces: nothing downstream depends on this module.

- [ ] **Step 1: Grep-verify zero remaining callers**

```bash
grep -rln "approve_plan" hermes_pipeline/ tests/ | grep -v "test_approve_plan.py\|approve_plan.py"
```
Expected: no output.

- [ ] **Step 2: Delete the module and its test file**

```bash
rm hermes_pipeline/approve_plan.py tests/test_approve_plan.py
```

- [ ] **Step 3: Run the full test suite**

```bash
uv run pytest
```
Expected: collection succeeds, all tests pass.

- [ ] **Step 4: Commit**

```bash
git add -A hermes_pipeline/approve_plan.py tests/test_approve_plan.py
git commit -m "chore: delete approve_plan.py (dead plan-gate module)"
```

---

### Task 3: Delete `merge.py`, `status.py`, `review_phase.py` modules and their tests

**Files:**
- Delete: `hermes_pipeline/merge.py`, `hermes_pipeline/status.py`, `hermes_pipeline/review_phase.py`
- Delete: `tests/test_review_phase.py`

**Interfaces:**
- Consumes: Task 1 removed `cli.py`'s calls into `merge.py` (`run_phase9`, `make_default_bump_fn`, `default_confirm_fn`) and `status.py` (`collect_pending`, `format_table`). `review_phase.py`'s only external caller, `phases.py`'s `_invoke_review_phase`, is deleted in Task 4 — but `review_phase.py` has no callers *outside* `phases.py`, so it's safe to delete now; Task 4 will remove the now-dangling import in `phases.py`.
- Produces: nothing downstream depends on these three modules. Note `default_bump_fn` also lived in `merge.py` and dies with the file.

- [ ] **Step 1: Grep-verify `merge.py` and `status.py` have zero remaining callers**

```bash
grep -rln "from .merge import\|from \.status import\|hermes_pipeline\.merge\|hermes_pipeline\.status" hermes_pipeline/ tests/
```
Expected: only `tests/test_merge.py`/`tests/test_chaos.py`/`tests/test_status.py` would show up, but those were already deleted in Task 1 Step 8 — so expected output is empty.

- [ ] **Step 2: Delete `merge.py` and `status.py`**

```bash
rm hermes_pipeline/merge.py hermes_pipeline/status.py
```

- [ ] **Step 3: Delete `review_phase.py` and its test**

`review_phase.py`'s only external caller is `phases.py`'s `_invoke_review_phase`, which Task 4 deletes. Deleting the module now and fixing the now-broken import in `phases.py` immediately keeps this task's own test run green:

```bash
rm hermes_pipeline/review_phase.py tests/test_review_phase.py
```

- [ ] **Step 4: Remove the now-dangling `review_phase` import in `phases.py`**

Find the import at the top of `hermes_pipeline/phases.py` (e.g. `from . import review_phase` or `from .review_phase import capture_pre_review_state, finalize_review, run_pytest, commit_all`) and delete it. This is a narrow pre-step for Task 4's full `_invoke_review_phase` deletion — done here only so this task's test run doesn't fail on a missing module.

- [ ] **Step 5: Run the full test suite**

```bash
uv run pytest
```
Expected: collection succeeds, no `ModuleNotFoundError` for `hermes_pipeline.merge`, `hermes_pipeline.status`, or `hermes_pipeline.review_phase`. `phases.py`'s `_invoke_review_phase` function body still references the now-removed names at this point only if it wasn't the sole caller pulled via the deleted import — confirm no `NameError`-shaped lint failure by running:

```bash
uv run python -c "import hermes_pipeline.phases"
```
Expected: no `ImportError`/`NameError` at import time (Python doesn't validate function bodies until called, so this only checks the module-level import list — full function-body cleanup happens in Task 4).

- [ ] **Step 6: Commit**

```bash
git add -A hermes_pipeline/merge.py hermes_pipeline/status.py hermes_pipeline/review_phase.py tests/test_review_phase.py hermes_pipeline/phases.py
git commit -m "chore: delete merge.py, status.py, review_phase.py (dead Phase 9/status/review-PRE-POST modules)"
```

---

### Task 4: Delete null-mode machinery from `phases.py` (marker helpers, gate dispatch, `run()`, `_invoke_hermes`/`_invoke_review_phase`)

**Files:**
- Modify: `hermes_pipeline/phases.py`

**Interfaces:**
- Consumes: Task 3 already removed the `review_phase` import. This task removes the functions that used it.
- Produces: a `phases.py` with no `run()`, `_invoke_hermes`, `_invoke_review_phase`, marker helpers (`_marker_path`, `_write_marker`, `_update_marker_pid`, `_delete_marker`, `MarkerHeld`), the gate-dispatch block, or `_generate_decision_sheet_post_autoplan`. Task 5 (harness.py) relies on `phases.run`/`PipelineRunner`'s only remaining caller being gone before `runner.py`/`watcher.py` are deleted — actually the ordering is: this task removes `phases.run`, then Task 5 deletes `runner.py` (whose only caller was `harness.py`'s null-mode branch) and `watcher.py` (which imports `phases.run` — must go after `phases.run` is confirmed dead, but `watcher.py` itself has zero external callers per the design doc, so order between Task 4 and Task 5 here is safe either way; this plan does Task 4 first since `phases.py` is the shared file both TODOs touch).

- [ ] **Step 1: Delete the marker helpers**

Delete `_marker_path()`, `MarkerHeld`, `_write_marker()`, `_update_marker_pid()`, `_delete_marker()` from `hermes_pipeline/phases.py` (contiguous block, ~lines 63-126):

```python
def _marker_path(state_dir: Path, todo_id: str) -> Path:
    d = Path(state_dir) / "phase_started"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{todo_id}.json"


class MarkerHeld(Exception):
    """Raised when a phase_started marker for this todo_id is already present."""


def _write_marker(state_dir: Path, *, todo_id: str, tick_id: str, phase_key: str) -> Path:
    ...


def _update_marker_pid(state_dir: Path, todo_id: str, child_pid: int) -> None:
    ...


def _delete_marker(state_dir: Path, todo_id: str, *, tick_id: str | None = None) -> None:
    ...
```

- [ ] **Step 2: Delete the gate-dispatch block inside `_invoke_hermes()`**

Delete the `if phase.gate:` block (~lines 330-350):

```python
    if phase.gate:
        from .gate_state import GateStatus, gate_status as _gate_status

        status = _gate_status(
            state_dir=sd, project_slug=project_slug, tick_id=tick_id,
            gate_key=phase.phase_key,
        )
        if status == GateStatus.RUNNING:
            log.info("gate %s approved (RUNNING) for %s tick %s — skipping",
                      phase.phase_key, todo_id, tick_id)
            return {"status": "success", "phase_key": phase_key, "tick_id": tick_id}
        raise RuntimeError(
            f"gate {phase.phase_key} for {todo_id} tick {tick_id} "
            f"is {status.value}, cannot proceed"
        )
```

- [ ] **Step 3: Delete the post-autoplan decision-sheet hook inside `_invoke_hermes()`**

Delete (~lines 379-384):

```python
    # Post-phase hook: after autoplan succeeds, generate decision sheet for plan gate
    if phase_key == "phase_2_autoplan" and result["returncode"] == 0:
        _generate_decision_sheet_post_autoplan(
            todo_id=todo_id, tick_id=tick_id, state_dir=sd,
            project_dir=kw.get("project_dir"),
        )
```

- [ ] **Step 4: Delete `_generate_decision_sheet_post_autoplan()`**

Delete the full function (~lines 464-497) — its only caller was the block removed in Step 3, and it imports `from .gates import stub_generate_decision_sheet`, which Task 6 deletes.

- [ ] **Step 5: Delete `_invoke_hermes`, `_invoke_review_phase`, and `run()`**

Delete these three function definitions entirely from `hermes_pipeline/phases.py`. `_invoke_hermes` and `_invoke_review_phase` are the null-mode dispatch functions (`_invoke_hermes` for non-review phases, `_invoke_review_phase` for the PRE/POST review split — the latter's body called into `review_phase.py`, already deleted in Task 3). `run()` is the library entrypoint that wired markers around whichever of the two `_invoke_*` functions it dispatched to (its marker calls were at ~lines 430, 453-454, now moot since the whole function goes).

- [ ] **Step 6: Grep-verify zero remaining references to deleted symbols**

```bash
grep -n "def run(\|_invoke_hermes\|_invoke_review_phase\|_marker_path\|_write_marker\|_update_marker_pid\|_delete_marker\|MarkerHeld\|_generate_decision_sheet_post_autoplan\|gate_state\|phase\.gate" hermes_pipeline/phases.py
```
Expected: no matches (a `Phase.gate` *field* on the dataclass, if it exists for `phases.yaml` schema reasons unrelated to dispatch, may legitimately remain — inspect any hit before treating it as a plan violation; only dispatch logic that *reads* `phase.gate` to route to the deleted gate block should be gone).

- [ ] **Step 7: Run the full test suite**

```bash
uv run pytest
```
Expected: `tests/test_phases.py` (if it exists) or any test importing `phases.run`/`_invoke_hermes`/`_invoke_review_phase` will fail collection if not yet pruned — check for such a file and remove only the now-invalid test cases (functions that directly call the deleted symbols), keeping cases that test surviving `phases.py` functionality (e.g. `phases.yaml` loading/parsing, `Phase` dataclass).

- [ ] **Step 8: Commit**

```bash
git add hermes_pipeline/phases.py tests/
git commit -m "chore: delete phases.py null-mode dispatch (run/_invoke_hermes/_invoke_review_phase/markers/gate-dispatch)"
```

---

### Task 5: Delete `runner.py`, `watcher.py`, and the `--kanban null` branch in `harness.py`

**Files:**
- Delete: `hermes_pipeline/runner.py`, `hermes_pipeline/watcher.py`
- Delete: `tests/test_runner.py`, `tests/test_runner_gate.py`
- Modify: `hermes_pipeline/harness.py`

**Interfaces:**
- Consumes: Task 4 removed `phases.run`, the function `watcher.py` imports and re-exposes with zero external callers of its own.
- Produces: `harness.py` with a single kanban mode (real, `--kanban hermes`); the `--kanban` flag either removed or restricted to `"hermes"` only, matching this plan's Success Criteria.

- [ ] **Step 1: Delete the `--kanban null` branch in `harness.py`'s `run_harness()`**

Delete the block (~lines 712-735):

```python
    # Null kanban path: PipelineRunner (backward compat)
    kanban = NullKanbanAdapter()
    runner = PipelineRunner(
        project=fixture["project_slug"],
        project_dir=temp_dir,
        branch=fixture["branch"],
        todo_id=fixture["todo_id"],
        title=f"Mock TODO-{fixture['todo_id']}",
        phases=phases,
        state=state,
        kanban=kanban,
        kanban_metadata=None,
        run_phase_fn=lambda phase: _dispatch_phase(
            phase,
            state_dir=state_dir,
            todo_id=fixture["todo_id"],
            tick_id=tick_id,
            project_slug=fixture["project_slug"],
            project_dir=temp_dir,
            error_holder=error_holder,
        ),
        continue_on_failure=True,
        monitor=monitor,
    )

    success, timed_out, result_box = _run_with_timeout(runner.run, timeout=timeout)
```

Confirm via `grep -n "kanban" hermes_pipeline/harness.py` what surrounding `if config.kanban_adapter == "hermes": ... else: ...` (or equivalent CLI-flag branch) structure wraps this block, and collapse it to the single `hermes` path — the `else`/null branch and any `--kanban` argparse choice restricting to `["hermes", "null"]` must be narrowed to `"hermes"` only (or the flag removed if `"hermes"` becomes the only possible value).

- [ ] **Step 2: Remove the `NullKanbanAdapter` and `PipelineRunner` imports in `harness.py`**

Delete:
```python
from .kanban import NullKanbanAdapter
```
(line ~613) and the `from .runner import PipelineRunner` import (wherever it appears), now unused after Step 1.

- [ ] **Step 3: Delete `runner.py` and `watcher.py`**

```bash
rm hermes_pipeline/runner.py hermes_pipeline/watcher.py
```

- [ ] **Step 4: Delete the dedicated test files**

```bash
rm tests/test_runner.py tests/test_runner_gate.py
```

- [ ] **Step 5: Grep-verify zero remaining references**

```bash
grep -rn "PipelineRunner\|from \.runner\|from \.watcher\|hermes_pipeline\.runner\|hermes_pipeline\.watcher\|--kanban.*null\|kanban.*=.*[\"']null[\"']" hermes_pipeline/ tests/
```
Expected: no matches.

- [ ] **Step 6: Run the full test suite**

```bash
uv run pytest
```
Expected: `tests/test_harness.py` collection succeeds. Per the research, `TestKanbanModeHermes`, `TestAutoCompleteGateTasks`, `TestPollKanbanPhases` and other classes already exercise the surviving `hermes` kanban mode and should stay untouched — if any test in `test_harness.py` explicitly sets `--kanban null` or asserts on `PipelineRunner`/`NullKanbanAdapter` wiring, remove only that test function.

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/harness.py tests/
git rm hermes_pipeline/runner.py hermes_pipeline/watcher.py tests/test_runner.py tests/test_runner_gate.py
git commit -m "chore: delete runner.py, watcher.py, harness.py --kanban null branch"
```

---

### Task 6: Delete plan-gate branches in `gates.py`, `gate_state.py`, `kanban_tasks.py`

**Files:**
- Delete: `hermes_pipeline/gate_state.py`
- Modify: `hermes_pipeline/gates.py`, `hermes_pipeline/kanban_tasks.py`
- Modify: `tests/test_gates.py` (prune `TestMaybePlanGateReady`)

**Interfaces:**
- Consumes: Task 1 removed `cli.py`'s `maybe_plan_gate_ready` call site. Task 4 removed `phases.py`'s `gate_state.gate_status` call (inside the deleted gate-dispatch block) and `gates.stub_generate_decision_sheet` call (inside the deleted `_generate_decision_sheet_post_autoplan`).
- Produces: `gates.py` with no `PLAN_GATE_PHASE_KEY`, `is_high_risk`, `maybe_plan_gate_ready`, or `stub_generate_decision_sheet`; `gate_state.py` deleted entirely; `kanban_tasks.py` with no `phase_2b_plan_gate` branch.

- [ ] **Step 1: Delete `stub_generate_decision_sheet()` in `gates.py`**

Delete the function (~lines 127-227). Its only caller, `phases.py`'s `_generate_decision_sheet_post_autoplan`, was deleted in Task 4.

- [ ] **Step 2: Delete `PLAN_GATE_PHASE_KEY` in `gates.py`**

Delete:
```python
PLAN_GATE_PHASE_KEY = "phase_2b_plan_gate"
```
(line ~234). Confirm via grep this constant has no remaining readers after Step 4 removes `kanban_tasks.py`'s use of it (if `kanban_tasks.py` imports the constant rather than hardcoding the string — check before deleting).

- [ ] **Step 3: Delete `is_high_risk()` in `gates.py`**

Delete the function (~lines 251-286) — zero callers anywhere in `hermes_pipeline/*.py` per the design doc's original verification.

- [ ] **Step 4: Delete `maybe_plan_gate_ready()` in `gates.py`**

Delete the function (~lines 322-371). Its only caller, `cli.py`'s tick-loop call site, was removed in Task 1 Step 6.

- [ ] **Step 5: Delete `hermes_pipeline/gate_state.py` entirely**

```bash
rm hermes_pipeline/gate_state.py
```
This removes `GateStatus` and `gate_status()`. Both callers (`phases.py`'s gate-dispatch block, deleted Task 4; `kanban_tasks.py`'s `phase_2b_plan_gate` branch, deleted next step) are gone.

- [ ] **Step 6: Delete the `phase_2b_plan_gate` branch in `kanban_tasks.py`**

Delete (~lines 443-456):

```python
    if key == "phase_2b_plan_gate":
        from .gate_state import GateStatus, gate_status

        status = gate_status(
            state_dir=state_dir_path, project_slug=tenant,
            tick_id=tick_id, gate_key=key,
        )
        if status == GateStatus.FAILED:
            log.info(
                "plan-gate %s rejected — skipping "
                "missing-phase check for tick %s",
                key, tick_id,
            )
            continue
```

- [ ] **Step 7: Grep-verify zero remaining references**

```bash
grep -rn "PLAN_GATE_PHASE_KEY\|is_high_risk\|maybe_plan_gate_ready\|stub_generate_decision_sheet\|gate_state\|GateStatus\|phase_2b_plan_gate" hermes_pipeline/
```
Expected: no matches (docs cleanup is Task 8).

- [ ] **Step 8: Prune `TestMaybePlanGateReady` in `tests/test_gates.py`**

Remove the `TestMaybePlanGateReady` test class (~lines 378-492) — it tests the now-deleted `maybe_plan_gate_ready`. Keep every other test class in `test_gates.py` (decision-sheet schema, override sanitization, etc. — the parts of `gates.py` this task does not touch). Inspect `TestTickProjectWiring` (if present) for any direct reference to `maybe_plan_gate_ready`/`gate_state` and prune only those specific test functions, keeping the rest of the class.

- [ ] **Step 9: Delete `tests/test_gate_state.py` if it exists**

```bash
test -f tests/test_gate_state.py && rm tests/test_gate_state.py || echo "no dedicated gate_state test file"
```

- [ ] **Step 10: Run the full test suite**

```bash
uv run pytest
```
Expected: `tests/test_gates.py` and `tests/test_kanban_tasks.py` collection succeeds, all remaining tests pass.

- [ ] **Step 11: Commit**

```bash
git add hermes_pipeline/gates.py hermes_pipeline/kanban_tasks.py tests/test_gates.py
git rm hermes_pipeline/gate_state.py
git add -A
git commit -m "chore: delete plan-gate branches in gates.py/gate_state.py/kanban_tasks.py"
```

---

### Task 7: Delete `NullKanbanAdapter` and collapse `decision/context.py`'s `in_flight_ids()` onto the kanban-only path

**Files:**
- Modify: `hermes_pipeline/kanban.py`
- Modify: `hermes_pipeline/decision/context.py`
- Modify: `tests/test_kanban.py` (prune `TestNullKanbanAdapter`)
- Modify: `tests/test_decision_context.py`, `tests/test_decision_context_edge.py`

**Interfaces:**
- Consumes: Task 1 removed `cli.py`'s `_cmd_merge` (the stated reason `NullKanbanAdapter` was originally kept); Task 5 removed `harness.py`'s `--kanban null` usage (the other caller).
- Produces: `kanban.py` with no `NullKanbanAdapter`; `decision/context.py`'s `in_flight_ids()` relying solely on the kanban lookup, with `_rfr_ids`/`_phase_started_ids` and their `| set(...)` union removed. This is the one genuine **behavior change** in this PR (design doc item 6) — during a kanban outage, in-flight detection now returns nothing from the file-marker fallback instead of a stale/inconsistent partial view, since nothing writes those markers anymore post-deletion anyway.

- [ ] **Step 1: Grep-verify `NullKanbanAdapter` has zero remaining callers**

```bash
grep -rn "NullKanbanAdapter" hermes_pipeline/ tests/
```
Expected: only `hermes_pipeline/kanban.py` (definition) and `tests/test_kanban.py` (its dedicated test class).

- [ ] **Step 2: Delete `NullKanbanAdapter` in `kanban.py`**

Delete the class (~lines 86-115):

```python
class NullKanbanAdapter:
    """No-op kanban adapter. All operations succeed silently."""

    def set_active_task(
        self,
        project: str,
        *,
        todo_id: int,
        title: str,
        phase: str,
        metadata: dict[str, str] | None = None,
    ) -> SyncResult:
        return SyncResult(ok=True)

    def update_phase(
        self,
        project: str,
        *,
        phase: str,
        status: PhaseStatus,
    ) -> SyncResult:
        return SyncResult(ok=True)

    def clear_active_task(
        self,
        project: str,
        *,
        outcome: KanbanOutcome,
    ) -> SyncResult:
        return SyncResult(ok=True)
```

- [ ] **Step 3: Prune `TestNullKanbanAdapter` in `tests/test_kanban.py`**

Delete the `TestNullKanbanAdapter` class (~lines 43-89). Keep the rest of the 812-line file — protocol/outbox/`HermesKanbanAdapter` tests are untouched.

- [ ] **Step 4: Delete `_rfr_ids()` in `decision/context.py`**

Delete (~lines 25-49):

```python
def _rfr_ids(state_dir: Path) -> list[str]:
    """Extract TODO IDs from ready_for_review/todo-<n>.json files.
    ...
    """
    d = state_dir / "ready_for_review"
    if not d.exists():
        return []
    out = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        stem = p.stem
        if stem.startswith("todo-"):
            stem = stem[len("todo-"):]
        try:
            tid = int(stem)
        except ValueError:
            continue
        out.append(f"TODO-{tid}")
    return out
```

- [ ] **Step 5: Delete `_phase_started_ids()` in `decision/context.py`**

Delete (~lines 51-88):

```python
def _phase_started_ids(state_dir: Path, *, max_phase_timeout_min: int) -> list[str]:
    """Extract TODO IDs from phase_started/ markers, sweeping stale + dead.
    ...
    """
    d = state_dir / "phase_started"
    if not d.exists():
        return []
    cutoff = time.time() - max_phase_timeout_min * 60
    out = []
    for p in d.iterdir():
        if not p.is_file():
            continue
        stale_mtime = p.stat().st_mtime < cutoff
        if not stale_mtime:
            out.append(p.stem)
            continue
        child_pid = None
        try:
            data = json.loads(p.read_text())
            child_pid = data.get("child_pid")
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if child_pid is not None and _pid_alive(int(child_pid)):
            out.append(p.stem)
            continue
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return out
```

- [ ] **Step 6: Collapse `in_flight_ids()` onto the kanban-only path**

Find `in_flight_ids()` in `hermes_pipeline/decision/context.py` (~lines 159-163 per the design doc, though it may be a few lines longer once you include the union). It currently looks like a shape such as:

```python
def in_flight_ids(...) -> list[str]:
    kanban_ids = _kanban_in_flight_ids(...)
    fallback_ids = set(_rfr_ids(state_dir)) | set(_phase_started_ids(state_dir, max_phase_timeout_min=...))
    return list(set(kanban_ids) | fallback_ids)
```

Rewrite it to rely solely on the kanban lookup — remove the `_rfr_ids`/`_phase_started_ids` calls and the `| set(...)` union entirely, e.g.:

```python
def in_flight_ids(...) -> list[str]:
    return _kanban_in_flight_ids(...)
```

(Match the exact existing signature and the exact name of the kanban-lookup helper already in the file — read the current function body with `Read` before editing, since the snippet above is illustrative of the shape being removed, not an exact transcription. Do not rename `in_flight_ids` or change its call sites' expectations — it must keep returning `list[str]`.)

- [ ] **Step 7: Grep-verify zero remaining references to the deleted helpers**

```bash
grep -n "_rfr_ids\|_phase_started_ids" hermes_pipeline/decision/context.py
```
Expected: no matches.

- [ ] **Step 8: Update `tests/test_decision_context.py` and `tests/test_decision_context_edge.py`**

Remove any test function that directly exercises `_rfr_ids`, `_phase_started_ids`, or asserts that `in_flight_ids()` includes IDs sourced only from `ready_for_review/*.json` or `phase_started/*` markers when the kanban lookup itself returns nothing for those IDs (the fallback-only scenario). Keep every test that exercises `in_flight_ids()` via the kanban lookup path (`_kanban_in_flight_ids` / `build_in_flight` per the research) — those are unaffected by this change.

- [ ] **Step 9: Run the full test suite**

```bash
uv run pytest
```
Expected: all tests pass, including the updated `test_decision_context*.py` files.

- [ ] **Step 10: Commit**

```bash
git add hermes_pipeline/kanban.py hermes_pipeline/decision/context.py tests/test_kanban.py tests/test_decision_context.py tests/test_decision_context_edge.py
git commit -m "chore: delete NullKanbanAdapter; collapse in_flight_ids() onto kanban-only lookup

Behavior change: during a kanban outage, in-flight detection no longer
falls back to reading ready_for_review/phase_started markers off disk —
nothing has written those since the null-mode scheduler was removed."
```

---

### Task 8: Update forward-facing docs; delete removed-feature how-to guides

**Files:**
- Modify: `docs/reference-cli.md`, `README.md`, `docs/ARCHITECTURE.md` (only if it references the removed subsystems)
- Delete: `docs/howto-approve-plan-gate.md`, `docs/howto-kill-stuck-phase.md`
- Modify: `docs/howto-approve-and-ship.md` (remove merge-specific portions only)

**Interfaces:**
- Consumes: nothing code-level — this task only needs the prior tasks' deletions to be true statements about the codebase.
- Produces: docs that accurately describe the post-deletion CLI surface. `CHANGELOG.md` and `docs/gstack/*` are explicitly untouched per this plan's Global Constraints.

- [ ] **Step 1: Remove subcommand entries from `docs/reference-cli.md`**

Delete the `### status` section (~lines 40-48), `### merge` section (~lines 52-69), `### approve-plan` section (~lines 103-131), and `### kill` section (~lines 132-155). Remove the reference to `howto-kill-stuck-phase.md` (~line 288) since that guide is deleted in Step 3.

- [ ] **Step 2: Update `README.md`**

Remove the plan-gate feature-list bullet (~lines 14-15):
```
- **Plan Gate (phase_2b)** — Human review checkpoint between Autoplan and Writing Plan. Blocks the pipeline until a human approves or rejects the plan via `pipeline-watch approve-plan`. Includes risk classifier, decision sheet schema, and override sanitization.
```
Grep the rest of `README.md` for `approve-plan`, `merge`, `kill`, `status`, `plan-gate`, `PipelineRunner`, `--kanban null` and remove/update any other stale references found.

- [ ] **Step 3: Delete removed-feature how-to guides**

```bash
rm docs/howto-approve-plan-gate.md docs/howto-kill-stuck-phase.md
```

- [ ] **Step 4: Prune merge-specific portions of `docs/howto-approve-and-ship.md`**

Read the file, remove sections describing the `merge`/`kill` CLI subcommands and the manual Phase 9 merge flow via `pipeline-watch merge`. Keep any sections describing the real, surviving Phase 9 flow (`ship.py`'s `maybe_ship_ready`/`approve_ship`) if the file also documents that path — do not delete the whole file unless every remaining section is merge-specific (confirm by reading before deciding).

- [ ] **Step 5: Check `docs/ARCHITECTURE.md` for stale references**

```bash
grep -n "plan.gate\|phase_2b\|PipelineRunner\|null.kanban\|NullKanbanAdapter\|approve-plan" docs/ARCHITECTURE.md
```
If matches are found, update the surrounding prose to describe the current (post-deletion) architecture — a single production dispatch path via kanban tasks, no plan-gate checkpoint, no null-mode scheduler.

- [ ] **Step 6: Grep-verify no remaining doc references to deleted commands**

```bash
grep -rln "approve-plan\|pipeline-watch merge\|pipeline-watch kill\|pipeline-watch status\|PipelineRunner\|NullKanbanAdapter" docs/ README.md
```
Expected: no matches outside `docs/gstack/*` and `CHANGELOG.md` (explicitly out of scope per Global Constraints).

- [ ] **Step 7: Commit**

```bash
git add docs/reference-cli.md README.md docs/howto-approve-and-ship.md
git rm docs/howto-approve-plan-gate.md docs/howto-kill-stuck-phase.md
git add docs/ARCHITECTURE.md 2>/dev/null || true
git commit -m "docs: remove approve-plan/merge/status/kill references and how-to guides"
```

---

### Task 9: Version bump (VERSION + pyproject.toml + uv.lock + CHANGELOG.md) and final full-suite verification

**Files:**
- Modify: `VERSION`, `pyproject.toml`, `uv.lock`, `CHANGELOG.md`

**Interfaces:**
- Consumes: all prior tasks' deletions must be complete and green.
- Produces: a version-synced repo state per this repo's CLAUDE.md version-bump-sync rule, and a final confirmation that Success Criteria are met.

- [ ] **Step 1: Read the current version**

```bash
cat VERSION
grep '^version' pyproject.toml
```
Confirm both match. Pick the next patch version (this is a cleanup PR, not a feature — patch bump unless the repo's convention says otherwise; check the last few `CHANGELOG.md` entries for precedent).

- [ ] **Step 2: Bump `VERSION`**

Edit `VERSION` to the new 3-digit version.

- [ ] **Step 3: Bump `pyproject.toml`**

Edit the `version = "..."` line in `pyproject.toml` to match `VERSION` exactly.

- [ ] **Step 4: Regenerate `uv.lock`**

```bash
uv sync
```
This regenerates the `hermes-pipeline` package entry's version in `uv.lock`. Do not hand-edit `uv.lock`.

- [ ] **Step 5: Add a `CHANGELOG.md` entry**

Add a new `## [X.Y.Z] - 2026-07-22` entry at the top of `CHANGELOG.md` (below any header) describing the deletion: removal of the dead plan-gate subsystem (`approve-plan` CLI, gate dispatch) and the dead null-kanban-scheduler subsystem (`PipelineRunner`, `--kanban null`, `merge`/`status`/`kill` CLI subcommands), plus the `in_flight_ids()` behavior change (no more file-marker fallback during kanban outages).

- [ ] **Step 6: Verify sync across all 4 files**

```bash
cat VERSION
grep '^version' pyproject.toml
grep -A1 'name = "hermes-pipeline"' uv.lock
head -10 CHANGELOG.md
```
Confirm `VERSION`, `pyproject.toml`, and `uv.lock`'s `hermes-pipeline` entry all show the identical 3-digit number, and `CHANGELOG.md` has an entry for that exact version.

- [ ] **Step 7: Run the full test suite one final time**

```bash
uv run pytest
```
Expected: all tests pass, zero import errors.

- [ ] **Step 8: Confirm Success Criteria from the design doc**

```bash
test -f hermes_pipeline/approve_plan.py && echo "FAIL: approve_plan.py still exists" || echo "OK: approve_plan.py deleted"
test -f hermes_pipeline/runner.py && echo "FAIL: runner.py still exists" || echo "OK: runner.py deleted"
test -f hermes_pipeline/watcher.py && echo "FAIL: watcher.py still exists" || echo "OK: watcher.py deleted"
grep -n "def run(\|_invoke_hermes\|_invoke_review_phase" hermes_pipeline/phases.py && echo "FAIL: phases.py still has null-mode functions" || echo "OK: phases.py clean"
grep -n "approve-plan\|\"merge\"\|\"kill\"\|\"status\"" hermes_pipeline/cli.py | grep -v "^#" && echo "CHECK manually — grep hit, verify not a false positive" || echo "OK: cli.py subcommands removed"
grep -n "PLAN_GATE_PHASE_KEY\|maybe_plan_gate_ready" hermes_pipeline/gates.py && echo "FAIL: gates.py still has plan-gate code" || echo "OK: gates.py clean"
test -f hermes_pipeline/state.py && grep -c "ReadyForReview\|read_ready_for_review\|write_ready_for_review\|list_ready_for_review_pending" hermes_pipeline/state.py
```

Note on the last check: per this session's research, `state.py`'s `ReadyForReview` machinery is still written by a surviving terminal-phase code path in `phases.py` (not the deleted `run()`/marker machinery) — if the grep count is nonzero, treat that as expected and confirm by reading the calling code, rather than treating it as a plan violation. If research and code disagree, trust the code: re-run the design doc's original claim ("written only by phases.py:342 and runner.py:301, both deleted") against the current file before deciding whether `state.py` needs further edits. If `ReadyForReview` genuinely has zero remaining writers after this PR's deletions, delete it and its dedicated test cases in a follow-up step here; otherwise leave it — this plan does not force a state.py deletion that contradicts what Task 4-5's actual diffs left behind.

- [ ] **Step 9: Commit**

```bash
git add VERSION pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore: bump version and changelog for plan-gate + null-scheduler deletion"
```

---

## Self-Review Notes

- **Spec coverage:** All Success Criteria from the design doc are covered — Task 2/5 (module deletions), Task 4 (phases.py), Task 5 (harness.py single kanban mode), Task 1 (cli.py subcommands), Task 6 (gates.py/gate_state.py/kanban_tasks.py), Task 9 Step 8 (state.py verification), Task 1/2/3/5/6/7 (test file deletion/pruning), Task 9 Step 7 (full suite green), Task 9 Steps 1-6 (version sync).
- **`state.py` caveat:** Task 9 Step 8 flags that the design doc's original claim about `ReadyForReview`'s writers may be stale — the research subagent found it's also written by a surviving terminal-phase path, not just the deleted `run()`/`runner.py`. This plan does not force a deletion the actual code doesn't support; it directs the implementer to verify against the real diff state at that point, consistent with "trust the code over old research."
- **Ordering rationale:** cli.py (Task 1) goes first so no downstream module in Tasks 2-7 still has a live caller when it's deleted — this matches the design doc's Approach A "bottom-up dependency order" from the CLI surface inward.
- **Behavior change isolated:** Task 7 is the only task with a genuine behavior change (`in_flight_ids()` fallback removal) — called out explicitly in its commit message per the design doc's own framing, not buried alongside pure deletions.
