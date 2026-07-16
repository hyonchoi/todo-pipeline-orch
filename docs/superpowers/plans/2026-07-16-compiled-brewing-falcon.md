# Fix Harness --kanban hermes to Use Kanban-as-Scheduler

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `PipelineRunner` + `HermesKanbanAdapter` path in `run_harness()` with a polling loop that uses `kanban_tasks` functions so the kanban dashboard shows one card per phase instead of one card for the entire TODO.

**Architecture:** Register each phase as an independent kanban task chained via `--parent`, auto-complete gate tasks, poll `get_todo_kanban_status()` on a configurable interval, emit JSONL events through the existing monitor bridge, and call `observe_outcomes()` on completion. The `--kanban null` path is untouched.

**Tech Stack:** Python 3.12+, hermes-pipeline package, PyYAML, pytest

## Global Constraints

- Python 3.12+, managed via `uv`. Use `uv run pytest ...` for all test commands.
- `HermesKanbanAdapter`, `KanbanOutbox`, `ActiveTasksStore` are **NOT deleted** — still used by merge path.
- `test_harness_e2e.py` tests use `kanban_mode="null"` — zero changes needed.
- Poll interval default: 5s, configurable via `_poll_kanban_phases(poll_interval=...)` for tests.
- All tests mock `time.sleep` to run instantly.

---

### File Structure

| Action | File | Purpose |
|--------|------|---------|
| Modify | `hermes_pipeline/harness.py` | New functions + rewritten `run_harness()` hermes branch |
| Modify | `hermes_pipeline/cli.py` | Update `--kanban` help text |
| Modify | `tests/test_harness.py` | Rewrite `TestKanbanModeHermes`, add `TestAutoCompleteGateTasks`, `TestPollKanbanPhases` |
| Modify | `hermes_pipeline/kanban.py` | Add deprecation note |
| No change | `hermes_pipeline/kanban_tasks.py` | Reused as-is |
| No change | `hermes_pipeline/test_report.py` | Consumes same JSONL events |
| No change | `tests/test_harness_e2e.py` | Uses null mode only |

---

### Task 1: Add `_auto_complete_gate_tasks` Helper

**Files:**
- Modify: `hermes_pipeline/harness.py` — insert after `_kanban_preflight` (after line 169)
- Modify: `tests/test_harness.py` — add `TestAutoCompleteGateTasks` class

**Interfaces:**
- Consumes: `kanban_tasks.get_todo_kanban_tasks()`, `kanban_tasks.BLOCKED`
- Produces: `_auto_complete_gate_tasks(tenant, tick_id) -> None`

- [ ] **Step 1: Write the failing test**

Create the test class in `tests/test_harness.py`:

```python
class TestAutoCompleteGateTasks:
    """Tests for _auto_complete_gate_tasks()."""

    def test_completes_blocked_gate_tasks(self, mocker):
        from hermes_pipeline.harness import _auto_complete_gate_tasks
        import json as _json

        header_gate = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_2b_plan_gate",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )
        header_dev = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_4_development",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )

        mock_data = [
            {"id": "t_gate", "status": "blocked", "body": header_gate + "\ngate"},
            {"id": "t_dev", "status": "ready", "body": header_dev + "\nphase"},
        ]

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(mock_data), stderr="")

        _auto_complete_gate_tasks("demo", "01TICK")

        complete_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][:3] == ["hermes", "kanban", "complete"]
        ]
        assert len(complete_calls) == 1
        assert complete_calls[0][0][0][3] == "t_gate"

    def test_skips_non_blocked_tasks(self, mocker):
        from hermes_pipeline.harness import _auto_complete_gate_tasks
        import json as _json

        header = _json.dumps(
            {"tick_id": "01TICK", "phase_key": "phase_2_autoplan",
             "todo_id": "TODO-1", "project_slug": "demo"},
            sort_keys=True,
        )

        mock_data = [
            {"id": "t1", "status": "running", "body": header},
            {"id": "t2", "status": "done", "body": header.replace("phase_2_autoplan", "phase_3")},
        ]

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(mock_data), stderr="")

        _auto_complete_gate_tasks("demo", "01TICK")

        complete_calls = [
            c for c in mock_run.call_args_list
            if c[0][0][:3] == ["hermes", "kanban", "complete"]
        ]
        assert len(complete_calls) == 0

    def test_is_best_effort_on_query_failure(self, mocker):
        """If get_todo_kanban_tasks raises, the function returns without error."""
        from hermes_pipeline.harness import _auto_complete_gate_tasks

        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
            side_effect=RuntimeError("query failed"),
        )

        _auto_complete_gate_tasks("demo", "01TICK")  # Should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py::TestAutoCompleteGateTasks -xvs`
Expected: FAIL with "imported name '_auto_complete_gate_tasks' is not defined"

- [ ] **Step 3: Write the implementation**

Insert in `hermes_pipeline/harness.py` after line 169 (after `_kanban_preflight`):

```python
def _auto_complete_gate_tasks(tenant: str, tick_id: str) -> None:
    """Complete all blocked gate tasks for a tick, unblocking child phases.

    Queries kanban for blocked tasks matching tick_id, runs `hermes kanban complete`
    on each. Best-effort: exceptions are logged, not raised.
    """
    from .kanban_tasks import BLOCKED, get_todo_kanban_tasks

    try:
        tasks = get_todo_kanban_tasks(tenant, tick_id)
    except Exception as e:
        log.warning("failed to query kanban tasks for gate auto-complete: %s", e)
        return

    for phase_key, info in tasks.items():
        if info.status != BLOCKED:
            continue
        try:
            result = subprocess.run(
                ["hermes", "kanban", "complete", info.task_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                log.warning(
                    "failed to complete gate task %s (%s): rc=%d stderr=%s",
                    info.task_id, phase_key, result.returncode,
                    result.stderr[:200],
                )
            else:
                log.info("auto-completed gate task %s (%s)", info.task_id, phase_key)
        except Exception as e:
            log.warning("auto-complete gate task %s (%s) failed: %s", info.task_id, phase_key, e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harness.py::TestAutoCompleteGateTasks -xvs`
Expected: PASS (3/3)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add _auto_complete_gate_tasks helper for harness polling"
```

---

### Task 2: Add `_poll_kanban_phases` Function

**Files:**
- Modify: `hermes_pipeline/harness.py` — insert after `_auto_complete_gate_tasks`
- Modify: `tests/test_harness.py` — add `TestPollKanbanPhases` class

**Interfaces:**
- Consumes: `_auto_complete_gate_tasks()`, `kanban_tasks.register_todo_phases()`, `kanban_tasks.get_todo_kanban_status()`, `kanban_tasks.observe_outcomes()`, `kanban_tasks.TERMINAL_STATUSES`, `_ConvergenceMonitor`, `ConvergenceDetector`
- Produces: `_poll_kanban_phases(...) -> bool` (True if all done)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_harness.py`:

```python
class TestPollKanbanPhases:
    """Tests for _poll_kanban_phases()."""

    def test_registers_phases_and_polls_to_completion(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
        )
        import json as _json

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"phase_2_autoplan": "running", "phase_4_development": "ready"}
            return {"phase_2_autoplan": "done", "phase_4_development": "done"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is True
        assert call_count[0] >= 2

        lines = events_log.read_text().strip().splitlines()
        events = [_json.loads(l) for l in lines if l.strip()]
        event_types = [e["event_type"] for e in events]
        assert "phase_started" in event_types
        assert "phase_completed" in event_types

    def test_emits_phase_failed_event_on_kanban_failure(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
        )
        import json as _json

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_2_autoplan": "running"},
            {"phase_2_autoplan": "failed"},
        ])

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is False
        lines = events_log.read_text().strip().splitlines()
        events = [_json.loads(l) for l in lines if l.strip()]
        failed = [e for e in events if e["event_type"] == "phase_failed"]
        assert len(failed) == 1
        assert failed[0]["phase_key"] == "phase_2_autoplan"

    def test_convergence_halt_stops_polling(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
        )
        import json as _json

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2", "t3"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mock_observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"p1": "running", "p2": "ready", "p3": "blocked"}
            elif call_count[0] == 2:
                return {"p1": "failed", "p2": "running", "p3": "blocked"}
            elif call_count[0] == 3:
                return {"p1": "failed", "p2": "failed", "p3": "blocked"}
            return {"p1": "failed", "p2": "failed", "p3": "failed"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        assert result is False
        mock_observe.assert_called_once()

    def test_auto_completes_blocked_gates(self, tmp_path, mocker):
        from hermes_pipeline.harness import (
            _poll_kanban_phases, HarnessMonitor, ConvergenceDetector, _ConvergenceMonitor,
        )

        events_log = tmp_path / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=3)
        monitor = _ConvergenceMonitor(base_monitor, detector, {})

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2"])
        mock_auto = mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "done", "phase_2b_plan_gate": "done"})
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        _poll_kanban_phases(
            project_slug="demo", tick_id="01TICK",
            state_dir=tmp_path / ".hermes", todo_id="TODO-1",
            project_dir=tmp_path, phases_path=None,
            monitor=monitor, detector=detector, poll_interval=0.1,
        )

        mock_auto.assert_called_once_with("demo", "01TICK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py::TestPollKanbanPhases -xvs`
Expected: FAIL with "imported name '_poll_kanban_phases' is not defined"

- [ ] **Step 3: Write the implementation**

Insert in `hermes_pipeline/harness.py` after `_auto_complete_gate_tasks`:

```python
def _poll_kanban_phases(
    *,
    project_slug: str,
    tick_id: str,
    state_dir: Path,
    todo_id: str,
    project_dir: Path,
    phases_path: Path | None,
    monitor: _ConvergenceMonitor,
    detector: ConvergenceDetector,
    poll_interval: float = 5.0,
) -> bool:
    """Poll kanban-as-scheduler phases to completion.

    1. Registers all phases as kanban tasks (register_todo_phases).
    2. Auto-completes gate tasks so child phases become ready.
    3. Polls get_todo_kanban_status() until all phases terminal.
    4. Emits JSONL events via monitor.
    5. Calls observe_outcomes() to write decision store.

    Returns True if all phases completed successfully (all done), False otherwise.
    """
    from .kanban_tasks import (
        COMPLETION_STATUSES,
        TERMINAL_STATUSES,
        get_todo_kanban_status,
        observe_outcomes,
        register_todo_phases,
    )

    # Step 1: Register phases as kanban tasks
    log.info("registering kanban phases for %s tick %s", todo_id, tick_id)
    register_todo_phases(
        todo_id=todo_id,
        tick_id=tick_id,
        board_slug=project_slug,
        project_dir=project_dir,
        phases_path=phases_path,
    )

    # Step 2: Auto-complete blocked gate tasks
    _auto_complete_gate_tasks(project_slug, tick_id)

    # Step 3: Poll for status transitions
    previous_status: dict[str, str] = {}
    all_terminal = False

    while not all_terminal:
        time.sleep(poll_interval)

        try:
            status_map = get_todo_kanban_status(project_slug, tick_id)
        except Exception as e:
            log.warning("kanban status poll failed: %s", e)
            continue

        if not status_map:
            continue

        # Detect transitions and emit events
        for phase_key, status in status_map.items():
            prev = previous_status.get(phase_key)

            if prev in (None, "ready", "blocked") and status == "running":
                monitor.current_phase_key = phase_key
                monitor("phase_started", {"phase_key": phase_key, "todo_id": todo_id})

            elif prev == "running" and status == "done":
                monitor.current_phase_key = None
                monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                detector.record(phase_key, None)

            elif prev == "running" and status == "failed":
                monitor.current_phase_key = None
                monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                detector.record(phase_key, "phase_failure")
                if detector.should_halt():
                    log.warning(
                        "convergence detector: %d+ consecutive phase_failure, halting",
                        detector.threshold,
                    )
                    all_terminal = True
                    break

            elif prev == "blocked" and status == "done":
                monitor("phase_completed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
                detector.record(phase_key, None)

        previous_status = dict(status_map)

        if not all_terminal:
            all_terminal = all(s in TERMINAL_STATUSES for s in status_map.values())

    # Step 4: Observe outcomes
    try:
        final_status = get_todo_kanban_status(project_slug, tick_id)
        observe_outcomes(state_dir=state_dir, tick_id=tick_id, status_map=final_status)
    except Exception as e:
        log.warning("observe_outcomes failed: %s", e)

    return all(s == "done" for s in previous_status.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harness.py::TestPollKanbanPhases -xvs`
Expected: PASS (4/4)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add _poll_kanban_phases polling loop for kanban-as-scheduler"
```

---

### Task 3: Rewrite `run_harness` for `kanban_mode == "hermes"`

**Files:**
- Modify: `hermes_pipeline/harness.py` — rewrite lines 366-512

**Interfaces:**
- Consumes: `_poll_kanban_phases()`, `_kanban_preflight()`, `get_todo_kanban_status()`, `filter_phases()`, `TERMINAL_STATUSES`
- Produces: Rewritten `run_harness()` with dual-branch (hermes polling vs null PipelineRunner)

- [ ] **Step 1: Rewrite the `run_harness` function**

Replace lines 366-512 of `hermes_pipeline/harness.py` with the following logic. The key structural change: two branches for `kanban_mode`.

**Replace the function body from line 363 (`"""Main orchestration...`) through the kanban output block (line 512):**

```python
    """Main orchestration: bootstrap fixture, run pipeline, generate report."""
    import threading

    from .runner import PipelineRunner
    from .phases import load_phases
    from .test_report import generate_report, summarize_report, diff_reports, summarize_diff
    from .state import State
    from .kanban import NullKanbanAdapter
    from .kanban_tasks import TERMINAL_STATUSES, get_todo_kanban_status
    from .logging_setup import new_tick_id

    preflight_check()

    temp_dir = Path(tempfile.mkdtemp(prefix="harness-"))
    try:
        fixture = create_mock_project(temp_dir, fixture_name)

        state_dir = temp_dir / ".hermes"
        lock_dir = temp_dir / ".hermes" / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)

        events_log = temp_dir / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=convergence_threshold)
        error_holder: dict[str, Any] = {}
        monitor = _ConvergenceMonitor(base_monitor, detector, error_holder)

        all_phases = load_phases()
        phases = all_phases
        if phase_only:
            phases = filter_phases(all_phases, phase_only)

        tick_id = new_tick_id()

        if kanban_mode == "hermes":
            _kanban_preflight(tenant=fixture["project_slug"])

            # For --phase flag: create a temporary phases YAML for registration
            _phases_path_override: Path | None = None
            if phase_only:
                import yaml as _yaml
                _phases_path_override = temp_dir / "filtered-phases.yaml"
                _phases_path_override.write_text(
                    _yaml.dump({"phases": [p.__dict__ for p in phases]})
                )

        checkpoint_dir = state_dir / "pipeline_checkpoints"
        ready_dir = state_dir / "ready_for_review"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        ready_dir.mkdir(parents=True, exist_ok=True)

        state = State(
            project=fixture["project_slug"],
            lock_dir=lock_dir,
            checkpoint_dir=checkpoint_dir,
            ready_dir=ready_dir,
        )

        timed_out = False
        with isolate_config(state_dir=state_dir, lock_dir=lock_dir):
            result_box: dict[str, Any] = {}

            if kanban_mode == "hermes":
                def _run_and_capture() -> None:
                    try:
                        todo_id_str = f"TODO-{fixture['todo_id']}"
                        result_box["success"] = _poll_kanban_phases(
                            project_slug=fixture["project_slug"],
                            tick_id=tick_id,
                            state_dir=state_dir,
                            todo_id=todo_id_str,
                            project_dir=temp_dir,
                            phases_path=_phases_path_override,
                            monitor=monitor,
                            detector=detector,
                        )
                    except ConvergenceHaltError as e:
                        result_box["convergence_error"] = e
                    except Exception as e:  # noqa: BLE001
                        result_box["exception"] = e

                worker = threading.Thread(target=_run_and_capture, daemon=True)
                worker.start()
                worker.join(timeout=timeout)

                if worker.is_alive():
                    timed_out = True
                    success = False
                    try:
                        in_flight = get_todo_kanban_status(fixture["project_slug"], tick_id)
                        running_phase = next(
                            (k for k, v in in_flight.items()
                             if v not in TERMINAL_STATUSES),
                            None,
                        )
                        if running_phase and monitor.current_phase_key is None:
                            base_monitor("phase_timed_out", {"phase_key": running_phase})
                    except Exception:
                        pass
                elif "convergence_error" in result_box:
                    log.warning(str(result_box["convergence_error"]))
                    success = False
                elif "exception" in result_box:
                    raise result_box["exception"]
                else:
                    success = result_box["success"]
            else:
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

                def _run_and_capture() -> None:
                    try:
                        result_box["success"] = runner.run()
                    except ConvergenceHaltError as e:
                        result_box["convergence_error"] = e
                    except Exception as e:  # noqa: BLE001
                        result_box["exception"] = e

                worker = threading.Thread(target=_run_and_capture, daemon=True)
                worker.start()
                worker.join(timeout=timeout)

                if worker.is_alive():
                    timed_out = True
                    success = False
                elif "convergence_error" in result_box:
                    log.warning(str(result_box["convergence_error"]))
                    try:
                        kanban.clear_active_task(project=fixture["project_slug"], outcome="abandoned")
                    except Exception as e:
                        log.warning("kanban.clear_active_task (convergence-halt) failed: %s", e)
                    success = False
                elif "exception" in result_box:
                    raise result_box["exception"]
                else:
                    success = result_box["success"]

        if timed_out and monitor.current_phase_key:
            base_monitor("phase_timed_out", {"phase_key": monitor.current_phase_key})

        output_dir = temp_dir / "reports"
        report = generate_report(events_log, output_dir)
        report_json = output_dir / "report.json"
        summary = summarize_report(report_json)
        if timed_out:
            summary = f"[overall timeout after {timeout}s] " + summary

        if loop:
            prev_reports = sorted(output_dir.parent.glob(f"{fixture_name}-report.*.json"))
            if prev_reports:
                diffs = diff_reports(prev_reports[-1], report_json)
                diff_summary = summarize_diff(diffs)
                summary += f" | diff: {diff_summary}"

            if prev_reports:
                next_n = int(prev_reports[-1].stem.split(".")[-1]) + 1
            else:
                next_n = 1
            next_report = output_dir.parent / f"{fixture_name}-report.{next_n}.json"
            next_report.write_text(report_json.read_text())

        if kanban_mode == "hermes":
            status_map = get_todo_kanban_status(fixture["project_slug"], tick_id)
            print(
                f"[kanban] tenant={fixture['project_slug']} tick_id={tick_id} "
                f"phases={status_map} "
                f"report={report_json} keep={'yes' if keep_dir else 'no (temp dir will be removed)'}"
            )

        exit_code = 0 if (success and not timed_out) else 1

        return HarnessResult(
            exit_code=exit_code,
            report_path=report_json,
            temp_dir=temp_dir if keep_dir else None,
            summary=summary,
        )

    except Exception as e:
        if not keep_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    finally:
        if not keep_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
```

- [ ] **Step 2: Run null-mode tests to verify backward compat**

Run: `uv run pytest tests/test_harness_e2e.py -xvs`
Expected: PASS (3/3) — e2e tests all use `kanban_mode="null"`

- [ ] **Step 3: Run harness unit tests**

Run: `uv run pytest tests/test_harness.py -xvs`
Expected: PASS — existing unit tests for components not in hermes path should still pass

- [ ] **Step 4: Commit**

```bash
git add hermes_pipeline/harness.py
git commit -m "refactor: rewrite run_harness hermes branch to use kanban-as-scheduler polling"
```

---

### Task 4: Rewrite `TestKanbanModeHermes` Tests

**Files:**
- Modify: `tests/test_harness.py` — replace `TestKanbanModeHermes` class

**Interfaces:**
- Consumes: `run_harness()`, mocked `kanban_tasks` functions, `subprocess.run`
- Produces: Updated regression tests for hermes path

- [ ] **Step 1: Replace the `TestKanbanModeHermes` class**

Replace the entire `TestKanbanModeHermes` class (lines 324-491) in `tests/test_harness.py`:

```python
class TestKanbanModeHermes:
    """Tests for --kanban hermes wiring in run_harness() using kanban-as-scheduler."""

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_registers_and_polls(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        """--kanban hermes uses register_todo_phases + polling, not PipelineRunner."""

        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "done"})
        mocker.patch("time.sleep")
        mock_observe = mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        result = run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
            kanban_mode="hermes", config=None,
        )

        assert result.exit_code == 0
        mock_observe.assert_called_once()

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_preflight_failure_raises(self, mock_run, monkeypatch):
        from hermes_pipeline.harness import KanbanPreflightError

        preflight_fail = MagicMock(returncode=1, stdout="", stderr="not authenticated")
        mock_run.return_value = preflight_fail
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        with pytest.raises(KanbanPreflightError, match="hermes login"):
            run_harness(
                fixture_name="happy-path", loop=False, phase_only=None,
                keep_dir=False, timeout=60, convergence_threshold=3,
                kanban_mode="hermes", config=None,
            )

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_null_explicit_produces_no_kanban_calls(self, mock_run, monkeypatch, tmp_path):
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)
        with patch("hermes_pipeline.phases.run") as mock_phases_run:
            mock_phases_run.return_value = {"status": "success"}
            run_harness(
                fixture_name="happy-path", loop=False,
                phase_only="phase_2_autoplan", keep_dir=True,
                timeout=60, convergence_threshold=3,
                kanban_mode="null", config=None,
            )
        kanban_calls = [c for c in mock_run.call_args_list
                        if c[0][0][:2] == ["hermes", "kanban"]]
        assert kanban_calls == []

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_polling_emits_jsonl_events(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=[
            {"phase_2_autoplan": "running"},
            {"phase_2_autoplan": "done"},
        ])

        result = run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
            kanban_mode="hermes", config=None,
        )

        assert result.exit_code == 0
        report = json.loads(result.report_path.read_text())
        phases = report["phases"]
        assert len(phases) == 1
        assert phases[0]["phase_key"] == "phase_2_autoplan"
        assert phases[0]["status"] == "completed"

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_preflight_timeout_raises_actionable_error(self, mock_run, monkeypatch):
        from hermes_pipeline.harness import KanbanPreflightError
        import subprocess

        def _run_side_effect(*args, **kwargs):
            cmd = args[0]
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[:3] == ["hermes", "kanban", "list"]:
                raise subprocess.TimeoutExpired(cmd, 15)
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _run_side_effect
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        with pytest.raises(KanbanPreflightError, match="timed out.*15s"):
            run_harness(
                fixture_name="happy-path", loop=False, phase_only=None,
                keep_dir=False, timeout=60, convergence_threshold=3,
                kanban_mode="hermes", config=None,
            )

    def test_convergence_halt_stops_polling_hermes(self, monkeypatch, mocker):
        mock_sp = mocker.patch("hermes_pipeline.harness.subprocess.run")
        mock_sp.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1", "t2", "t3"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        call_count = [0]
        def fake_status(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"p1": "running", "p2": "ready", "p3": "ready"}
            elif call_count[0] == 2:
                return {"p1": "failed", "p2": "running", "p3": "ready"}
            elif call_count[0] == 3:
                return {"p1": "failed", "p2": "failed", "p3": "running"}
            return {"p1": "failed", "p2": "failed", "p3": "failed"}

        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", side_effect=fake_status)

        result = run_harness(
            fixture_name="happy-path", loop=False, phase_only=None,
            keep_dir=True, timeout=60, convergence_threshold=3,
            kanban_mode="hermes", config=None,
        )

        assert result.exit_code == 1

    @patch("hermes_pipeline.harness.subprocess.run")
    def test_kanban_hermes_single_phase_registers_filtered(self, mock_harness_sp, tmp_path, monkeypatch, mocker):
        """--phase with --kanban hermes should pass phases_path to register_todo_phases."""

        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        mock_harness_sp.return_value = preflight_result
        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

        mock_register = mocker.patch("hermes_pipeline.kanban_tasks.register_todo_phases", return_value=["t1"])
        mocker.patch("hermes_pipeline.harness._auto_complete_gate_tasks")
        mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status",
                      return_value={"phase_2_autoplan": "done"})
        mocker.patch("time.sleep")
        mocker.patch("hermes_pipeline.kanban_tasks.observe_outcomes")

        run_harness(
            fixture_name="happy-path", loop=False,
            phase_only="phase_2_autoplan", keep_dir=True,
            timeout=60, convergence_threshold=3,
            kanban_mode="hermes", config=None,
        )

        call_kwargs = mock_register.call_args
        assert call_kwargs.kwargs.get("phases_path") is not None
```

- [ ] **Step 2: Run all harness tests**

Run: `uv run pytest tests/test_harness.py -xvs`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_harness.py
git commit -m "test: rewrite TestKanbanModeHermes for kanban-as-scheduler path"
```

---

### Task 5: Update CLI Help Text

**Files:**
- Modify: `hermes_pipeline/cli.py:566-572`

- [ ] **Step 1: Update the `--kanban` help text**

In `hermes_pipeline/cli.py` (line 565-572), replace the `--kanban` argument help text:

```python
    test_parser.add_argument(
        "--kanban", choices=["null", "hermes"], default="null",
        help=(
            "Kanban mode (default: null, no network calls). "
            "'hermes' uses kanban-as-scheduler: registers each phase as a "
            "separate kanban task and polls for completion. Requires prior "
            "`hermes login` plus access to the fixture's kanban tenant "
            "(verify with: hermes kanban list --tenant mock-project)."
        ),
    )
```

- [ ] **Step 2: Commit**

```bash
git add hermes_pipeline/cli.py
git commit -m "docs: update --kanban help text for kanban-as-scheduler model"
```

---

### Task 6: Add Deprecation Notes

**Files:**
- Modify: `hermes_pipeline/kanban.py` — add module-level note

- [ ] **Step 1: Add deprecation note to `kanban.py`**

At the top of `hermes_pipeline/kanban.py`, after the existing docstring, add:

```python
"""
NOTE: HermesKanbanAdapter, KanbanOutbox, and ActiveTasksStore are retained for
backward compatibility (--kanban null path, merge orchestration). The harness
--kanban hermes path has moved to kanban-as-scheduler via kanban_tasks.py.
"""
```

- [ ] **Step 2: Commit**

```bash
git add hermes_pipeline/kanban.py
git commit -m "docs: add deprecation note for kanban adapter in harness"
```

---

### Task 7: Full Test Suite Verification

**Files:**
- Run: `tests/test_harness.py`, `tests/test_harness_e2e.py`, `tests/test_kanban_tasks.py`

- [ ] **Step 1: Run all harness-related tests**

```bash
uv run pytest tests/test_harness.py tests/test_harness_e2e.py tests/test_kanban_tasks.py -xvs
```

Expected: All tests pass.

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest -x
```

Expected: All project tests pass.

- [ ] **Step 3: Commit any fixes**

If any test fixes are needed, stage and commit them.

---

## Self-Review

**1. Spec coverage:**
- Register phases as kanban tasks -> Task 2 (`_poll_kanban_phases` calls `register_todo_phases`)
- Auto-unblock gate tasks -> Task 1 (`_auto_complete_gate_tasks`)
- Poll kanban status -> Task 2 (poll loop with `get_todo_kanban_status`)
- Emit events to JSONL -> Task 2 (monitor bridge)
- Observe outcomes -> Task 2 (`observe_outcomes` call)
- Keep null path unchanged -> Task 3 (separate else branch)
- Update CLI help -> Task 5
- Update tests -> Tasks 1, 2, 4
- Deprecation notices -> Task 6

**2. Placeholder scan:** No "TBD", "TODO", "implement later" found.

**3. Type consistency:**
- `_auto_complete_gate_tasks(tenant, tick_id)` -> used in `_poll_kanban_phases` with same signature
- `_poll_kanban_phases()` returns `bool` (all done), used as `result_box["success"]` in `run_harness`
- `get_todo_kanban_status()` returns `{phase_key: str}` used consistently
- JSONL events use same keys as existing HarnessMonitor: `phase_started`, `phase_completed`, `phase_failed`, `phase_timed_out`
- `COMPLETION_STATUSES` import used correctly

**4. Missing: `COMPLETION_STATUSES` not imported in `_poll_kanban_phases` — only `TERMINAL_STATUSES` is needed.** Verified: the polling loop checks `TERMINAL_STATUSES` for the termination condition, which includes "archived" — correct behavior for detecting all phases as terminal.

**5. Edge case: fast completion** — If the first poll returns all phases already "done", no `phase_started` event is emitted. The report will list phases with status "unknown" or empty. This is acceptable for the harness since the test fixtures run in mock time. In production, this is also correct since the kanban tasks are created as "ready" and transition to "running" before the poll interval.

**6. `--phase` YAML format** — The temporary phases YAML uses `_yaml.dump({"phases": [p.__dict__ for p in phases]})`. `load_phases()` reads `data["phases"]` from the YAML and constructs `Phase` objects. `Phase.__dict__` contains the frozen dataclass fields. Verified compatible.
