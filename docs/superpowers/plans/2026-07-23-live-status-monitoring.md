# Live Status Monitoring for `pipeline-watch test` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pipeline-watch test` print phase status to the console as it runs, and stop the default `--timeout` from killing healthy long runs.

**Architecture:** Add `log.info()` calls directly inside `_poll_kanban_phases()`'s existing per-transition branches (harness.py:325-358) plus one initial status print right after registration (harness.py:298). Raise the `--timeout` CLI default in cli.py from 3600 to 86400. No new classes, no new modules — this is Approach A from the design doc (docs/gstack/hyonchoi-main-design-20260723-174708.md).

**Tech Stack:** Python 3.12+, stdlib `logging` (already configured unconditionally to stderr in `logging_setup.configure()`), pytest + `unittest.mock`/`pytest-mock` for tests.

## Global Constraints

- Scoped to `hermes_pipeline/harness.py` and `hermes_pipeline/cli.py` only — do not touch `pipeline-watch tick` / `register_todo_phases` call chain in `cli.py`'s production path.
- Do not change `_run_with_timeout`'s threading/cancellation semantics (harness.py:494-526) — the daemon thread stays un-cancelled on timeout; that is a pre-existing, explicitly out-of-scope gap.
- Console output uses `log.info()`, not `print()` — matches the existing pattern in this module (e.g. harness.py:284's `log.info("registering kanban phases for...")`); root logger's `StreamHandler(sys.stderr)` is unconditional so no `-v`/`--debug` flag is needed for these lines to reach the console.
- `--timeout` CLI flag must remain functional as an opt-in override — only its **default** changes (3600 → 86400).
- Every VERSION bump touches all 4 files together (VERSION, pyproject.toml, uv.lock, CHANGELOG.md) per this repo's CLAUDE.md — out of scope for this plan itself (ships via the normal release flow per the design doc's Distribution Plan), but do not bump VERSION as a side effect of any task below.

---

### Task 1: Console output on phase transitions in `_poll_kanban_phases`

**Files:**
- Modify: `hermes_pipeline/harness.py:325-358` (the `if/elif` transition chain inside `_poll_kanban_phases`)
- Test: `tests/test_harness.py` (new `TestPollKanbanPhasesConsoleOutput` class)

**Interfaces:**
- Consumes: `_poll_kanban_phases()`'s existing signature (harness.py:251-264) — no signature change. Consumes `log` (module-level `logging.getLogger(__name__)`, harness.py:18).
- Produces: no new public symbols. `_poll_kanban_phases` behavior is additive (extra `log.info()` calls); return value and side effects (monitor calls, gate auto-complete) are unchanged.

Current code at harness.py:321-358:

```python
        try:
            for phase_key, status in status_map.items():
                prev = previous_status.get(phase_key)

                if prev in (None, "ready", "blocked") and status == "running":
                    monitor.current_phase_key = phase_key
                    monitor("phase_started", {"phase_key": phase_key, "todo_id": todo_id})

                elif prev == "running" and status == "done":
                    monitor.current_phase_key = None
                    monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                    # Auto-complete any gate task whose predecessor just finished
                    _auto_complete_gate_tasks(
                        project_slug, tick_id, completed_phase_key=phase_key, phases=phases
                    )

                elif prev == "running" and status == "failed":
                    monitor.current_phase_key = None
                    # monitor() records the failure with the detector and raises
                    # ConvergenceHaltError itself if the threshold is tripped —
                    # see _ConvergenceMonitor.__call__. No separate detector.record()
                    # call is needed here.
                    monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})

                elif prev in (None, "ready", "blocked") and status == "done":
                    # Phase completed between polls without ever being observed
                    # as "running" (fast phase, coarse poll interval). Still
                    # emit the event and run gate auto-complete so downstream
                    # gates aren't left blocked.
                    monitor.current_phase_key = None
                    monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                    _auto_complete_gate_tasks(
                        project_slug, tick_id, completed_phase_key=phase_key, phases=phases
                    )

                elif prev in (None, "ready", "blocked") and status == "failed":
                    monitor.current_phase_key = None
                    monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
        except ConvergenceHaltError:
```

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_harness.py` (place it after `TestConvergenceMonitor`, before `TestRunHarnessTimeout`). It calls `_poll_kanban_phases` directly with mocked kanban functions, using `caplog` to assert on emitted log records instead of going through the full `run_harness()` stack:

```python
class TestPollKanbanPhasesConsoleOutput:
    """Each phase transition must be logged to the console (via log.info), not just
    written to events.jsonl, so `pipeline-watch test` is no longer silent mid-run."""

    def _run_poll(self, monkeypatch, mocker, status_sequence, tmp_path):
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        monkeypatch.setattr("hermes_pipeline.harness.time.sleep", lambda *_a, **_kw: None)
        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            side_effect=status_sequence,
        )

        log_path = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(log_path)
        detector = ConvergenceDetector(threshold=99)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        return _poll_kanban_phases(
            project_slug="proj",
            tick_id="tick-1",
            state_dir=tmp_path,
            todo_id="TODO-30",
            project_dir=tmp_path,
            phases_path=None,
            monitor=monitor,
            detector=detector,
            poll_interval=0.0,
            max_poll_interval=0.0,
        )

    def test_none_to_running_logs_phase_start(self, monkeypatch, mocker, tmp_path, caplog):
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "running"},
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        assert any("p1" in r.message and "running" in r.message for r in caplog.records)

    def test_running_to_done_logs_completion(self, monkeypatch, mocker, tmp_path, caplog):
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "running"},
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        assert any("p1" in r.message and "done" in r.message for r in caplog.records)

    def test_running_to_failed_logs_failure(self, monkeypatch, mocker, tmp_path, caplog):
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "running"},
                {"p1": "failed"},
                {"p1": "failed"},
            ],
        )
        assert any("p1" in r.message and "failed" in r.message for r in caplog.records)

    def test_fast_phase_none_to_done_still_logs(self, monkeypatch, mocker, tmp_path, caplog):
        """Phase finishes between polls without ever being observed as 'running'."""
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        assert any("p1" in r.message and "done" in r.message for r in caplog.records)

    def test_fast_phase_none_to_failed_still_logs(self, monkeypatch, mocker, tmp_path, caplog):
        """Phase fails between polls without ever being observed as 'running'."""
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "failed"},
                {"p1": "failed"},
            ],
        )
        assert any("p1" in r.message and "failed" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness.py::TestPollKanbanPhasesConsoleOutput -v`
Expected: All 5 tests FAIL (no `log.info` calls exist yet in the transition branches, so `caplog.records` is empty and the `any(...)` assertions are `False`).

- [ ] **Step 3: Add `log.info()` calls to each transition branch**

Replace harness.py:321-358 with:

```python
        try:
            for phase_key, status in status_map.items():
                prev = previous_status.get(phase_key)

                if prev in (None, "ready", "blocked") and status == "running":
                    log.info("phase %s: %s -> running", phase_key, prev or "none")
                    monitor.current_phase_key = phase_key
                    monitor("phase_started", {"phase_key": phase_key, "todo_id": todo_id})

                elif prev == "running" and status == "done":
                    log.info("phase %s: running -> done", phase_key)
                    monitor.current_phase_key = None
                    monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                    # Auto-complete any gate task whose predecessor just finished
                    _auto_complete_gate_tasks(
                        project_slug, tick_id, completed_phase_key=phase_key, phases=phases
                    )

                elif prev == "running" and status == "failed":
                    log.info("phase %s: running -> failed", phase_key)
                    monitor.current_phase_key = None
                    # monitor() records the failure with the detector and raises
                    # ConvergenceHaltError itself if the threshold is tripped —
                    # see _ConvergenceMonitor.__call__. No separate detector.record()
                    # call is needed here.
                    monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})

                elif prev in (None, "ready", "blocked") and status == "done":
                    # Phase completed between polls without ever being observed
                    # as "running" (fast phase, coarse poll interval). Still
                    # emit the event and run gate auto-complete so downstream
                    # gates aren't left blocked.
                    log.info("phase %s: %s -> done", phase_key, prev or "none")
                    monitor.current_phase_key = None
                    monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                    _auto_complete_gate_tasks(
                        project_slug, tick_id, completed_phase_key=phase_key, phases=phases
                    )

                elif prev in (None, "ready", "blocked") and status == "failed":
                    log.info("phase %s: %s -> failed", phase_key, prev or "none")
                    monitor.current_phase_key = None
                    monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
        except ConvergenceHaltError:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness.py::TestPollKanbanPhasesConsoleOutput -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Run the full harness test suite to check for regressions**

Run: `uv run pytest tests/test_harness.py -v`
Expected: All tests PASS (including the pre-existing `TestKanbanModeHermes` and `TestConvergenceMonitor` classes, which exercise the same branches indirectly).

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: log phase transitions to console in poll loop"
```

---

### Task 2: Initial status table after registration + registration-failure test

**Files:**
- Modify: `hermes_pipeline/harness.py:290-298` (immediately after `register_todo_phases()` returns)
- Test: `tests/test_harness.py` (extend `TestPollKanbanPhasesConsoleOutput`)

**Interfaces:**
- Consumes: `register_todo_phases()`'s return value (list of task IDs, per `mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])` in existing tests) and `get_todo_kanban_status(project_slug, tick_id)` (harness.py:313, already imported at harness.py:277).
- Produces: no new public symbols — one additional `log.info()` call inside `_poll_kanban_phases`, placed after registration and before the polling `while` loop.

Current code at harness.py:290-304:

```python
    log.info("registering kanban phases for %s tick %s (assignee=%s)", todo_id, tick_id, assignee)
    register_todo_phases(
        todo_id=todo_id,
        tick_id=tick_id,
        board_slug=project_slug,
        project_dir=project_dir,
        phases_path=phases_path,
        assignee=assignee,
    )

    # Gate tasks will be auto-completed when their parent phase finishes,
    # not at registration time — this ensures parent output exists before
    # child phases can start.

    previous_status: dict[str, str] = {}
```

- [ ] **Step 1: Write the failing tests**

Add these two tests to `TestPollKanbanPhasesConsoleOutput` in `tests/test_harness.py`:

```python
    def test_initial_status_table_prints_after_registration(self, monkeypatch, mocker, tmp_path, caplog):
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "ready"},
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        assert any("initial phase status" in r.message.lower() for r in caplog.records)

    def test_initial_status_table_prints_before_any_transition(self, monkeypatch, mocker, tmp_path, caplog):
        """Even if the first poll already shows a phase terminal, the initial table
        must have printed first."""
        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        self._run_poll(
            monkeypatch, mocker, tmp_path=tmp_path,
            status_sequence=[
                {"p1": "done"},
                {"p1": "done"},
            ],
        )
        messages = [r.message.lower() for r in caplog.records]
        initial_idx = next(i for i, m in enumerate(messages) if "initial phase status" in m)
        transition_idx = next(i for i, m in enumerate(messages) if "p1" in m and "-> done" in m)
        assert initial_idx < transition_idx

    def test_registration_failure_emits_no_transition_logs(self, monkeypatch, mocker, tmp_path, caplog):
        """If register_todo_phases() raises, the poll loop must never start —
        matches today's behavior, no new failure path."""
        from hermes_pipeline.harness import (
            ConvergenceDetector,
            HarnessMonitor,
            _ConvergenceMonitor,
            _poll_kanban_phases,
        )

        caplog.set_level("INFO", logger="hermes_pipeline.harness")
        monkeypatch.setattr("hermes_pipeline.harness.time.sleep", lambda *_a, **_kw: None)
        mocker.patch(
            "hermes_pipeline.kanban_tasks.register_todo_phases",
            side_effect=RuntimeError("boom"),
        )
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status")

        log_path = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(log_path)
        detector = ConvergenceDetector(threshold=99)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        with pytest.raises(RuntimeError, match="boom"):
            _poll_kanban_phases(
                project_slug="proj",
                tick_id="tick-1",
                state_dir=tmp_path,
                todo_id="TODO-30",
                project_dir=tmp_path,
                phases_path=None,
                monitor=monitor,
                detector=detector,
                poll_interval=0.0,
                max_poll_interval=0.0,
            )

        assert not any("initial phase status" in r.message.lower() for r in caplog.records)
        assert not any("->" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness.py::TestPollKanbanPhasesConsoleOutput -v`
Expected: `test_initial_status_table_prints_after_registration` and `test_initial_status_table_prints_before_any_transition` FAIL (no "initial phase status" log exists yet). `test_registration_failure_emits_no_transition_logs` PASSES already (no code path change needed for it — this confirms today's no-new-failure-path behavior before you touch anything).

- [ ] **Step 3: Add the initial status print**

Replace harness.py:290-304 with:

```python
    log.info("registering kanban phases for %s tick %s (assignee=%s)", todo_id, tick_id, assignee)
    register_todo_phases(
        todo_id=todo_id,
        tick_id=tick_id,
        board_slug=project_slug,
        project_dir=project_dir,
        phases_path=phases_path,
        assignee=assignee,
    )

    initial_status = get_todo_kanban_status(project_slug, tick_id)
    log.info(
        "initial phase status: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(initial_status.items())) or "(none)",
    )

    # Gate tasks will be auto-completed when their parent phase finishes,
    # not at registration time — this ensures parent output exists before
    # child phases can start.

    previous_status: dict[str, str] = {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness.py::TestPollKanbanPhasesConsoleOutput -v`
Expected: All tests in the class PASS.

- [ ] **Step 5: Run the full harness test suite to check for regressions**

Run: `uv run pytest tests/test_harness.py -v`
Expected: All tests PASS. In particular, confirm `TestKanbanModeHermes::test_kanban_hermes_registers_and_polls` and `test_kanban_hermes_polling_emits_jsonl_events` still pass — both now cause `get_todo_kanban_status` to be called one extra time (once for the initial print, before the poll loop). Both tests already mock `get_todo_kanban_status` with either a fixed `return_value` (unaffected by extra calls) or a `side_effect` list — if either test starts failing with `StopIteration`, that means its `side_effect` list of return values needs one more leading entry for this new upfront call; add it and re-run.

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: print initial phase status table after registration"
```

---

### Task 3: Raise `--timeout` CLI default and verify the regression test

**Files:**
- Modify: `hermes_pipeline/cli.py:320-323`
- Test: `tests/test_harness.py:240-271` (`TestRunHarnessTimeout`, verify unmodified — no new test needed, this is a regression check)
- Test: new CLI-level test in `tests/test_cli.py` if that file exists, else added to `tests/test_harness.py`

**Interfaces:**
- Consumes: `argparse.ArgumentParser.add_argument`'s existing `--timeout` registration (cli.py:320-323).
- Produces: no new public symbols. `args.timeout` (cli.py:1191) still flows into `run_harness(timeout=...)` unchanged — only the parser's `default=` value changes.

Current code at cli.py:320-323:

```python
    test_parser.add_argument(
        "--timeout", type=int, default=3600,
        help="Overall run timeout in seconds (default: 60min)",
    )
```

- [ ] **Step 1: Check whether a CLI parser test file exists**

Run: `ls tests/test_cli.py 2>/dev/null && grep -n "def test.*timeout\|parse_args" tests/test_cli.py | head -20`

If `tests/test_cli.py` exists and has a pattern for testing parsed defaults, use that file for Step 2. Otherwise, add the test to `tests/test_harness.py`.

- [ ] **Step 2: Write the failing test**

Add this test (to `tests/test_cli.py` if it exists with a parser-building helper already used by other tests, otherwise to `tests/test_harness.py` near the top-level, outside any class):

```python
def test_test_subcommand_timeout_default_is_86400():
    """--timeout must default large enough that it stops being the de-facto
    kill switch for healthy long test runs (raised from 3600s / 1h)."""
    from hermes_pipeline.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path"])
    assert args.timeout == 86400
```

If `hermes_pipeline.cli` does not expose a `build_parser()` function, first run:

Run: `grep -n "^def \|ArgumentParser(" /Users/hyonchoi/Personal/todo-pipeline-orchestrator/hermes_pipeline/cli.py | grep -i pars`

and use whatever function actually constructs and returns the parser (adjust the import and call in the test above to match). Do not guess a name that doesn't exist in the file.

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_harness.py::test_test_subcommand_timeout_default_is_86400 -v` (or the equivalent path if placed in `tests/test_cli.py`)
Expected: FAIL — `assert 3600 == 86400`.

- [ ] **Step 4: Raise the default**

Replace cli.py:320-323 with:

```python
    test_parser.add_argument(
        "--timeout", type=int, default=86400,
        help="Overall run timeout in seconds (default: 86400 = 24h)",
    )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_harness.py::test_test_subcommand_timeout_default_is_86400 -v`
Expected: PASS.

- [ ] **Step 6: Verify the pre-existing timeout regression test still passes unmodified**

Run: `uv run pytest tests/test_harness.py::TestRunHarnessTimeout -v`
Expected: The one non-skipped test in this class (if any — `test_hung_phase_times_out_and_reports_partial_progress` is currently `@pytest.mark.skip`'d per harness.py test file) is unaffected, since it calls `run_harness(timeout=1, ...)` directly and bypasses the CLI parser default entirely. Confirm: `uv run pytest tests/test_harness.py::TestRunHarnessTimeout -v -rs` shows the skip reason unchanged (`"phases.run deleted in Task 4; restored when Task 5 rewrites harness dispatch"`) — this plan does not touch that skip.

- [ ] **Step 7: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS (or skip, matching pre-existing skips).

- [ ] **Step 8: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_harness.py
git commit -m "fix: raise pipeline-watch test --timeout default from 1h to 24h"
```

(If the test was placed in `tests/test_cli.py` instead, `git add` that file in place of `tests/test_harness.py` for this commit.)

---

### Task 4: CHANGELOG entry (no version bump)

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing — documentation only.

This plan does **not** bump VERSION (per Global Constraints and the design doc's Distribution Plan — this ships via the project's normal release flow, not as part of this feature branch). Do not touch VERSION, pyproject.toml, or uv.lock in this task. Add an `### Unreleased` or top-of-file note so the next real version bump has the entry ready; if `CHANGELOG.md` already has an `## [Unreleased]` section, add to it instead of creating a new one.

- [ ] **Step 1: Read the top of CHANGELOG.md to match its existing format**

Run: `head -20 /Users/hyonchoi/Personal/todo-pipeline-orchestrator/CHANGELOG.md`

- [ ] **Step 2: Add an entry**

If an `## [Unreleased]` heading already exists at the top, add these two bullets under it. If not, insert a new `## [Unreleased]` section above the most recent `## [X.Y.Z]` entry with the same bullets:

```markdown
- `pipeline-watch test` now logs each phase transition (running/done/failed) and an
  initial phase status table to the console via `log.info()`, instead of writing
  silently to `events.jsonl` only. (TODO-30)
- Raised the `--timeout` default for `pipeline-watch test` from 3600s (1h) to 86400s
  (24h) so healthy long test runs are no longer killed by the default. The flag still
  works as an explicit override. (TODO-30)
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for live status monitoring (TODO-30)"
```

---

## Self-Review Notes

- **Spec coverage:** Success Criteria's 3 bullets map to Task 1 (per-transition logging), Task 2 (initial table + registration-failure no-op), and Task 3 (timeout default) respectively. All 7 branch/regression cases from the design doc's Next Steps §4 are covered: the 5 transition branches in Task 1's tests, the initial-table-before-first-transition and registration-failure cases in Task 2, and the `TestRunHarnessTimeout` regression check in Task 3.
- **No placeholders:** every step shows exact code, exact file/line ranges, exact commands with expected output.
- **Type/signature consistency:** `_poll_kanban_phases` keyword-only signature (harness.py:251-264) is reused verbatim across Task 1 and Task 2's tests; no new functions or dataclasses are introduced, so there's no cross-task signature drift to check.
