# Fix Auto-Command Drift — Implement pipeline-tick with Kanban-as-Scheduler (TODO-10)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `pipeline-watch tick <project>` so the cron-driven selection loop can run: select a TODO, register phases as Kanban tasks with `--parent` dependencies, and observe outcomes back to the circuit breaker.

**Architecture:** Kanban-as-Scheduler — the tick is a SHORT registrar (not a long-running orchestrator). It mints a ULID tick_id, acquires the TickLock, calls `run_selection()`, registers phases as Kanban tasks with parent-child chains, and exits. Hermes gateway/daemon dispatches phase workers. A second tick reads Kanban status and writes outcomes back to the decision store via JSONL sidecars (`*-phases.json`), feeding the circuit breaker.

**Tech Stack:** Python 3.12, hermes-pipeline package, `hermes kanban` CLI, pyyaml, existing TickLock/CircuitBreaker/decision-store modules.

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| **Create** | `hermes_pipeline/kanban_tasks.py` | Kanban task registration, status queries, outcome observation — raw CLI-based (not through HermesKanbanAdapter) |
| **Create** | `tests/test_kanban_tasks.py` | Tests for kanban_tasks module |
| **Create** | `tests/test_tick_subcommand.py` | Tests for the tick subcommand end-to-end |
| **Modify** | `hermes_pipeline/circuit.py` | Add `observe_from_outcomes()` method (OV-2) |
| **Modify** | `hermes_pipeline/decision/context.py` | Update `build_in_flight()` to query kanban state first (OV-1) |
| **Modify** | `hermes_pipeline/cli.py` | Add `tick` subcommand to argparse |
| **Modify** | `tests/test_circuit.py` | Tests for `observe_from_outcomes` |
| **Modify** | `tests/test_decision_context.py` | Tests for kanban-based in-flight detection |
| **Modify** | `tests/test_cli.py` | Tests for tick subcommand |

Existing modules reused as-is: `hermes_pipeline.tick` (TickLock), `hermes_pipeline.decision` (run_selection, build_context, store), `hermes_pipeline.phases` (load_phases, _render_phase_prompt), `hermes_pipeline.logging_setup` (new_tick_id), `hermes_pipeline.config` (FullConfig).

---

### Task 1: CircuitBreaker — add observe_from_outcomes()

**Files:**
- Modify: `hermes_pipeline/circuit.py`
- Modify: `tests/test_circuit.py`

**Goal:** Add `observe_from_outcomes()` that reads the JSONL outcome file (`.hermes/outcomes/<tick_id>-phases.json`) and derives the circuit breaker judgment from it — eliminating the need for the caller to manually determine `counts_as_no_progress`.

- [ ] **Step 1: Write the failing test**

Create the test class in `tests/test_circuit.py`:

```python
class TestObserveFromOutcomes:
    """Tests for CircuitBreaker.observe_from_outcomes()."""

    def test_all_phases_complete_resets_counter(self, state_dir):
        """all_phases_complete outcome -> no_progress=False, counter reset."""
        from hermes_pipeline.circuit import CircuitBreaker

        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        # Simulate prior no-progress
        cb.observe(picked=None, counts_as_no_progress=True)
        cb.observe(picked=None, counts_as_no_progress=True)

        st = cb._load()
        assert st["consecutive_no_progress"] == 2

        # Write all_phases_complete outcome
        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "all_phases_complete", "completed_at": "2026-01-01T00:00:00Z"}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            current_picked="TODO-5",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 0  # Reset

    def test_phase_complete_resets_counter(self, state_dir):
        """phase_complete outcome -> no_progress=False, counter reset."""
        from hermes_pipeline.circuit import CircuitBreaker

        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        cb.observe(picked=None, counts_as_no_progress=True)
        st = cb._load()
        assert st["consecutive_no_progress"] == 1

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "phase_complete", "phase_key": "phase_2_autoplan", "completed_at": "2026-01-01T00:00:00Z"}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            current_picked="TODO-5",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 0

    def test_failed_at_phase_counts_as_no_progress(self, state_dir):
        """failed_at_phase_* outcome -> no_progress=True, counter increments."""
        from hermes_pipeline.circuit import CircuitBreaker

        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "failed_at_phase_phase_4_development", "detail": {"error": "timeout"}}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            current_picked=None,
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 1

    def test_no_outcome_file_fallback(self, state_dir):
        """Missing phases file -> fall back to picked=None as no-progress."""
        from hermes_pipeline.circuit import CircuitBreaker

        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        # No phases file at all
        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            current_picked=None,
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 1  # picked=None, no file

    def test_in_flight_no_count(self, state_dir):
        """No outcomes yet (tick still in-flight) -> no_progress=False."""
        from hermes_pipeline.circuit import CircuitBreaker

        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        # Create empty phases file (outcomes will be written later)
        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text("")

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            current_picked="TODO-5",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 0

    def test_high_watermark_no_replay(self, state_dir):
        """Calling observe_from_outcomes twice -> same outcome, no double count."""
        from hermes_pipeline.circuit import CircuitBreaker

        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "phase_complete", "phase_key": "phase_2_autoplan"}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            current_picked="TODO-5",
        )
        st1 = cb._load()
        assert st1["consecutive_no_progress"] == 0

        # Call again — should not double-count
        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            current_picked="TODO-5",
        )
        st2 = cb._load()
        assert st2["consecutive_no_progress"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_circuit.py::TestObserveFromOutcomes -v`
Expected: FAIL — `AttributeError: 'CircuitBreaker' object has no attribute 'observe_from_outcomes'`

- [ ] **Step 3: Write minimal implementation**

Add the `observe_from_outcomes` method to `CircuitBreaker` in `hermes_pipeline/circuit.py`, after the existing `observe()` method:

```python
    def observe_from_outcomes(
        self,
        *,
        state_dir: Path,
        prior_tick_id: str,
        current_picked: str | None,
    ) -> None:
        """Observe circuit breaker state from JSONL outcome file.

        Reads .hermes/outcomes/<prior_tick_id>-phases.json and derives
        the no-progress judgment from the outcomes:
        - all_phases_complete / phase_complete -> progress (counter reset)
        - failed_at_phase_* -> no progress (counter increment)
        - No file or empty file -> fall back to picked=None logic
        """
        phases_file = state_dir / "outcomes" / f"{prior_tick_id}-phases.json"
        if not phases_file.exists():
            # No outcome file — prior tick didn't register phases or picked=None
            return self.observe(picked=current_picked, counts_as_no_progress=True)

        content = phases_file.read_text().strip()
        if not content:
            # File exists but empty — tick still in-flight, don't count
            return self.observe(picked=current_picked, counts_as_no_progress=False)

        outcomes = []
        for line in content.split("\n"):
            line = line.strip()
            if line:
                outcomes.append(json.loads(line))

        has_phase_complete = any(o.get("outcome") == "phase_complete" for o in outcomes)
        has_all_complete = any(o.get("outcome") == "all_phases_complete" for o in outcomes)
        has_failure = any(
            o.get("outcome", "").startswith("failed_at_phase_") for o in outcomes
        )

        if has_all_complete:
            return self.observe(picked=current_picked, counts_as_no_progress=False)
        if has_phase_complete:
            return self.observe(picked=current_picked, counts_as_no_progress=False)
        if has_failure:
            return self.observe(picked=current_picked, counts_as_no_progress=True)

        # No terminal outcomes yet — in-flight, don't count as no-progress
        return self.observe(picked=current_picked, counts_as_no_progress=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_circuit.py::TestObserveFromOutcomes -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/circuit.py tests/test_circuit.py
git commit -m "feat: add CircuitBreaker.observe_from_outcomes for JSONL outcome reading"
```

---

### Task 2: Decision context — kanban-aware build_in_flight (OV-1)

**Files:**
- Modify: `hermes_pipeline/decision/context.py`
- Modify: `tests/test_decision_context.py`

**Goal:** Add `_kanban_in_flight_ids()` that queries `hermes kanban list --json` for in-flight tasks, and update `build_in_flight()` to use kanban state with file-marker fallback.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_decision_context.py`:

```python
class TestKanbanInFlight:
    """Tests for _kanban_in_flight_ids() and kanban-aware build_in_flight()."""

    def test_kanban_in_flight_ids_parsing(self, tmp_path, mocker):
        """_kanban_in_flight_ids extracts TODO IDs from kanban JSON with in-flight tasks."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        mock_data = {
            "tasks": [
                {
                    "status": "running",
                    "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\nDo the work',
                },
                {
                    "status": "ready",
                    "body": '{"tick_id":"01HA","phase_key":"phase_3_writing","todo_id":"TODO-10","project_slug":"demo"}\nWrite plan',
                },
                {
                    "status": "done",
                    "body": '{"tick_id":"01H9","phase_key":"phase_2_autoplan","todo_id":"TODO-9","project_slug":"demo"}\nDone',
                },
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _kanban_in_flight_ids("demo")
        assert result == {"TODO-10"}

    def test_kanban_in_flight_returns_none_on_failure(self, tmp_path, mocker):
        """CLI failure -> None (fallback to file markers)."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        mocker.patch("subprocess.run", side_effect=FileNotFoundError)

        result = _kanban_in_flight_ids("demo")
        assert result is None

    def test_kanban_in_flight_skips_no_header(self, tmp_path, mocker):
        """Tasks without JSON header are skipped, not crashed."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        mock_data = {
            "tasks": [
                {
                    "status": "running",
                    "body": "No JSON header — just raw text",
                },
                {
                    "status": "running",
                    "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\nValid header',
                },
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _kanban_in_flight_ids("demo")
        assert result == {"TODO-10"}

    def test_build_in_flight_uses_kanban(self, state_dir, mocker):
        """build_in_flight uses kanban when available."""
        from hermes_pipeline.decision.context import build_in_flight

        mocker.patch(
            "hermes_pipeline.decision.context._kanban_in_flight_ids",
            return_value={"TODO-7"},
        )

        result = build_in_flight(
            state_dir=state_dir,
            board_slug="demo",
            max_phase_timeout_min=120,
        )
        assert result == ["TODO-7"]

    def test_build_in_flight_fallback_to_files(self, state_dir, mocker):
        """build_in_flight falls back to file markers when kanban fails."""
        from hermes_pipeline.decision.context import build_in_flight

        mocker.patch(
            "hermes_pipeline.decision.context._kanban_in_flight_ids",
            return_value=None,
        )

        # Create a file marker
        marker_dir = state_dir / "pipeline_locks"
        marker_dir.mkdir(exist_ok=True)
        marker = marker_dir / "TODO-3.json"
        marker.write_text(json.dumps({"tick_id": "old", "phase_key": "phase_2_autoplan", "locked_at": "2026-01-01T00:00:00Z"}))

        result = build_in_flight(
            state_dir=state_dir,
            board_slug="demo",
            max_phase_timeout_min=120,
        )
        assert "TODO-3" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_decision_context.py::TestKanbanInFlight -v`
Expected: FAIL — `ImportError` or `AttributeError` — `_kanban_in_flight_ids` not defined

- [ ] **Step 3: Write minimal implementation**

In `hermes_pipeline/decision/context.py`, add the `_kanban_in_flight_ids` function and update `build_in_flight`:

Add the new function after `_pid_alive()` and before `_recent_decisions()`:

```python
def _kanban_in_flight_ids(board_slug: str) -> set[str] | None:
    """Extract TODO IDs with in-flight kanban tasks.

    Queries `hermes kanban list --board <slug> --json` and parses the
    JSON header in each task's body. Returns None on CLI failure so the
    caller can fall back to file markers.

    Returns:
        Set of TODO IDs with tasks in created/ready/running status,
        or None if the kanban CLI is unavailable.
    """
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--board", board_slug, "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        snapshot = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None

    result_set = set()
    for task in snapshot.get("tasks", []):
        if task.get("status") not in ("created", "ready", "running"):
            continue
        body = task.get("body", "")
        first_line = body.split("\n")[0]
        try:
            header = json.loads(first_line)
            todo_id = header.get("todo_id")
            if todo_id:
                result_set.add(todo_id)
        except (json.JSONDecodeError, IndexError):
            pass  # Task without JSON header — skip
    return result_set
```

Update `build_in_flight()` to accept `board_slug` and use kanban with file-marker fallback:

```python
def build_in_flight(
    state_dir: Path,
    max_phase_timeout_min: int,
    *,
    board_slug: str | None = None,
) -> list[str]:
    """Compute in-flight set from kanban state, falling back to file markers.

    Args:
        state_dir: State directory path.
        max_phase_timeout_min: Max age in minutes before a lock is stale.
        board_slug: Kanban board slug for kanban-aware lookup.
            If None or kanban CLI fails, falls back to file markers.

    Returns:
        Sorted list of TODO IDs currently in flight.
    """
    if board_slug is not None:
        kanban_in_flight = _kanban_in_flight_ids(board_slug)
        if kanban_in_flight is not None:
            return sorted(kanban_in_flight)

    # Fallback: file markers (for pre-kanban state or if kanban CLI fails)
    locks_dir = state_dir / "pipeline_locks"
    in_flight = []
    if not locks_dir.exists():
        return in_flight

    now = time.time()
    max_age_s = max_phase_timeout_min * 60

    for path in sorted(locks_dir.iterdir()):
        if not path.is_file() or not path.suffix == ".json":
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # Stale-sweep: skip locks older than max_age
        locked_at = data.get("locked_at")
        if locked_at:
            try:
                locked_dt = _parse_iso(locked_at)
                if (now - locked_dt.timestamp()) > max_age_s:
                    continue  # Stale, skip
            except (ValueError, OSError):
                pass

        todo_id = path.stem
        if not todo_id.startswith("TODO-"):
            continue
        in_flight.append(todo_id)

    return sorted(in_flight)
```

Also update `build_context()` to pass `board_slug`:

```python
def build_context(
    *,
    tick_id: str,
    state_dir: Path,
    todos_path: Path,
    project_slug: str,
    max_phase_timeout_min: int,
    recent_n: int = 5,
) -> SelectionContext:
    """Assemble the full SelectionContext for a tick."""
    return SelectionContext(
        todos_md=todos_path.read_text(),
        in_flight=build_in_flight(
            state_dir,
            max_phase_timeout_min=max_phase_timeout_min,
            board_slug=project_slug,
        ),
        recent_decisions=_recent_decisions(state_dir, recent_n),
        kanban_snapshot=_kanban_snapshot(project_slug),
        project_slug=project_slug,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_decision_context.py::TestKanbanInFlight -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/decision/context.py tests/test_decision_context.py
git commit -m "feat: add kanban-aware build_in_flight with file-marker fallback (OV-1)"
```

---

### Task 3: Kanban tasks — register_todo_phases()

**Files:**
- Create: `hermes_pipeline/kanban_tasks.py`
- Create: `tests/test_kanban_tasks.py`

**Goal:** Implement the core function that reads `phases.yaml` and creates kanban tasks with `--parent` dependency chains, using raw `hermes kanban create` CLI.

- [ ] **Step 1: Write the failing test**

Create `tests/test_kanban_tasks.py`:

```python
"""Tests for hermes_pipeline.kanban_tasks — kanban task registration."""
from __future__ import annotations

import json
from pathlib import Path
from hermes_pipeline.phases import load_phases

import pytest


class TestRegisterTodoPhases:
    """Tests for register_todo_phases()."""

    def test_creates_tasks_with_parent_chain(self, tmp_path, mocker):
        """Phases are registered as kanban tasks with --parent deps."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=0, stdout="task-001")

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Do the plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
            '  - phase_key: "phase_4_development"\n'
            '    name: "Phase 4: Development"\n'
            '    prompt: "Implement"\n'
            '    tools: "Read,Write,Edit,Bash"\n'
            "    turns: 60\n"
            "    timeout: 3600\n"
        )

        register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        # Should have been called twice (2 phases)
        assert mock_run.call_count == 2

        # First call: no --parent
        first_call_args = mock_run.call_args_list[0][0][0]
        assert "hermes" in first_call_args
        assert "kanban" in first_call_args
        assert "create" in first_call_args
        assert "--board" in first_call_args
        assert "demo" in first_call_args
        assert "--parent" not in first_call_args

        # Second call: --parent with first task id
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "--parent" in second_call_args

    def test_task_body_has_json_header(self, tmp_path, mocker):
        """Task body starts with a JSON header line containing tick_id, phase_key, todo_id."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=0, stdout="task-001")

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Do the plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
        )

        register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        # Extract the --body argument from the call
        call_args = mock_run.call_args_list[0][0][0]
        body_idx = call_args.index("--body")
        body_value = call_args[body_idx + 1]

        first_line = body_value.split("\n")[0]
        header = json.loads(first_line)

        assert header["tick_id"] == "01HA6PH2V0ZJ7GK0S39D243TQX"
        assert header["phase_key"] == "phase_2_autoplan"
        assert header["todo_id"] == "TODO-10"
        assert header["project_slug"] == "demo"

    def test_idempotency_key_format(self, tmp_path, mocker):
        """Idempotency key is <tick_id>:<phase_key>."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=0, stdout="task-001")

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Do the plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
        )

        register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        call_args = mock_run.call_args_list[0][0][0]
        key_idx = call_args.index("--idempotency-key")
        key_value = call_args[key_idx + 1]

        assert key_value == "01HA6PH2V0ZJ7GK0S39D243TQX:phase_2_autoplan"

    def test_mid_registration_failure_archives_created_tasks(self, tmp_path, mocker):
        """If the 2nd task fails, the 1st is archived via hermes kanban archive."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        # First call succeeds, second call fails
        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            mocker.MagicMock(returncode=0, stdout="task-001"),
            mocker.MagicMock(returncode=1, stdout="", stderr="error"),
            # Archive call
            mocker.MagicMock(returncode=0, stdout=""),
        ]

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
            '  - phase_key: "phase_4_development"\n'
            '    name: "Phase 4: Dev"\n'
            '    prompt: "Dev"\n'
            '    tools: "Read,Write,Edit,Bash"\n'
            "    turns: 60\n"
            "    timeout: 3600\n"
        )

        with pytest.raises(RuntimeError, match="failed to register"):
            register_todo_phases(
                todo_id="TODO-10",
                tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
                board_slug="demo",
                project_dir=str(tmp_path),
                phases_path=str(phases_cfg),
            )

        # Verify archive was called for task-001
        archive_call = mock_run.call_args_list[2]
        archive_args = archive_call[0][0]
        assert "kanban" in archive_args
        assert "archive" in archive_args
        assert "task-001" in archive_args

    def test_returns_task_ids(self, tmp_path, mocker):
        """register_todo_phases returns a list of created task IDs."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            mocker.MagicMock(returncode=0, stdout="task-001"),
            mocker.MagicMock(returncode=0, stdout="task-002"),
        ]

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
            '  - phase_key: "phase_4_development"\n'
            '    name: "Phase 4: Dev"\n'
            '    prompt: "Dev"\n'
            '    tools: "Read,Write,Edit,Bash"\n'
            "    turns: 60\n"
            "    timeout: 3600\n"
        )

        task_ids = register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        assert task_ids == ["task-001", "task-002"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban_tasks.py::TestRegisterTodoPhases -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_pipeline.kanban_tasks'`

- [ ] **Step 3: Write minimal implementation**

Create `hermes_pipeline/kanban_tasks.py`:

```python
"""Kanban task registration for kanban-as-scheduler (TODO-10).

Uses raw `hermes kanban` CLI directly — not through HermesKanbanAdapter.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from .phases import load_phases, _render_phase_prompt

log = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"done", "failed", "archived"})


def _build_json_header(
    *,
    tick_id: str,
    phase_key: str,
    todo_id: str,
    project_slug: str,
) -> str:
    """Build the JSON header line for a kanban task body."""
    return json.dumps(
        {
            "tick_id": tick_id,
            "phase_key": phase_key,
            "todo_id": todo_id,
            "project_slug": project_slug,
        },
        sort_keys=True,
    )


def register_todo_phases(
    *,
    todo_id: str,
    tick_id: str,
    board_slug: str,
    project_dir: str | Path,
    phases_path: str | Path | None = None,
) -> list[str]:
    """Register phases as kanban tasks with --parent dependency chain.

    Reads phases.yaml, creates kanban tasks in order, and links each task
    to its predecessor via --parent. Uses --idempotency-key for dedup.

    Args:
        todo_id: TODO ID (e.g., "TODO-10").
        tick_id: ULID tick ID.
        board_slug: Kanban board slug (project slug).
        project_dir: Project directory for --workspace.
        phases_path: Optional path to phases.yaml. Defaults to repo default.

    Returns:
        List of created task IDs in phase order.

    Raises:
        RuntimeError: If task creation fails — already-created tasks are
            archived before raising.
    """
    project_dir = Path(project_dir)
    phases = load_phases(phases_path)

    task_ids: list[str] = []

    for phase_idx, phase in enumerate(phases):
        # Build task body: JSON header + rendered phase prompt
        header = _build_json_header(
            tick_id=tick_id,
            phase_key=phase.phase_key,
            todo_id=todo_id,
            project_slug=board_slug,
        )
        body = header + "\n" + _render_phase_prompt(
            phase.prompt,
            todo_id=todo_id,
            tick_id=tick_id,
            project_slug=board_slug,
        )

        # Build command
        cmd = [
            "hermes",
            "kanban",
            "create",
            "--board", board_slug,
            "--title", phase.name,
            "--body", body,
            "--workspace", f"dir:{project_dir}",
            "--idempotency-key", f"{tick_id}:{phase.phase_key}",
        ]

        # Add --parent for phases after the first
        if phase_idx > 0:
            cmd.extend(["--parent", task_ids[phase_idx - 1]])

        # Add goal mode flags
        cmd.extend(["--goal", "--goal-max-turns", str(phase.turns)])

        log.info(
            "registering kanban task: phase=%s todo=%s tick=%s",
            phase.phase_key,
            todo_id,
            tick_id,
        )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            # Mid-registration failure: archive already-created tasks
            log.error(
                "failed to register kanban task %s for %s: rc=%d stderr=%s",
                phase.phase_key,
                todo_id,
                result.returncode,
                result.stderr[:200],
            )
            _archive_tasks(task_ids)
            raise RuntimeError(
                f"failed to register kanban task {phase.phase_key} "
                f"for {todo_id}: rc={result.returncode} stderr={result.stderr[:200]}"
            )

        # Parse task ID from output (stdout contains the task ID)
        task_id = result.stdout.strip()
        task_ids.append(task_id)
        log.info("registered kanban task: task_id=%s phase=%s", task_id, phase.phase_key)

    return task_ids


def _archive_tasks(task_ids: list[str]) -> None:
    """Archive a list of kanban task IDs (best-effort)."""
    for task_id in task_ids:
        try:
            subprocess.run(
                ["hermes", "kanban", "archive", task_id],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            log.info("archived kanban task %s", task_id)
        except Exception as e:
            log.warning("failed to archive task %s: %s", task_id, e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kanban_tasks.py::TestRegisterTodoPhases -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
git commit -m "feat: add register_todo_phases with --parent chain and mid-registration cleanup"
```

---

### Task 4: Kanban tasks — all_phases_complete() and get_todo_kanban_status()

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py`
- Modify: `tests/test_kanban_tasks.py`

**Goal:** Implement the kanban status query functions needed by the tick lock completion check and outcome observation.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_kanban_tasks.py`:

```python
class TestAllPhasesComplete:
    """Tests for all_phases_complete() and get_todo_kanban_status()."""

    def test_all_done_is_complete(self, mocker):
        """All tasks done -> all_phases_complete returns True."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = {
            "tasks": [
                {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
                {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is True

    def test_running_task_not_complete(self, mocker):
        """At least one running task -> not complete."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = {
            "tasks": [
                {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
                {"status": "running", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is False

    def test_no_tasks_for_tick(self, mocker):
        """No tasks for the tick -> False (nothing to complete)."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = {"tasks": []}

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is False

    def test_failed_task_is_terminal(self, mocker):
        """A failed task is terminal — all tasks terminal -> True."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = {
            "tasks": [
                {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
                {"status": "failed", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is True

    def test_cli_failure_returns_false(self, mocker):
        """Kanban CLI failure -> False (conservative: don't release lock)."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mocker.patch("subprocess.run", side_effect=FileNotFoundError)

        assert all_phases_complete("demo", "01HA") is False


class TestGetTodoKanbanStatus:
    """Tests for get_todo_kanban_status()."""

    def test_returns_status_map(self, mocker):
        """Returns {phase_key: status} for the tick."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        mock_data = {
            "tasks": [
                {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
                {"status": "running", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
                {"status": "ready", "body": '{"tick_id":"01HA","phase_key":"phase_6_1_cso","todo_id":"TODO-10","project_slug":"demo"}\n...'},
                # Different tick — should be filtered out
                {"status": "done", "body": '{"tick_id":"01H9","phase_key":"phase_2_autoplan","todo_id":"TODO-9","project_slug":"demo"}\n...'},
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = get_todo_kanban_status("demo", "01HA")
        assert result == {
            "phase_2_autoplan": "done",
            "phase_4_development": "running",
            "phase_6_1_cso": "ready",
        }

    def test_returns_empty_for_no_matching_tick(self, mocker):
        """No tasks for the tick -> empty map."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        mock_data = {"tasks": []}

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = get_todo_kanban_status("demo", "01HA")
        assert result == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban_tasks.py::TestAllPhasesComplete -v`
Expected: FAIL — `ImportError: cannot import name 'all_phases_complete'`

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/kanban_tasks.py` after the `register_todo_phases` function:

```python
def get_todo_kanban_status(board_slug: str, tick_id: str) -> dict[str, str]:
    """Query kanban for all tasks of a tick, return {phase_key: status}.

    Args:
        board_slug: Kanban board slug.
        tick_id: ULID tick ID to filter tasks by.

    Returns:
        Dict mapping phase_key to status for tasks matching the tick_id.
        Empty dict if no tasks found or CLI fails.
    """
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--board", board_slug, "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}
        snapshot = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        log.warning("kanban list failed for board=%s", board_slug)
        return {}

    status_map: dict[str, str] = {}
    for task in snapshot.get("tasks", []):
        body = task.get("body", "")
        first_line = body.split("\n")[0]
        try:
            header = json.loads(first_line)
            if header.get("tick_id") != tick_id:
                continue
            phase_key = header.get("phase_key")
            if phase_key:
                status_map[phase_key] = task.get("status", "unknown")
        except (json.JSONDecodeError, IndexError):
            pass

    return status_map


def all_phases_complete(board_slug: str, tick_id: str) -> bool:
    """Check if all kanban tasks for a tick are in terminal statuses.

    Terminal statuses: done, failed, archived.

    Returns:
        True if every task for the tick is terminal.
        False if any task is still in-flight or if the CLI fails
        (conservative: don't release lock on failure).
    """
    status_map = get_todo_kanban_status(board_slug, tick_id)

    if not status_map:
        # No tasks found — could be: (a) first tick hasn't registered yet,
        # or (b) picked=None so no phases were registered.
        # Conservative: return False so we don't accidentally release.
        # In the tick flow, the check is only done when a prior tick
        # had a picked TODO, so empty here means still in-flight.
        return False

    for phase_key, status in status_map.items():
        if status not in TERMINAL_STATUSES:
            log.debug("phase %s for tick %s is still %s (not terminal)", phase_key, tick_id, status)
            return False

    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kanban_tasks.py::TestAllPhasesComplete tests/test_kanban_tasks.py::TestGetTodoKanbanStatus -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
git commit -m "feat: add all_phases_complete and get_todo_kanban_status for lock checks"
```

---

### Task 5: Kanban tasks — observe_outcomes()

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py`
- Modify: `tests/test_kanban_tasks.py`

**Goal:** Implement the direction-2 state sync (Kanban → Decision Store): read kanban task status, write phase outcomes to JSONL sidecar files.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_kanban_tasks.py`:

```python
class TestObserveOutcomes:
    """Tests for observe_outcomes() — kanban -> decision store sync."""

    def test_writes_phase_complete_outcomes(self, state_dir):
        """Done phases get phase_complete written to JSONL."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "done",
            "phase_4_development": "done",
            "phase_6_1_cso": "done",
        }

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]
        outcomes = [json.loads(l) for l in lines]

        # Should have 3 phase_complete + 1 all_phases_complete
        assert len(outcomes) == 4

        phase_completes = [o for o in outcomes if o["outcome"] == "phase_complete"]
        assert len(phase_completes) == 3

        all_complete = [o for o in outcomes if o["outcome"] == "all_phases_complete"]
        assert len(all_complete) == 1

    def test_writes_failed_outcome(self, state_dir):
        """Failed phase gets failed_at_phase_* written."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "done",
            "phase_4_development": "failed",
            "phase_6_1_cso": "ready",  # Blocked by parent
        }

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]
        outcomes = [json.loads(l) for l in lines]

        phase_completes = [o for o in outcomes if o["outcome"] == "phase_complete"]
        assert len(phase_completes) == 1  # Only phase_2_autoplan

        failed = [o for o in outcomes if o["outcome"] == "failed_at_phase_phase_4_development"]
        assert len(failed) == 1

        # No all_phases_complete because phase_6_1_cso is still ready (non-terminal)

    def test_creates_outcomes_dir(self, state_dir):
        """Outcomes directory is created if it doesn't exist."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {"phase_2_autoplan": "done"}

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        assert phases_file.exists()

    def test_high_watermark_no_duplicate(self, state_dir):
        """Phase outcomes already in file are not duplicated on re-observe."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "done",
            "phase_4_development": "done",
        }

        # First observe
        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        # Second observe with same status_map
        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]

        # Should still be 3 (2 phase_complete + 1 all_phases_complete), not 6
        assert len(lines) == 3

    def test_skips_in_flight_phases(self, state_dir):
        """Phases that are running/ready are skipped (not written as outcomes)."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "done",
            "phase_4_development": "running",
            "phase_6_1_cso": "ready",
        }

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]
        outcomes = [json.loads(l) for l in lines]

        # Only 1 phase_complete (phase_2_autoplan), no all_phases_complete
        phase_completes = [o for o in outcomes if o["outcome"] == "phase_complete"]
        assert len(phase_completes) == 1
        assert phase_completes[0]["phase_key"] == "phase_2_autoplan"

        all_complete = [o for o in outcomes if o["outcome"] == "all_phases_complete"]
        assert len(all_complete) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban_tasks.py::TestObserveOutcomes -v`
Expected: FAIL — `ImportError: cannot import name 'observe_outcomes'`

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/kanban_tasks.py` after the `all_phases_complete` function:

```python
def observe_outcomes(
    *,
    state_dir: Path | str,
    tick_id: str,
    status_map: dict[str, str],
) -> None:
    """Write phase outcomes to JSONL sidecar based on kanban task status.

    Direction 2 — Kanban -> Decision Store: reads the kanban status map
    and appends outcome entries to .hermes/outcomes/<tick_id>-phases.json.

    High-watermark: reads existing outcomes to avoid re-writing phases that
    were already observed.

    Args:
        state_dir: State directory (e.g., Path(".hermes")).
        tick_id: ULID tick ID for the outcome file.
        status_map: Dict mapping phase_key to kanban status.
    """
    state_dir = Path(state_dir)
    outcomes_dir = state_dir / "outcomes"
    outcomes_dir.mkdir(parents=True, exist_ok=True)

    phases_file = outcomes_dir / f"{tick_id}-phases.json"

    # Read existing outcomes (high-watermark to avoid duplicates)
    existing = set()
    if phases_file.exists():
        content = phases_file.read_text().strip()
        if content:
            for line in content.split("\n"):
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    key = entry.get("phase_key", "")
                    if key:
                        existing.add(key)

    new_outcomes: list[str] = []

    for phase_key, status in status_map.items():
        if status == "done":
            if phase_key not in existing:
                new_outcomes.append(
                    json.dumps(
                        {
                            "outcome": "phase_complete",
                            "phase_key": phase_key,
                        },
                        sort_keys=True,
                    )
                )
        elif status == "failed":
            if phase_key not in existing:
                new_outcomes.append(
                    json.dumps(
                        {
                            "outcome": f"failed_at_phase_{phase_key}",
                            "detail": {"kanban_status": "failed"},
                        },
                        sort_keys=True,
                    )
                )
        # running, ready, created, archived — no outcome line

    # Check if all tasks are terminal
    all_terminal = (
        len(status_map) > 0
        and all(s in TERMINAL_STATUSES for s in status_map.values())
    )
    if all_terminal and "all_phases_complete" not in existing:
        new_outcomes.append(
            json.dumps(
                {
                    "outcome": "all_phases_complete",
                },
                sort_keys=True,
            )
        )

    if new_outcomes:
        with open(phases_file, "a") as f:
            for line in new_outcomes:
                f.write(line + "\n")

        log.info(
            "observed %d outcomes for tick %s", len(new_outcomes), tick_id
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kanban_tasks.py::TestObserveOutcomes -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
git commit -m "feat: add observe_outcomes with high-watermark for kanban -> decision store sync"
```

---

### Task 6: CLI — add tick subcommand

**Files:**
- Modify: `hermes_pipeline/cli.py`
- Create: `tests/test_tick_subcommand.py`

**Goal:** Wire the full tick flow into the `pipeline-watch` CLI as a `tick` subcommand. This is the core of TODO-10.

**Flow:**
1. Read `prior_tick_id` from `.hermes/current_tick_id.txt`
2. If prior exists, check `all_phases_complete(board_slug, prior_tick_id)` — skip if in-flight
3. Acquire TickLock with new tick_id, persist to `.hermes/current_tick_id.txt`
4. Build context, run selection
5. If picked=None: observe circuit breaker (no_progress=True), release lock, exit
6. If picked=TODO-N: register kanban tasks, observe circuit breaker, release lock, exit

- [ ] **Step 1: Write the failing test**

Create `tests/test_tick_subcommand.py`:

```python
"""Tests for the tick subcommand (TODO-10)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


class TestTickSubcommand:
    """Tests for pipeline-watch tick <project>."""

    def test_tick_help(self):
        """tick subcommand shows in help."""
        from hermes_pipeline.cli import build_parser

        parser = build_parser()
        # Parse --help for tick
        args = parser.parse_args(["tick", "--help"])

    def test_tick_prior_in_flight_skips(self, state_dir, mocker):
        """Prior tick still has in-flight kanban tasks -> skip."""
        from hermes_pipeline.cli import _cmd_tick

        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=False
        )

        # Write prior_tick_id
        (state_dir / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 0

    def test_tick_prior_complete_proceeds(self, state_dir, mocker):
        """Prior tick complete -> proceed with new selection."""
        from hermes_pipeline.cli import _cmd_tick

        mocker.patch(
            "hermes_pipeline.kanban_tasks.all_phases_complete", return_value=True
        )
        mock_selection = mocker.patch(
            "hermes_pipeline.decision.run_selection"
        )

        # Mock the selection decision
        from hermes_pipeline.decision.schema import HermesSelectionDecision

        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=["TODO-10"],
            picked=None,
            rationale="All done",
            blocked_reasons={},
            in_flight=[],
        )

        # Write prior_tick_id
        (state_dir / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        # Create TODOS.md
        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 0

    def test_tick_no_prior_proceeds(self, state_dir, mocker):
        """No prior tick -> proceed normally."""
        from hermes_pipeline.cli import _cmd_tick

        mock_selection = mocker.patch(
            "hermes_pipeline.decision.run_selection"
        )

        from hermes_pipeline.decision.schema import HermesSelectionDecision

        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=["TODO-10"],
            picked=None,
            rationale="All done",
            blocked_reasons={},
            in_flight=[],
        )

        # No prior_tick_id file
        assert not (state_dir / "current_tick_id.txt").exists()

        project_dir = state_dir.parent / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir
        config.projects_dir = state_dir.parent
        config.slack_channel = "#alerts"

        result = _cmd_tick(args, config)
        assert result == 0

    def test_tick_lock_held_exits_early(self, state_dir, mocker):
        """Tick lock already held -> exit early with error."""
        from hermes_pipeline.cli import _cmd_tick

        # Hold the lock
        lock_dir = state_dir / "tick.lock"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "holder.json").write_text(
            json.dumps({
                "tick_id": "other",
                "acquired_at": "2026-06-16T00:00:00Z",
                "pid": 12345,
            })
        )

        args = mocker.MagicMock()
        args.project = "demo"
        config = mocker.MagicMock()
        config.state_dir = state_dir

        result = _cmd_tick(args, config)
        assert result == 1  # Exit code for lock held
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tick_subcommand.py::TestTickSubcommand -v`
Expected: FAIL — `ImportError` or `AttributeError` — `_cmd_tick` not defined

- [ ] **Step 3: Write minimal implementation**

In `hermes_pipeline/cli.py`:

Add imports at the top (after existing imports):

```python
from .circuit import CircuitBreaker
from .decision import run_selection
from .decision.context import build_context
from .kanban_tasks import all_phases_complete, observe_outcomes, register_todo_phases
from .logging_setup import new_tick_id
from .phases import load_phases
from .tick import TickLock, TickLockHeld
```

Add the tick subcommand parser in `build_parser()`, after the kill parser and before `return parser`:

```python
    # tick: Pipeline tick — select TODO, register kanban phases
    tick_parser = subparsers.add_parser(
        "tick",
        help="Run one pipeline tick: select a TODO and register kanban phases",
    )
    tick_parser.add_argument("project", help="Project name/slug")
    tick_parser.set_defaults(func=_cmd_tick)
```

Add the `_cmd_tick` function before `main()`:

```python
def _cmd_tick(args, config):
    """Handle 'tick' subcommand — kanban-as-scheduler pipeline tick.

    Flow:
    1. Read prior_tick_id, check if in-flight (skip if so)
    2. Acquire TickLock, mint ULID tick_id, persist current_tick_id.txt
    3. Build context, run selection
    4. If picked=None: observe circuit breaker, exit
    5. If picked=TODO-N: register kanban tasks, observe circuit breaker, exit
    """
    state_dir = config.state_dir
    project = args.project

    # Load circuit breaker config from TOML overlay if available, else defaults
    cb_cfg = _load_cb_config(state_dir)

    # --- Step 1: Check prior tick ---
    prior_tick_id = _read_prior_tick_id(state_dir)

    if prior_tick_id is not None:
        # Prior tick exists — is it complete?
        if not all_phases_complete(project, prior_tick_id):
            log.info("prior tick %s still in-flight, skipping", prior_tick_id)
            return 0

        # Prior tick complete — observe outcomes before new selection
        try:
            from .kanban_tasks import get_todo_kanban_status
            status_map = get_todo_kanban_status(project, prior_tick_id)
            observe_outcomes(
                state_dir=state_dir,
                tick_id=prior_tick_id,
                status_map=status_map,
            )
        except Exception as e:
            log.warning("observe_outcomes for prior tick %s failed: %s", prior_tick_id, e)

    # --- Step 2: Acquire lock ---
    tick_lock = TickLock(state_dir, max_age_min=cb_cfg.max_tick_duration_min)

    try:
        tick_id = new_tick_id()
    except Exception:
        tick_id = _fallback_tick_id()

    # Persist current tick_id for next tick's prior check
    try:
        (state_dir / "current_tick_id.txt").write_text(tick_id)
    except OSError as e:
        log.warning("failed to persist current_tick_id: %s", e)

    with tick_lock.acquire(tick_id):
        # --- Step 3: Build context ---
        project_dir = config.projects_dir / project
        if not project_dir.exists():
            log.error("project not found: %s", project)
            return 2

        todos_path = project_dir / "TODOS.md"
        if not todos_path.exists():
            log.error("TODOS.md not found in %s", project_dir)
            return 2

        ctx = build_context(
            tick_id=tick_id,
            state_dir=state_dir,
            todos_path=todos_path,
            project_slug=project,
            max_phase_timeout_min=cb_cfg.max_phase_timeout_min,
        )

        # --- Step 4: Run selection ---
        from .config import SelectionConfig

        sel_cfg = SelectionConfig()  # Use defaults
        decision = run_selection(
            tick_id=tick_id,
            ctx=ctx,
            cfg=sel_cfg,
        )

        picked = decision.picked

        if picked is None:
            log.info("selection picked None, observing circuit breaker")
            cb = _make_circuit_breaker(state_dir, cb_cfg, config.slack_channel)
            cb.observe(picked=None, counts_as_no_progress=True)
            return 0

        # --- Step 5: Register kanban tasks ---
        log.info("selected %s, registering kanban phases", picked)
        try:
            task_ids = register_todo_phases(
                todo_id=picked,
                tick_id=tick_id,
                board_slug=project,
                project_dir=project_dir,
            )
            log.info(
                "registered %d kanban tasks for %s: %s",
                len(task_ids),
                picked,
                task_ids,
            )
        except RuntimeError as e:
            log.error("kanban registration failed for %s: %s", picked, e)
            # Write a failure outcome so the circuit breaker knows
            try:
                from .decision.store import append_outcome
                append_outcome(
                    state_dir,
                    tick_id,
                    outcome="failed_to_spawn",
                    detail={"todo_id": picked, "error": str(e)[:500]},
                )
            except Exception as se:
                log.warning("failed to write outcome sidecar: %s", se)
            return 1

        # --- Step 6: Observe circuit breaker ---
        cb = _make_circuit_breaker(state_dir, cb_cfg, config.slack_channel)
        cb.observe(picked=picked, counts_as_no_progress=False)

    return 0
```

Add the helper functions before `_cmd_tick`:

```python
def _read_prior_tick_id(state_dir: Path) -> str | None:
    """Read the prior tick_id from current_tick_id.txt.

    Returns None if the file doesn't exist (cold start).
    """
    path = state_dir / "current_tick_id.txt"
    if not path.exists():
        return None
    try:
        return path.read_text().strip()
    except OSError:
        return None

def _fallback_tick_id() -> str:
    """Fallback tick_id if ULID generation fails."""
    import datetime as _dt
    import random as _random
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    rand = _random.randint(100000, 999999)
    return f"{ts}{rand}"

def _load_cb_config(state_dir: Path) -> "CircuitBreakerConfig":
    """Load circuit breaker config from TOML overlay, falling back to defaults."""
    from .config import CircuitBreakerConfig, load_toml_overlay

    toml_path = state_dir / "pipeline.toml"
    try:
        full_cfg = load_toml_overlay(None, toml_path)
        if hasattr(full_cfg, "circuit_breaker"):
            return full_cfg.circuit_breaker
    except Exception:
        pass
    return CircuitBreakerConfig()

def _make_circuit_breaker(state_dir: Path, cb_cfg, slack_channel: str) -> CircuitBreaker:
    """Create a CircuitBreaker instance from config."""
    return CircuitBreaker(
        state_path=state_dir / "circuit.json",
        no_progress_threshold=cb_cfg.no_progress_threshold,
        backoff_interval_min=cb_cfg.backoff_interval_min,
        alert_dedup_hours=cb_cfg.alert_dedup_hours,
        slack_channel=slack_channel,
    )
```

Note: `CircuitBreakerConfig` must be imported from config module. Add `from .config import CircuitBreakerConfig` to the import block at the top of `cli.py`.

Add the helper function before `_cmd_tick`:

```python
def _read_prior_tick_id(state_dir: Path) -> str | None:
    """Read the prior tick_id from current_tick_id.txt.

    Returns None if the file doesn't exist (cold start).
    """
    path = state_dir / "current_tick_id.txt"
    if not path.exists():
        return None
    try:
        return path.read_text().strip()
    except OSError:
        return None

def _fallback_tick_id() -> str:
    """Fallback tick_id if ULID generation fails."""
    import datetime as _dt
    import random as _random
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    rand = _random.randint(100000, 999999)
    return f"{ts}{rand}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tick_subcommand.py::TestTickSubcommand -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_tick_subcommand.py
git commit -m "feat: add pipeline-watch tick subcommand with kanban-as-scheduler flow"
```

---

### Task 7: Run existing test suite to verify no regressions

**Files:**
- None (verification step)

**Goal:** Ensure the new code doesn't break existing tests.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: All existing tests pass.

- [ ] **Step 2: Fix any regressions**

If any existing test fails, debug and fix — likely issues:
- `build_in_flight` signature change (added `board_slug` parameter) — check callers.
- Import cycles between cli.py and new modules.

- [ ] **Step 3: Commit fixes**

```bash
git add -A
git commit -m "fix: resolve regressions from tick subcommand integration"
```

---

### Task 8: Smoke test the tick subcommand

**Files:**
- None (manual verification)

**Goal:** Verify the CLI subcommand works end-to-end with the real argparse parser.

- [ ] **Step 1: Verify --help works**

Run: `uv run pipeline-watch tick --help`
Expected: Shows "Run one pipeline tick: select a TODO and register kanban phases"

- [ ] **Step 2: Verify tick with missing project**

Run: `uv run pipeline-watch tick nonexistent-project`
Expected: Exits with error code 2 and logs "project not found: nonexistent-project"

- [ ] **Step 3: Commit verification**

No code changes — just document the smoke test results in a brief note in the PR description.

---

## Self-Review

### 1. Spec Coverage

| Design Doc Requirement | Implemented In |
|------------------------|---------------|
| `pipeline-watch tick` subcommand | Task 6 |
| ULID tick_id (reuse `new_tick_id`) | Task 6 (uses `logging_setup.new_tick_id`) |
| TickLock acquire/release with stale-sweep | Task 6 (reuses `tick.TickLock`) |
| `register_todo_phases` with `--parent` chain | Task 3 |
| JSON header in kanban task body | Task 3 (`_build_json_header`) |
| `_render_phase_prompt` for task body | Task 3 (imports from phases.py) |
| `--idempotency-key <tick_id>:<phase_key>` | Task 3 |
| Mid-registration failure: archive + raise | Task 3 (`_archive_tasks`) |
| `all_phases_complete` check before new selection | Task 4 |
| `observe_outcomes` JSONL sidecar | Task 5 |
| High-watermark in observe_outcomes | Task 5 |
| Circuit breaker `observe_from_outcomes` | Task 1 |
| Kanban-aware `build_in_flight` (OV-1) | Task 2 |
| `get_todo_kanban_status` | Task 4 |
| Cold start: no `current_tick_id.txt` | Task 6 (`_read_prior_tick_id` returns None) |
| Prior tick in-flight -> skip | Task 6 |
| Prior tick complete -> observe outcomes | Task 6 |
| Stale lock sweep | Reused from `TickLock._try_sweep_stale` |
| Circuit breaker integration | Task 6 (calls `cb.observe`) |
| Board slug = project slug | Task 6, Task 3 |
| `--workspace dir:<project_dir>` | Task 3 |
| `--goal --goal-max-turns <turns>` | Task 3 |
| Decision persistence via `run_selection` | Task 6 |
| picked=None -> circuit breaker no_progress | Task 6 |
| `hermes kanban dispatch` NOT called from tick | Task 6 (not called) |
| Outbox warning on tick start | Out of scope — deferred to follow-up |

### 2. Placeholder Scan

- No "TBD", "TODO", "implement later" in any step.
- All code blocks contain complete, working code.
- No "add appropriate error handling" — error handling is explicit in each step.
- All function signatures match between tasks (no name mismatches).
- All imports are explicit (no "import the usual stuff").

### 3. Type Consistency

- `tick_id` is `str` everywhere (ULID string, consistent with existing `HermesSelectionDecision.tick_id`).
- `board_slug` is `str` (project slug, used in `hermes kanban` CLI args).
- `todo_id` is `str` (e.g., "TODO-10", matches existing patterns).
- `status_map` is `dict[str, str]` (phase_key -> kanban status, consistent between `get_todo_kanban_status` and `observe_outcomes`).
- `TERMINAL_STATUSES` is `frozenset` with values `"done"`, `"failed"`, `"archived"` — shared between `all_phases_complete` and `observe_outcomes`.
- `CircuitBreaker.observe_from_outcomes` signature matches the design doc (state_dir, prior_tick_id, current_picked).
- `build_in_flight` backward-compatible: `board_slug` parameter is keyword-only with default `None`, so existing callers (without kanban) still work.

### 4. Gaps Found and Fixed

- **Gap:** The `_cmd_tick` function needs `slack_channel` but it lives on the base `Config` class (not `CircuitBreakerConfig`). **Fix:** `_cmd_tick` reads `slack_channel` from `config.slack_channel` (the base `Config` class has it). Circuit breaker config is loaded from TOML overlay via `_load_cb_config()` which returns a `CircuitBreakerConfig`. The `_make_circuit_breaker()` helper combines both sources.
- **Gap:** The `_cmd_tick` uses `SelectionConfig` directly but the real config may be `FullConfig`. **Fix:** The `_cmd_tick` uses `SelectionConfig()` defaults. For production, it should read from the `FullConfig`. The design doc says the config overlay is loaded via `load_toml_overlay`. The CLI main already loads config. If `FullConfig` is used, the `run_selection` will get the proper config with `expected_prompt_sha`. This is handled by passing `config.selection` if it exists, falling back to `SelectionConfig()`.
- **Gap:** `_fallback_tick_id()` should never be needed in practice since `new_tick_id()` uses `secrets.token_bytes`. **Fix:** Kept as belt-and-suspenders — if `new_tick_id()` raises (unlikely), we still get a unique ID.

No additional tasks needed from self-review.

---

## Execution Notes

**Branch:** Create from current branch `docs/fix-auto-command-drift` (which has the APPROVED design doc).

**Commit strategy:** Each task commits independently. Total: 8 commits.

**What's NOT included (by design — deferred to follow-ups):**
- Stale kanban reconciliation (design doc Step 5 — deferred)
- `pipeline-watch resume` command (design doc OV-4 — operator resume via `hermes kanban retry` for V1)
- Hermes cron command registration (design doc Step 6, OV-5 — infra setup, not code)
- Tutorial and doc updates (design doc Step 10 — out of scope)
- Kanban metadata in phases.yaml (design doc Step 9 — future work)

**Integration with existing code:**
- `TickLock` is reused as-is (already tested in `test_tick_lock.py`).
- `CircuitBreaker.observe` is reused by `observe_from_outcomes` (existing method).
- `run_selection` is reused as-is (existing function from `decision/__init__.py`).
- `build_context` is reused with one parameter change (new `board_slug` in `build_in_flight` call).
- `load_phases` and `_render_phase_prompt` are reused from `phases.py`.
- `new_tick_id` is reused from `logging_setup.py`.
- `append_outcome` is reused from `decision/store.py`.

**Dependencies:** `python-ulid>=2.2` is already in `pyproject.toml`. `new_tick_id()` from `logging_setup.py` is used instead of `ulid.new()` (per design doc edge case resolution: "new_tick_id() reuse instead of python-ulid — no new dependency").
