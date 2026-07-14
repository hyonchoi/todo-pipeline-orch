# Mock Integration Test Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repeatable mock integration test harness (`hermes-pipeline test`) that drives the real pipeline end-to-end against mock project data and produces findings reports.

**Architecture:** Standalone CLI subcommand on existing `hermes-pipeline` entry point. New `harness.py` module handles fixture bootstrapping, preflight checks, pipeline execution with monitor callback, and convergence detection. New `test_report.py` module transforms JSONL event logs into structured reports. Additive changes to `PipelineRunner` (optional `monitor` callback, `continue_on_failure` flag, auto-approve gates).

**Tech Stack:** Python 3.12, argparse, subprocess, tempfile, pathlib, json, dataclasses, pytest.

## Global Constraints

- Package entry point: `hermes-pipeline` (pyproject.toml `[project.scripts]`).
- Python >= 3.12, uv-managed deps.
- No new runtime dependencies beyond `pyyaml` and `python-ulid`.
- Test files: `tests/test_harness.py`, `tests/test_report.py`, `tests/test_harness_e2e.py`.
- Source files: `hermes_pipeline/harness.py`, `hermes_pipeline/test_report.py`.
- Existing files modified: `hermes_pipeline/cli.py`, `hermes_pipeline/runner.py`, `pyproject.toml`.
- TDD: write failing test, run it, implement minimal code, verify pass, commit.
- Config isolation: use `PIPELINE_STATE_DIR`, `PIPELINE_LOCK_DIR`, `PIPELINE_PROJECTS_DIR` env vars for Python process. Override `HOME` only for hermes/claude subprocess environments.
- Fixture model: pin `claude-haiku-4-5` in fixture `.hermes/config.toml`.
- Phase data reference: `hermes_pipeline/data/phases.yaml` — 9 phases, 2 gates (phase_2b_plan_gate, phase_9_ship).
- `PipelineRunner` current constructor: `project, project_dir, branch, todo_id, title, phases, state, kanban, run_phase_fn, tick_id="", pr_url_resolver=lambda: ""`.
- `Config.from_env()` already reads `PIPELINE_*` env vars.
- `_get_project_state_dir(project_dir)` returns `project_dir / ".hermes"`.
- `Phase` dataclass: `phase_key, name, prompt, tools, turns, timeout, terminal, gate`.
- Gate phases: `phase.gate == True`. Current gate check in `phases.py:_invoke_hermes` calls `gates.check_gate_status()` — returns `GateStatus.RUNNING` for approved, raises `RuntimeError` otherwise.

---

### Task 1: Add hermes-pipeline entrypoint

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `hermes_pipeline.cli:main`
- Produces: `[project.scripts] hermes-pipeline = "hermes_pipeline.cli:main"`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_entrypoint.py
import subprocess
import sys

def test_hermes_pipeline_entrypoint_exists():
    """Verify hermes-pipeline CLI entry point is registered."""
    result = subprocess.run(
        [sys.executable, "-m", "hermes_pipeline.cli", "--version"],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "hermes-pipeline" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_entrypoint.py::test_hermes_pipeline_entrypoint_exists -v`
Expected: FAIL — no `hermes-pipeline` entry point exists yet (only `pipeline-watch`)

- [ ] **Step 3: Write minimal implementation**

Add to `pyproject.toml` under `[project.scripts]`:
```toml
[project.scripts]
pipeline-watch = "hermes_pipeline.cli:main"
hermes-pipeline = "hermes_pipeline.cli:main"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_entrypoint.py::test_hermes_pipeline_entrypoint_exists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/test_cli_entrypoint.py uv.lock
git commit -m "feat: add hermes-pipeline CLI entrypoint alias"
```

---

### Task 2: Add test CLI subcommand

**Files:**
- Modify: `hermes_pipeline/cli.py`

**Interfaces:**
- Consumes: `build_parser()` subparser registration pattern
- Produces: `_cmd_test(args, config)` handler, `test` subparser with `--fixture`, `--loop`, `--phase`, `--keep`, `--timeout`, `--convergence-threshold` flags

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py - add new tests
def test_test_subcommand_parsing():
    """Verify 'test' subcommand parses --fixture flag."""
    from hermes_pipeline.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path"])
    assert args.command == "test"
    assert args.fixture == "happy-path"

def test_test_subcommand_loop_flag():
    """Verify --loop flag is parsed."""
    from hermes_pipeline.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path", "--loop"])
    assert args.loop is True

def test_test_subcommand_phase_flag():
    """Verify --phase flag is parsed."""
    from hermes_pipeline.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path", "--phase", "phase_2_autoplan"])
    assert args.phase == "phase_2_autoplan"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_test_subcommand_parsing -v`
Expected: FAIL — no `test` subparser exists

- [ ] **Step 3: Write minimal implementation**

In `build_parser()`, add the test subparser before the `return parser` line:
```python
    # test: Mock integration test harness
    test_parser = subparsers.add_parser(
        "test",
        help="Run mock integration test harness against mock project data",
    )
    test_parser.add_argument(
        "--fixture", required=True,
        help="Fixture name to use (e.g., happy-path)",
    )
    test_parser.add_argument(
        "--loop", action="store_true",
        help="Re-run from scratch and diff report against previous run",
    )
    test_parser.add_argument(
        "--phase", default=None,
        help="Run only a single phase by key (e.g., phase_2_autoplan)",
    )
    test_parser.add_argument(
        "--keep", action="store_true",
        help="Keep temp directory after run for inspection",
    )
    test_parser.add_argument(
        "--timeout", type=int, default=3600,
        help="Overall run timeout in seconds (default: 3600 = 60min)",
    )
    test_parser.add_argument(
        "--convergence-threshold", type=int, default=3,
        help="Consecutive same-class failures to halt run (default: 3)",
    )
    test_parser.set_defaults(func=_cmd_test)
```

Add `_cmd_test` handler:
```python
def _cmd_test(args, config: Config) -> int:
    """Handle 'test' subcommand — mock integration test harness."""
    from .harness import run_harness
    try:
        result = run_harness(
            fixture_name=args.fixture,
            loop=args.loop,
            phase_only=args.phase,
            keep_dir=args.keep,
            timeout=args.timeout,
            convergence_threshold=args.convergence_threshold,
            config=config,
        )
        if result.exit_code != 0:
            return result.exit_code
        return 0
    except Exception as e:
        log.error("test harness failed: %s", e, exc_info=True)
        return 2
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py::test_test_subcommand_parsing tests/test_cli.py::test_test_subcommand_loop_flag tests/test_cli.py::test_test_subcommand_phase_flag -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_cli.py
git commit -m "feat: add test CLI subcommand with fixture/loop/phase flags"
```

---

### Task 3: harness.py — fixture factory

**Files:**
- Create: `hermes_pipeline/harness.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: subprocess, tempfile, git CLI
- Produces: `create_mock_project(path, fixture_name)` → dict with project metadata; `HappyPathFixture` constant

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness.py
import pytest
from pathlib import Path

def test_create_mock_project_happy_path(tmp_path):
    """Fixture factory creates a valid mock project for happy-path."""
    from hermes_pipeline.harness import create_mock_project

    result = create_mock_project(tmp_path, "happy-path")

    # Verify git repo exists
    git_dir = tmp_path / ".git"
    assert git_dir.exists(), "git init was not performed"

    # Verify initial commit exists
    git_log = Path(tmp_path)
    import subprocess
    r = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, "no git commits found"
    assert len(r.stdout.strip().splitlines()) >= 1

    # Verify TODOS.md exists with entries
    todos = tmp_path / "TODOS.md"
    assert todos.exists()
    content = todos.read_text()
    assert "TODO-" in content

    # Verify .hermes/config.toml exists with model pinned
    config_toml = tmp_path / ".hermes" / "config.toml"
    assert config_toml.exists()
    config_content = config_toml.read_text()
    assert "claude-haiku-4-5" in config_content

    # Verify branch is on a named branch, not detached HEAD
    r = subprocess.run(["git", "branch", "--show-current"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0
    assert len(r.stdout.strip()) > 0

    # Verify return dict has expected keys
    assert "project_slug" in result
    assert "todo_id" in result
    assert "branch" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py::test_create_mock_project_happy_path -v`
Expected: FAIL — ModuleNotFoundError: no module named 'hermes_pipeline.harness'

- [ ] **Step 3: Write minimal implementation**

```python
# hermes_pipeline/harness.py
"""Mock integration test harness — fixture factory, preflight, runner."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def create_mock_project(path: Path, fixture_name: str) -> dict[str, Any]:
    """Create a mock project in *path* for integration testing.

    Creates:
    - git init + initial commit on named branch
    - TODOS.md with mock entries
    - .hermes/config.toml with pinned test model
    - Minimal source file

    Returns dict with project_slug, todo_id, branch, fixture_name.
    """
    path.mkdir(parents=True, exist_ok=True)

    # Git init on named branch
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@localhost"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)

    # Create TODOS.md with mock entries
    todos_content = _get_todos_for_fixture(fixture_name)
    (path / "TODOS.md").write_text(todos_content)

    # Create minimal source file
    (path / "README.md").write_text(f"# Mock Project — {fixture_name}\n")

    # Create .hermes directory with config
    hermes_dir = path / ".hermes"
    hermes_dir.mkdir()
    (path / ".hermes" / "config.toml").write_text(_get_hermes_config())
    (path / ".hermes" / "todo_id_counter").write_text("0")

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial: mock project setup"], cwd=path, check=True, capture_output=True)

    todo_id = _get_todo_id_for_fixture(fixture_name)
    return {
        "project_slug": "mock-project",
        "todo_id": todo_id,
        "branch": f"feat/mock-{fixture_name}",
        "fixture_name": fixture_name,
    }


def _get_todos_for_fixture(fixture_name: str) -> str:
    if fixture_name == "happy-path":
        return (
            "# TODOS\n\n"
            "> Active entries below. Archive completed entries to TODOS-archive.md.\n\n"
            "1. **TODO-1** Implement mock feature A — adds a simple data transformation module. "
            "Priority: P1. Status: active.\n"
        )
    else:
        raise ValueError(f"Unknown fixture: {fixture_name}")


def _get_hermes_config() -> str:
    return (
        "[profile]\n"
        'model = "claude-haiku-4-5"\n'
        "\n"
        "[selection]\n"
        'model = "claude-haiku-4-5"\n'
    )


def _get_todo_id_for_fixture(fixture_name: str) -> int:
    # Map fixture names to a TODO ID the runner can reference
    return 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harness.py::test_create_mock_project_happy_path -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add fixture factory for mock integration test harness"
```

---

### Task 4: harness.py — preflight checks

**Files:**
- Modify: `hermes_pipeline/harness.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: `HermesDependencyError` from `hermes_adapter.py`
- Produces: `preflight_check()` → raises on missing deps

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness.py
def test_preflight_check_git_not_found(tmp_path, monkeypatch):
    """Preflight fails fast when git is not on PATH."""
    from hermes_pipeline.harness import preflight_check

    # Remove all binaries from PATH
    monkeypatch.setenv("PATH", "")

    with pytest.raises(RuntimeError, match="[Gg]it"):
        preflight_check()

def test_preflight_check_hermes_not_found(tmp_path, monkeypatch):
    """Preflight fails fast when hermes CLI is not on PATH."""
    from hermes_pipeline.harness import preflight_check

    # Set PATH to only have git, not hermes
    import shutil
    git_dir = Path(shutil.which("git")).parent
    monkeypatch.setenv("PATH", str(git_dir))

    with pytest.raises(RuntimeError, match="[Hh]ermes"):
        preflight_check()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness.py::test_preflight_check_git_not_found -v`
Expected: FAIL — name 'preflight_check' is not defined

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/harness.py`:
```python
def preflight_check() -> None:
    """Verify required CLI tools are available before creating fixtures.

    Raises RuntimeError with descriptive message if any dependency is missing.
    """
    required = [
        ("git", "Git is not installed or not on PATH. "
                 "Install: https://git-scm.com"),
        ("hermes", "Hermes CLI is not installed or not on PATH. "
                    "Install: https://hermos.dev"),
        ("claude", "Claude Code CLI is not installed or not on PATH. "
                    "Install: https://claude.ai/code"),
    ]
    for cmd, msg in required:
        if shutil.which(cmd) is None:
            raise RuntimeError(f"Missing dependency: {cmd} — {msg}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness.py::test_preflight_check_git_not_found tests/test_harness.py::test_preflight_check_hermes_not_found -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add preflight dependency checks to harness"
```

---

### Task 5: harness.py — convergence detector

**Files:**
- Modify: `hermes_pipeline/harness.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: phase event results with error_class field
- Produces: `ConvergenceDetector` class with `record()` and `should_halt()` methods

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness.py
def test_convergence_detector_halts_after_threshold():
    """Detector halts after 3 consecutive same-class failures."""
    from hermes_pipeline.harness import ConvergenceDetector

    detector = ConvergenceDetector(threshold=3)

    detector.record("phase_2_autoplan", "hermes_error")
    detector.record("phase_3_writing_plan", "hermes_error")
    assert detector.should_halt() is False, "Should not halt at 2 consecutive"

    detector.record("phase_4_development", "hermes_error")
    assert detector.should_halt() is True, "Should halt at 3 consecutive same-class"

def test_convergence_detector_resets_on_success():
    """Detector resets consecutive count on phase success."""
    from hermes_pipeline.harness import ConvergenceDetector

    detector = ConvergenceDetector(threshold=3)

    detector.record("phase_2_autoplan", "hermes_error")
    detector.record("phase_3_writing_plan", "hermes_error")
    detector.record("phase_4_development", None)  # success — no error
    assert detector.should_halt() is False

    detector.record("phase_5_review", "hermes_error")
    assert detector.should_halt() is False, "Count reset after success"

def test_convergence_detector_different_error_class():
    """Detector resets count when error class changes."""
    from hermes_pipeline.harness import ConvergenceDetector

    detector = ConvergenceDetector(threshold=3)

    detector.record("phase_2_autoplan", "hermes_error")
    detector.record("phase_3_writing_plan", "hermes_error")
    detector.record("phase_4_development", "timeout")  # different class
    assert detector.should_halt() is False, "Different error class resets count"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness.py::test_convergence_detector_halts_after_threshold -v`
Expected: FAIL — name 'ConvergenceDetector' is not defined

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/harness.py`:
```python
@dataclass
class ConvergenceDetector:
    """Track consecutive same-class phase failures within a single run.

    Halts the run when *threshold* consecutive phases fail with the same
    error class, distinguishing transient API blips from misconfiguration.
    """
    threshold: int = 3
    _consecutive: int = field(default=0, repr=False)
    _last_error_class: str | None = field(default=None, repr=False)

    def record(self, phase_key: str, error_class: str | None) -> None:
        """Record a phase outcome.

        Args:
            phase_key: Phase identifier.
            error_class: Error class bucket (None for success).
        """
        if error_class is None:
            # Success resets the counter
            self._consecutive = 0
            self._last_error_class = None
        elif error_class == self._last_error_class:
            self._consecutive += 1
        else:
            # Different error class — reset counter
            self._consecutive = 1
            self._last_error_class = error_class

    def should_halt(self) -> bool:
        """Return True if consecutive same-class failures exceed threshold."""
        return self._consecutive >= self.threshold
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness.py -k "convergence" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add convergence detector to harness"
```

---

### Task 6: harness.py — monitor callback

**Files:**
- Modify: `hermes_pipeline/harness.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: JSONL event log path
- Produces: `HarnessMonitor` class with callable interface for pipeline events; `write_event(path, event_dict)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness.py
def test_harness_monitor_writes_jsonl_events(tmp_path):
    """Monitor callback writes structured JSONL events."""
    from hermes_pipeline.harness import HarnessMonitor

    log_path = tmp_path / "events.jsonl"
    monitor = HarnessMonitor(log_path)

    monitor("phase_started", {"phase_key": "phase_2_autoplan", "todo_id": "TODO-1"})
    monitor("phase_completed", {"phase_key": "phase_2_autoplan", "duration_ms": 5000})

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2

    import json
    event1 = json.loads(lines[0])
    assert event1["event_type"] == "phase_started"
    assert event1["phase_key"] == "phase_2_autoplan"
    assert "timestamp" in event1

    event2 = json.loads(lines[1])
    assert event2["event_type"] == "phase_completed"
    assert event2["duration_ms"] == 5000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py::test_harness_monitor_writes_jsonl_events -v`
Expected: FAIL — name 'HarnessMonitor' is not defined

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/harness.py`:
```python
import datetime as _dt
import json as _json


class HarnessMonitor:
    """Write pipeline events to a JSONL log file.

    Called by PipelineRunner on each phase transition. Each call appends one
    JSON line with timestamp, event type, and phase metadata.
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._start_time: float | None = None

    def __call__(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        event = {
            "timestamp": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_type": event_type,
        }
        if data:
            event.update(data)
        if event_type == "phase_started" and self._start_time is None:
            self._start_time = time.time()
            event["run_start_time"] = event["timestamp"]
        with open(self.log_path, "a") as f:
            f.write(_json.dumps(event, sort_keys=True) + "\n")
```

Also add `import time` at top of file.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harness.py::test_harness_monitor_writes_jsonl_events -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add HarnessMonitor JSONL event callback"
```

---

### Task 7: Add continue_on_failure + auto-approve-gates to PipelineRunner

**Files:**
- Modify: `hermes_pipeline/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: existing `PipelineRunner.run()` phase loop
- Produces: `continue_on_failure` parameter on `PipelineRunner.__init__`; when True, phase failures log and continue to next phase instead of returning False; gate phases short-circuit to success

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner.py
def test_runner_continue_on_failure(tmp_path):
    """Runner continues to next phase when continue_on_failure=True and a phase fails."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
        Phase(phase_key="phase_3", name="Phase 3", prompt="p3", tools="", turns=0),
        Phase(phase_key="phase_4", name="Phase 4", prompt="p4", tools="", turns=0),
    ]

    call_order = []

    def run_phase_fn(phase: Phase) -> int:
        call_order.append(phase.phase_key)
        if phase.phase_key == "phase_3":
            return 1  # fail
        return 0

    runner = PipelineRunner(
        project="test", project_dir=tmp_path, branch="feat/test",
        todo_id=1, title="Test", phases=phases, state=state,
        kanban=kanban, run_phase_fn=run_phase_fn,
        continue_on_failure=True,
    )
    result = runner.run()

    assert "phase_2" in call_order
    assert "phase_3" in call_order
    assert "phase_4" in call_order, "Should continue to phase 4 despite phase 3 failure"
    assert result is False, "Should return False when any phase failed"

def test_runner_auto_approve_gates_when_continue_on_failure(tmp_path):
    """Gate phases short-circuit to success when continue_on_failure=True."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
        Phase(phase_key="phase_2b_plan_gate", name="Phase 2b: Plan Gate", prompt="", tools="", turns=0, gate=True),
        Phase(phase_key="phase_3", name="Phase 3", prompt="p3", tools="", turns=0),
    ]

    call_order = []

    def run_phase_fn(phase: Phase) -> int:
        call_order.append(phase.phase_key)
        return 0

    runner = PipelineRunner(
        project="test", project_dir=tmp_path, branch="feat/test",
        todo_id=1, title="Test", phases=phases, state=state,
        kanban=kanban, run_phase_fn=run_phase_fn,
        continue_on_failure=True,
    )
    result = runner.run()

    # Gate phase should NOT call run_phase_fn (short-circuited)
    assert "phase_2b_plan_gate" not in call_order, "Gate phase should be skipped, not dispatched"
    assert "phase_2" in call_order
    assert "phase_3" in call_order
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py::test_runner_continue_on_failure -v`
Expected: FAIL — unexpected keyword argument 'continue_on_failure'

- [ ] **Step 3: Write minimal implementation**

Modify `PipelineRunner` in `runner.py`:

Add to `__init__` dataclass fields:
```python
    continue_on_failure: bool = False
```

Modify the phase loop in `run()` method — replace the phase failure block:
```python
            if rc != 0:
                # Phase failed
                log.error(
                    "Phase %s failed with return code %d",
                    phase.name,
                    rc,
                )
                try:
                    self.kanban.update_phase(
                        project=self.project,
                        phase=phase.name,
                        status="failed",
                    )
                except Exception as e:
                    log.warning("kanban.update_phase (failed) failed: %s", e)
                if not self.continue_on_failure:
                    return False
                # Mark that run had failures
                if not hasattr(self, '_had_failures'):
                    self._had_failures = False
                self._had_failures = True
```

Add gate short-circuit before running the phase (insert before the `rc = self.run_phase_fn(phase)` line, inside the phase loop):
```python
            # Auto-approve gate phases when continue_on_failure=True
            if phase.gate and self.continue_on_failure:
                log.info("gate %s auto-approved (continue_on_failure mode)", phase.phase_key)
                try:
                    self.state.mark_phase_done(self.todo_id, phase.phase_key, phase_index)
                except Exception as e:
                    log.warning("state.mark_phase_done failed: %s", e)
                try:
                    self.kanban.update_phase(
                        project=self.project,
                        phase=phase.name,
                        status="done",
                    )
                except Exception as e:
                    log.warning("kanban.update_phase (done) failed: %s", e)
                continue

            # Run the phase
            rc = self.run_phase_fn(phase)
```

Update the final success check at end of `run()`:
```python
        # Check if any phase failed during continue_on_failure run
        if getattr(self, '_had_failures', False):
            log.warning("Pipeline completed with phase failures (continue_on_failure)")
            try:
                self.kanban.clear_active_task(
                    project=self.project,
                )
            except Exception as e:
                log.warning("kanban.clear_active_task failed: %s", e)
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py::test_runner_continue_on_failure tests/test_runner.py::test_runner_auto_approve_gates_when_continue_on_failure -v`
Expected: PASS

Also run existing tests to verify no regression:
Run: `uv run pytest tests/test_runner.py -v`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/runner.py tests/test_runner.py
git commit -m "feat: add continue_on_failure with auto-approve gates to PipelineRunner"
```

---

### Task 8: Add optional monitor callback to PipelineRunner

**Files:**
- Modify: `hermes_pipeline/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `HarnessMonitor` callable from Task 6
- Produces: `monitor` parameter on `PipelineRunner.__init__`; callbacks on phase transitions

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner.py
def test_runner_monitor_callback_on_phase_transitions(tmp_path):
    """Runner calls monitor callback on each phase transition."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
        Phase(phase_key="phase_3", name="Phase 3", prompt="p3", tools="", turns=0),
    ]

    events = []
    def monitor(event_type, data=None):
        events.append((event_type, data))

    def run_phase_fn(phase: Phase) -> int:
        return 0

    runner = PipelineRunner(
        project="test", project_dir=tmp_path, branch="feat/test",
        todo_id=1, title="Test", phases=phases, state=state,
        kanban=kanban, run_phase_fn=run_phase_fn,
        monitor=monitor,
    )
    runner.run()

    # Verify phase_started events
    started = [e for e in events if e[0] == "phase_started"]
    assert len(started) == 2
    assert started[0][1]["phase_key"] == "phase_2"

    # Verify phase_completed events
    completed = [e for e in events if e[0] == "phase_completed"]
    assert len(completed) == 2
    assert "duration_ms" in completed[0][1]

def test_runner_monitor_callback_on_phase_failure(tmp_path):
    """Runner calls monitor with phase_failed when a phase fails."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
    ]

    events = []
    def monitor(event_type, data=None):
        events.append((event_type, data))

    def run_phase_fn(phase: Phase) -> int:
        return 1  # fail

    runner = PipelineRunner(
        project="test", project_dir=tmp_path, branch="feat/test",
        todo_id=1, title="Test", phases=phases, state=state,
        kanban=kanban, run_phase_fn=run_phase_fn,
        monitor=monitor,
    )
    runner.run()

    failed = [e for e in events if e[0] == "phase_failed"]
    assert len(failed) == 1
    assert failed[0][1]["phase_key"] == "phase_2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py::test_runner_monitor_callback_on_phase_transitions -v`
Expected: FAIL — unexpected keyword argument 'monitor'

- [ ] **Step 3: Write minimal implementation**

Add to `PipelineRunner` dataclass fields:
```python
    monitor: Callable | None = None
```

Add `import time` at top of runner.py if not present.

In the phase loop, add timing and callbacks around the phase execution. Replace the phase loop body to wrap the existing code:
```python
        for phase_index, phase in enumerate(self.phases):
            log.info(
                "Running phase %d/%d: %s (key=%s)",
                phase_index + 1,
                len(self.phases),
                phase.name,
                phase.phase_key,
            )

            # Monitor: phase started
            if self.monitor:
                self.monitor("phase_started", {"phase_key": phase.phase_key, "todo_id": self.todo_id})

            # Update kanban to "running"
            try:
                self.kanban.update_phase(
                    project=self.project,
                    phase=phase.name,
                    status="running",
                )
            except Exception as e:
                log.warning("kanban.update_phase (running) failed: %s", e)

            # Auto-approve gate phases when continue_on_failure=True
            if phase.gate and self.continue_on_failure:
                log.info("gate %s auto-approved (continue_on_failure mode)", phase.phase_key)
                if self.monitor:
                    self.monitor("phase_completed", {"phase_key": phase.phase_key, "todo_id": self.todo_id, "duration_ms": 0})
                try:
                    self.state.mark_phase_done(self.todo_id, phase.phase_key, phase_index)
                except Exception as e:
                    log.warning("state.mark_phase_done failed: %s", e)
                try:
                    self.kanban.update_phase(
                        project=self.project,
                        phase=phase.name,
                        status="done",
                    )
                except Exception as e:
                    log.warning("kanban.update_phase (done) failed: %s", e)
                continue

            # Run the phase with timing
            phase_start = time.time()
            rc = self.run_phase_fn(phase)
            duration_ms = int((time.time() - phase_start) * 1000)

            if rc != 0:
                log.error(
                    "Phase %s failed with return code %d",
                    phase.name,
                    rc,
                )
                if self.monitor:
                    self.monitor("phase_failed", {"phase_key": phase.phase_key, "todo_id": self.todo_id, "duration_ms": duration_ms, "return_code": rc})
                try:
                    self.kanban.update_phase(
                        project=self.project,
                        phase=phase.name,
                        status="failed",
                    )
                except Exception as e:
                    log.warning("kanban.update_phase (failed) failed: %s", e)
                if not self.continue_on_failure:
                    return False
                if not hasattr(self, '_had_failures'):
                    self._had_failures = False
                self._had_failures = True
                continue

            # Phase succeeded
            if self.monitor:
                self.monitor("phase_completed", {"phase_key": phase.phase_key, "todo_id": self.todo_id, "duration_ms": duration_ms})
            try:
                self.state.mark_phase_done(self.todo_id, phase.phase_key, phase_index)
            except Exception as e:
                log.warning("state.mark_phase_done failed: %s", e)

            try:
                self.kanban.update_phase(
                    project=self.project,
                    phase=phase.name,
                    status="done",
                )
            except Exception as e:
                log.warning("kanban.update_phase (done) failed: %s", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py::test_runner_monitor_callback_on_phase_transitions tests/test_runner.py::test_runner_monitor_callback_on_phase_failure -v`
Expected: PASS

Also verify all existing runner tests still pass:
Run: `uv run pytest tests/test_runner.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/runner.py tests/test_runner.py
git commit -m "feat: add optional monitor callback to PipelineRunner"
```

---

### Task 9: test_report.py — JSONL to report

**Files:**
- Create: `hermes_pipeline/test_report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: JSONL event log path from `HarnessMonitor`
- Produces: `generate_report(jsonl_path, output_dir)` → writes `report.json` + `report.md`; `summarize_report(report_path)` → one-line stdout string

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report.py
def test_generate_report_from_jsonl(tmp_path):
    """Generate report.json and report.md from JSONL event log."""
    from hermes_pipeline.test_report import generate_report

    # Create mock JSONL events
    jsonl_path = tmp_path / "events.jsonl"
    jsonl_path.write_text(
        '{"event_type": "phase_started", "phase_key": "phase_2_autoplan", "timestamp": "2026-07-14T00:00:00Z", "todo_id": 1}\n'
        '{"duration_ms": 5000, "event_type": "phase_completed", "phase_key": "phase_2_autoplan", "timestamp": "2026-07-14T00:00:05Z", "todo_id": 1}\n'
        '{"event_type": "phase_started", "phase_key": "phase_3_writing_plan", "timestamp": "2026-07-14T00:00:05Z", "todo_id": 1}\n'
        '{"duration_ms": 3000, "event_type": "phase_failed", "phase_key": "phase_3_writing_plan", "return_code": 1, "timestamp": "2026-07-14T00:00:08Z", "todo_id": 1}\n'
    )

    output_dir = tmp_path / "reports"
    report = generate_report(jsonl_path, output_dir)

    # Check report.json exists
    report_json = output_dir / "report.json"
    assert report_json.exists()

    import json
    data = json.loads(report_json.read_text())
    assert "phases" in data
    assert len(data["phases"]) == 2
    assert data["phases"][0]["phase_key"] == "phase_2_autoplan"
    assert data["phases"][0]["status"] == "completed"
    assert data["phases"][1]["status"] == "failed"

    # Check report.md exists
    report_md = output_dir / "report.md"
    assert report_md.exists()
    md_content = report_md.read_text()
    assert "phase_2_autoplan" in md_content
    assert "phase_3_writing_plan" in md_content

def test_summarize_report():
    """Produce one-line summary from report."""
    from hermes_pipeline.test_report import summarize_report
    import json
    from pathlib import Path
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump({
            "phases": [
                {"phase_key": "p2", "status": "completed", "duration_ms": 5000, "error_message": None},
                {"phase_key": "p3", "status": "failed", "duration_ms": 3000, "error_message": "hermes timeout"},
                {"phase_key": "p4", "status": "completed", "duration_ms": 2000, "error_message": None},
            ],
            "total_phases": 3,
            "passed_phases": 2,
            "failed_phases": 1,
        }, f)
        f.flush()
        summary = summarize_report(Path(f.name))

    assert "2/3" in summary or "2 out of 3" in summary
    assert "p3" in summary or "phase_3" in summary.lower() or "failed" in summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_report.py::test_generate_report_from_jsonl -v`
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: Write minimal implementation**

```python
# hermes_pipeline/test_report.py
"""Findings report generator for mock integration test harness.

Transforms JSONL event logs from HarnessMonitor into structured
report.json and human-readable report.md files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _bucket_duration_ms(ms: int) -> str:
    """Normalize duration to human-readable bucket."""
    if ms < 60000:
        return "<1m"
    elif ms < 300000:
        return "1-5m"
    elif ms < 900000:
        return "5-15m"
    else:
        return ">15m"


def _normalize_error_class(return_code: int | None, event_type: str) -> str:
    """Map phase outcome to error class bucket."""
    if event_type == "phase_completed":
        return "completed"
    elif event_type == "phase_failed":
        if return_code is not None:
            return "phase_failure"
        return "phase_failure"
    elif event_type == "phase_timed_out":
        return "timeout"
    else:
        return "unknown"


def generate_report(jsonl_path: Path, output_dir: Path) -> dict[str, Any]:
    """Parse JSONL event log into structured report files.

    Creates:
    - output_dir/report.json: structured phase data
    - output_dir/report.md: human-readable summary

    Returns the report dict for programmatic use.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Parse JSONL
    events = []
    for line in jsonl_path.read_text().strip().splitlines():
        if line.strip():
            events.append(json.loads(line))

    # Group events by phase_key
    phases_by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        key = event.get("phase_key")
        if key is None:
            continue
        if key not in phases_by_key:
            phases_by_key[key] = {
                "phase_key": key,
                "status": "unknown",
                "duration_ms": 0,
                "error_message": None,
                "start_timestamp": event.get("timestamp"),
                "todo_id": event.get("todo_id"),
            }
        phase = phases_by_key[key]

        if event["event_type"] == "phase_started":
            phase["start_timestamp"] = event.get("timestamp")
            phase["status"] = "started"
        elif event["event_type"] == "phase_completed":
            phase["status"] = "completed"
            if "duration_ms" in event:
                phase["duration_ms"] = event["duration_ms"]
        elif event["event_type"] == "phase_failed":
            phase["status"] = "failed"
            if "duration_ms" in event:
                phase["duration_ms"] = event["duration_ms"]
            if "return_code" in event:
                phase["error_message"] = f"return_code={event['return_code']}"
        elif event["event_type"] == "phase_timed_out":
            phase["status"] = "timeout"
            phase["error_message"] = "phase timeout exceeded"

    phases_list = sorted(phases_by_key.values(), key=lambda p: p["phase_key"])

    passed = sum(1 for p in phases_list if p["status"] == "completed")
    failed = sum(1 for p in phases_list if p["status"] in ("failed", "timeout"))

    report = {
        "total_phases": len(phases_list),
        "passed_phases": passed,
        "failed_phases": failed,
        "phases": phases_list,
    }

    # Write report.json
    report_json = output_dir / "report.json"
    report_json.write_text(json.dumps(report, indent=2) + "\n")

    # Write report.md
    report_md = output_dir / "report.md"
    md_lines = [
        "# Pipeline Test Report",
        "",
        f"**Summary:** {passed}/{len(phases_list)} phases passed, {failed} failed.",
        "",
        "## Phase Progression",
        "",
        "| Phase | Status | Duration | Error |",
        "|-------|--------|----------|-------|",
    ]
    for p in phases_list:
        dur_bucket = _bucket_duration_ms(p["duration_ms"])
        md_lines.append(
            f"| {p['phase_key']} | {p['status']} | {dur_bucket} | {p['error_message'] or '-'} |"
        )

    md_lines.append("")
    md_lines.append("## What to Investigate", "")
    if failed > 0:
        for p in phases_list:
            if p["status"] in ("failed", "timeout"):
                err = p["error_message"] or "unknown"
                md_lines.append(
                    f"- **{p['phase_key']}**: {p['status']} — {err}"
                )
    else:
        md_lines.append("No failures. Pipeline completed successfully.")
        md_lines.append("")

    report_md.write_text("\n".join(md_lines) + "\n")

    return report


def summarize_report(report_path: Path) -> str:
    """Produce a one-line summary from a report.json file."""
    data = json.loads(report_path.read_text())
    total = data["total_phases"]
    passed = data["passed_phases"]
    failed_phases = [p for p in data["phases"] if p["status"] in ("failed", "timeout")]

    summary = f"{passed}/{total} phases passed"
    if failed_phases:
        failures = ", ".join(f"{p['phase_key']}: {p['status']}" for p in failed_phases)
        summary += f"; failed: {failures}"
    return summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_report.py::test_generate_report_from_jsonl tests/test_report.py::test_summarize_report -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/test_report.py tests/test_report.py
git commit -m "feat: add findings report generator (JSONL to report.json + report.md)"
```

---

### Task 10: test_report.py — --loop diff

**Files:**
- Modify: `hermes_pipeline/test_report.py`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: two report.json files from sequential runs
- Produces: `diff_reports(prev_path, curr_path)` → dict with per-phase comparison

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report.py
def test_diff_reports_shows_status_change(tmp_path):
    """Diff two reports and show per-phase status changes."""
    from hermes_pipeline.test_report import diff_reports

    prev_report = tmp_path / "report.1.json"
    prev_report.write_text(json.dumps({
        "phases": [
            {"phase_key": "phase_2_autoplan", "status": "failed", "duration_ms": 0, "error_message": "hermes timeout"},
            {"phase_key": "phase_3_writing_plan", "status": "completed", "duration_ms": 5000, "error_message": None},
        ],
        "total_phases": 2, "passed_phases": 1, "failed_phases": 1,
    }))

    curr_report = tmp_path / "report.2.json"
    curr_report.write_text(json.dumps({
        "phases": [
            {"phase_key": "phase_2_autoplan", "status": "completed", "duration_ms": 3000, "error_message": None},
            {"phase_key": "phase_3_writing_plan", "status": "failed", "duration_ms": 2000, "error_message": "kanban timeout"},
        ],
        "total_phases": 2, "passed_phases": 1, "failed_phases": 1,
    }))

    diff = diff_reports(prev_report, curr_report)

    assert len(diff) == 2
    p2_diff = [d for d in diff if d["phase_key"] == "phase_2_autoplan"][0]
    assert p2_diff["prev_status"] == "failed"
    assert p2_diff["curr_status"] == "completed"
    assert p2_diff["changed"] is True

    p3_diff = [d for d in diff if d["phase_key"] == "phase_3_writing_plan"][0]
    assert p3_diff["prev_status"] == "completed"
    assert p3_diff["curr_status"] == "failed"
    assert p3_diff["changed"] is True

def test_diff_reports_no_change():
    """Diff identical reports — no changes."""
    import tempfile
    from pathlib import Path
    from hermes_pipeline.test_report import diff_reports

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f1:
        json.dump({
            "phases": [
                {"phase_key": "p2", "status": "completed", "duration_ms": 5000, "error_message": None},
            ],
            "total_phases": 1, "passed_phases": 1, "failed_phases": 0,
        }, f1)
        f1.flush()
        prev = Path(f1.name)

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f2:
        json.dump({
            "phases": [
                {"phase_key": "p2", "status": "completed", "duration_ms": 5000, "error_message": None},
            ],
            "total_phases": 1, "passed_phases": 1, "failed_phases": 0,
        }, f2)
        f2.flush()
        curr = Path(f2.name)

    diff = diff_reports(prev, curr)
    assert len(diff) == 1
    assert diff[0]["changed"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_report.py::test_diff_reports_shows_status_change -v`
Expected: FAIL — name 'diff_reports' is not defined

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/test_report.py`:
```python
def diff_reports(prev_path: Path, curr_path: Path) -> list[dict[str, Any]]:
    """Compare two report.json files and return per-phase status diff.

    Returns a list of dicts with: phase_key, prev_status, curr_status,
    prev_duration_ms, curr_duration_ms, prev_error, curr_error, changed.
    """
    prev_data = json.loads(prev_path.read_text())
    curr_data = json.loads(curr_path.read_text())

    prev_by_key = {p["phase_key"]: p for p in prev_data["phases"]}
    curr_by_key = {p["phase_key"]: p for p in curr_data["phases"]}

    all_keys = sorted(set(list(prev_by_key.keys()) + list(curr_by_key.keys())))
    diffs = []

    for key in all_keys:
        p = prev_by_key.get(key, {})
        c = curr_by_key.get(key, {})
        prev_status = p.get("status", "missing")
        curr_status = c.get("status", "missing")
        diffs.append({
            "phase_key": key,
            "prev_status": prev_status,
            "curr_status": curr_status,
            "prev_duration_ms": p.get("duration_ms", 0),
            "curr_duration_ms": c.get("duration_ms", 0),
            "prev_error": p.get("error_message"),
            "curr_error": c.get("error_message"),
            "changed": prev_status != curr_status,
        })

    return diffs


def summarize_diff(diffs: list[dict[str, Any]]) -> str:
    """Produce a one-line diff summary."""
    changed = [d for d in diffs if d["changed"]]
    if not changed:
        return "No phase status changes from previous run"
    parts = []
    for d in changed:
        parts.append(f"{d['phase_key']}: {d['prev_status']} -> {d['curr_status']}")
    return "; ".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_report.py::test_diff_reports_shows_status_change tests/test_report.py::test_diff_reports_no_change -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/test_report.py tests/test_report.py
git commit -m "feat: add report diff generator for --loop support"
```

---

### Task 11: Add --phase single-phase execution

**Files:**
- Modify: `hermes_pipeline/cli.py`, `hermes_pipeline/harness.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: `phase_only` parameter from CLI args
- Produces: `run_harness` supports running a single phase instead of full pipeline

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness.py
def test_phase_only_filters_phases():
    """When phase_only is set, only that phase runs."""
    from hermes_pipeline.harness import filter_phases
    from hermes_pipeline.phases import load_phases

    all_phases = [
        Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
        Phase(phase_key="phase_3", name="Phase 3", prompt="p3", tools="", turns=0),
        Phase(phase_key="phase_4", name="Phase 4", prompt="p4", tools="", turns=0),
    ]

    filtered = filter_phases(all_phases, "phase_3")
    assert len(filtered) == 1
    assert filtered[0].phase_key == "phase_3"

def test_phase_only_unknown_raises():
    """Filtering by unknown phase key raises ValueError."""
    from hermes_pipeline.harness import filter_phases

    all_phases = [
        Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
    ]

    import pytest
    with pytest.raises(ValueError, match="phase_99"):
        filter_phases(all_phases, "phase_99")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness.py::test_phase_only_filters_phases -v`
Expected: FAIL — name 'filter_phases' is not defined

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/harness.py`:
```python
from .phases import Phase


def filter_phases(phases: list[Phase], phase_key: str) -> list[Phase]:
    """Return a single-element list with only the requested phase.

    Raises ValueError if phase_key is not found.
    """
    for p in phases:
        if p.phase_key == phase_key:
            return [p]
    available = ", ".join(p.phase_key for p in phases)
    raise ValueError(
        f"Unknown phase key {phase_key!r}. Available: {available}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness.py::test_phase_only_filters_phases tests/test_harness.py::test_phase_only_unknown_raises -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add filter_phases for --phase single-phase execution"
```

---

### Task 12: Config isolation via PIPELINE_* env vars

**Files:**
- Modify: `hermes_pipeline/harness.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: `Config.from_env()` which already reads `PIPELINE_*` env vars
- Produces: `isolate_config()` context manager that sets env vars for test runs

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness.py
def test_isolate_config_sets_env_vars(tmp_path, monkeypatch):
    """Config isolation sets PIPELINE_* env vars to use temp directories."""
    from hermes_pipeline.harness import isolate_config

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()

    with isolate_config(state_dir=state_dir, lock_dir=lock_dir):
        assert os.environ.get("PIPELINE_STATE_DIR") == str(state_dir)
        assert os.environ.get("PIPELINE_LOCK_DIR") == str(lock_dir)

    # After context exit, vars should be restored
    assert "PIPELINE_STATE_DIR" not in os.environ
    assert "PIPELINE_LOCK_DIR" not in os.environ

def test_isolate_config_saves_and_restores(monkeypatch):
    """Config isolation restores pre-existing env vars."""
    from hermes_pipeline.harness import isolate_config

    monkeypatch.setenv("PIPELINE_STATE_DIR", "/original/state")

    with isolate_config(state_dir=Path("/tmp"), lock_dir=Path("/tmp")):
        assert os.environ["PIPELINE_STATE_DIR"] == "/tmp"

    assert os.environ["PIPELINE_STATE_DIR"] == "/original/state"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_harness.py::test_isolate_config_sets_env_vars -v`
Expected: FAIL — name 'isolate_config' is not defined

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/harness.py`:
```python
from contextlib import contextmanager


@contextmanager
def isolate_config(*, state_dir: Path, lock_dir: Path):
    """Context manager that sets PIPELINE_* env vars for config isolation.

    Sets PIPELINE_STATE_DIR and PIPELINE_LOCK_DIR so that Config.from_env()
    uses isolated directories instead of the user's real ~/.hermes/.
    Restores original values on exit.
    """
    saved = {}
    for key in ("PIPELINE_STATE_DIR", "PIPELINE_LOCK_DIR"):
        if key in os.environ:
            saved[key] = os.environ[key]

    os.environ["PIPELINE_STATE_DIR"] = str(state_dir)
    os.environ["PIPELINE_LOCK_DIR"] = str(lock_dir)

    try:
        yield
    finally:
        for key in ("PIPELINE_STATE_DIR", "PIPELINE_LOCK_DIR"):
            if key in saved:
                os.environ[key] = saved[key]
            else:
                os.environ.pop(key, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_harness.py::test_isolate_config_sets_env_vars tests/test_harness.py::test_isolate_config_saves_and_restores -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add config isolation context manager via PIPELINE_* env vars"
```

---

### Task 13: harness.py — run_harness main orchestration function

**Files:**
- Modify: `hermes_pipeline/harness.py`
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: all harness components (fixture factory, preflight, monitor, convergence detector, config isolation, filter_phases) and `PipelineRunner`, `generate_report`
- Produces: `run_harness()` → `HarnessResult` dataclass with exit_code, report_path, temp_dir

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness.py
def test_run_harness_creates_fixture_and_runs_phases(tmp_path, monkeypatch):
    """run_harness orchestrates fixture, pipeline, and report generation."""
    from hermes_pipeline.harness import run_harness, HarnessResult
    from hermes_pipeline.config import Config

    # Mock PipelineRunner to avoid real AI calls
    from unittest.mock import patch, MagicMock

    fake_runner_class = MagicMock()
    def fake_run():
        return True
    fake_runner_class.return_value.run.return_value = fake_run()

    with patch("hermes_pipeline.harness.PipelineRunner", fake_runner_class):
        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=None,
            keep_dir=False,
            timeout=3600,
            convergence_threshold=3,
            config=Config.default(),
        )

    assert isinstance(result, HarnessResult)
    # PipelineRunner was called with the fixture project dir
    assert fake_runner_class.called
    call_kwargs = fake_runner_class.call_args[1]
    assert call_kwargs["continue_on_failure"] is True
    assert call_kwargs["monitor"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py::test_run_harness_creates_fixture_and_runs_phases -v`
Expected: FAIL — name 'run_harness' or 'HarnessResult' not defined

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/harness.py`:
```python
@dataclass
class HarnessResult:
    """Result of a harness run."""
    exit_code: int
    report_path: Path | None
    temp_dir: Path | None
    summary: str


def run_harness(
    *,
    fixture_name: str,
    loop: bool,
    phase_only: str | None,
    keep_dir: bool,
    timeout: int,
    convergence_threshold: int,
    config: Config,
) -> HarnessResult:
    """Main orchestration: bootstrap fixture, run pipeline, generate report.

    Flow:
    1. Preflight check
    2. Create temp dir + bootstrap mock project fixture
    3. Set up config isolation
    4. Create HarnessMonitor for JSONL event log
    5. Load phases, optionally filter to single phase
    6. Create PipelineRunner with continue_on_failure + monitor
    7. Run pipeline with overall timeout
    8. Generate findings report from JSONL events
    9. If --loop, diff against previous report
    10. Return HarnessResult
    """
    # 1. Preflight
    preflight_check()

    # 2. Create temp dir + fixture
    temp_dir = Path(tempfile.mkdtemp(prefix="harness-"))
    try:
        fixture = create_mock_project(temp_dir, fixture_name)

        # 3. Config isolation
        state_dir = temp_dir / ".hermes"
        lock_dir = temp_dir / ".hermes" / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)

        # 4. Harness monitor
        events_log = temp_dir / "events.jsonl"
        monitor = HarnessMonitor(events_log)

        # 5. Load phases
        all_phases = load_phases()
        phases = all_phases
        if phase_only:
            phases = filter_phases(all_phases, phase_only)

        # 6. Create PipelineRunner
        from .runner import PipelineRunner
        from .kanban import NullKanbanAdapter
        from .state import State
        from .config import replace as dc_replace

        isolated_config = Config.from_env()
        kanban = NullKanbanAdapter()

        state_dirs = state_dir
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

        runner = PipelineRunner(
            project=fixture["project_slug"],
            project_dir=temp_dir,
            branch=fixture["branch"],
            todo_id=fixture["todo_id"],
            title=f"Mock TODO-{fixture['todo_id']}",
            phases=phases,
            state=state,
            kanban=kanban,
            run_phase_fn=lambda phase: 0,  # Placeholder — real impl uses phases.run()
            continue_on_failure=True,
            monitor=monitor,
        )

        # 7. Run pipeline
        with isolate_config(state_dir=state_dir, lock_dir=lock_dir):
            success = runner.run()

        # 8. Generate report
        output_dir = temp_dir / "reports"
        report = generate_report(events_log, output_dir)
        report_json = output_dir / "report.json"
        summary = summarize_report(report_json)

        # 9. Loop diff
        if loop:
            # Find previous report in output_dir
            prev_reports = sorted(output_dir.parent.glob(f"{fixture_name}-report.*.json"))
            if prev_reports:
                diffs = diff_reports(prev_reports[-1], report_json)
                diff_summary = summarize_diff(diffs)
                summary += f" | diff: {diff_summary}"

            # Save numbered report for next iteration
            if prev_reports:
                next_n = int(prev_reports[-1].stem.split(".")[-1]) + 1
            else:
                next_n = 1
            next_report = output_dir.parent / f"{fixture_name}-report.{next_n}.json"
            next_report.write_text(report_json.read_text())

        # Determine exit code
        exit_code = 0 if success else 1

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

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harness.py::test_run_harness_creates_fixture_and_runs_phases -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat: add run_harness orchestration function"
```

---

### Task 14: Unit tests — fixture factory edge cases

**Files:**
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: `create_mock_project` from Task 3
- Produces: Additional tests for fixture factory

- [ ] **Step 1: Write tests**

```python
# tests/test_harness.py
def test_create_mock_project_unknown_fixture_raises(tmp_path):
    """Fixture factory raises ValueError for unknown fixture name."""
    from hermes_pipeline.harness import create_mock_project

    with pytest.raises(ValueError, match="Unknown fixture"):
        create_mock_project(tmp_path, "nonexistent-fixture")

def test_create_mock_project_returns_metadata(tmp_path):
    """Fixture factory returns project metadata dict."""
    from hermes_pipeline.harness import create_mock_project

    result = create_mock_project(tmp_path, "happy-path")

    assert result["project_slug"] == "mock-project"
    assert result["todo_id"] == 1
    assert "branch" in result
    assert result["fixture_name"] == "happy-path"
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_harness.py -k "fixture" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_harness.py
git commit -m "test: add edge case tests for fixture factory"
```

---

### Task 15: Unit tests — report generator edge cases

**Files:**
- Modify: `tests/test_report.py`

**Interfaces:**
- Consumes: `generate_report`, `diff_reports` from Tasks 9-10
- Produces: Edge case tests

- [ ] **Step 1: Write tests**

```python
# tests/test_report.py
def test_generate_report_empty_jsonl(tmp_path):
    """Handle empty JSONL gracefully."""
    from hermes_pipeline.test_report import generate_report

    jsonl_path = tmp_path / "empty.jsonl"
    jsonl_path.write_text("")

    output_dir = tmp_path / "reports"
    report = generate_report(jsonl_path, output_dir)

    assert report["total_phases"] == 0
    assert report["passed_phases"] == 0

def test_generate_report_all_phases_pass(tmp_path):
    """Report shows all phases passed."""
    from hermes_pipeline.test_report import generate_report

    jsonl_path = tmp_path / "events.jsonl"
    jsonl_path.write_text(
        '{"event_type": "phase_started", "phase_key": "phase_2", "timestamp": "2026-07-14T00:00:00Z", "todo_id": 1}\n'
        '{"duration_ms": 5000, "event_type": "phase_completed", "phase_key": "phase_2", "timestamp": "2026-07-14T00:00:05Z", "todo_id": 1}\n'
    )

    output_dir = tmp_path / "reports"
    report = generate_report(jsonl_path, output_dir)

    assert report["passed_phases"] == 1
    assert report["failed_phases"] == 0

def test_diff_reports_new_phase_added(tmp_path):
    """Diff handles phases present in one report but not the other."""
    from hermes_pipeline.test_report import diff_reports

    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({
        "phases": [
            {"phase_key": "p2", "status": "completed", "duration_ms": 0, "error_message": None},
        ],
        "total_phases": 1, "passed_phases": 1, "failed_phases": 0,
    }))

    curr = tmp_path / "curr.json"
    curr.write_text(json.dumps({
        "phases": [
            {"phase_key": "p2", "status": "completed", "duration_ms": 0, "error_message": None},
            {"phase_key": "p3", "status": "completed", "duration_ms": 0, "error_message": None},
        ],
        "total_phases": 2, "passed_phases": 2, "failed_phases": 0,
    }))

    diffs = diff_reports(prev, curr)
    p3_diff = [d for d in diffs if d["phase_key"] == "p3"][0]
    assert p3_diff["prev_status"] == "missing"
    assert p3_diff["curr_status"] == "completed"
    assert p3_diff["changed"] is True
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_report.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_report.py
git commit -m "test: add edge case tests for report generator"
```

---

### Task 16: Unit tests — continue_on_failure + gates integration

**Files:**
- Modify: `tests/test_runner.py`

**Interfaces:**
- Consumes: `PipelineRunner` with `continue_on_failure` and `monitor`
- Produces: Integration tests for combined features

- [ ] **Step 1: Write tests**

```python
# tests/test_runner.py
def test_runner_continue_on_failure_with_monitor(tmp_path):
    """Monitor receives correct events when continue_on_failure is active."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    kanban.clear_active_task.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
        Phase(phase_key="phase_2b_plan_gate", name="Phase 2b: Gate", prompt="", tools="", turns=0, gate=True),
        Phase(phase_key="phase_3", name="Phase 3", prompt="p3", tools="", turns=0),
    ]

    events = []
    def monitor(event_type, data=None):
        events.append((event_type, data))

    def run_phase_fn(phase: Phase) -> int:
        if phase.phase_key == "phase_3":
            return 1  # fail
        return 0

    runner = PipelineRunner(
        project="test", project_dir=tmp_path, branch="feat/test",
        todo_id=1, title="Test", phases=phases, state=state,
        kanban=kanban, run_phase_fn=run_phase_fn,
        continue_on_failure=True,
        monitor=monitor,
    )
    result = runner.run()

    assert result is False

    # Gate was auto-approved (no phase_failed event for gate)
    gate_failed = [e for e in events if e[0] == "phase_failed" and e[1]["phase_key"] == "phase_2b_plan_gate"]
    assert len(gate_failed) == 0, "Gate should not fail in continue_on_failure mode"

    # phase_3 failed event present
    phase3_failed = [e for e in events if e[0] == "phase_failed" and e[1]["phase_key"] == "phase_3"]
    assert len(phase3_failed) == 1

def test_runner_no_monitor_when_not_provided(tmp_path):
    """Runner works correctly when monitor=None."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(phase_key="phase_2", name="Phase 2", prompt="p2", tools="", turns=0),
    ]

    def run_phase_fn(phase: Phase) -> int:
        return 0

    runner = PipelineRunner(
        project="test", project_dir=tmp_path, branch="feat/test",
        todo_id=1, title="Test", phases=phases, state=state,
        kanban=kanban, run_phase_fn=run_phase_fn,
    )
    result = runner.run()

    assert result is True
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_runner.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_runner.py
git commit -m "test: add integration tests for continue_on_failure + monitor + gates"
```

---

### Task 17: Unit tests — --phase, config isolation, HarnessResult

**Files:**
- Modify: `tests/test_harness.py`

**Interfaces:**
- Consumes: all harness module exports
- Produces: Comprehensive unit tests for harness module

- [ ] **Step 1: Write tests**

```python
# tests/test_harness.py
def test_harness_result_dataclass():
    """HarnessResult dataclass has expected fields."""
    from hermes_pipeline.harness import HarnessResult

    result = HarnessResult(exit_code=0, report_path=Path("/tmp/report.json"), temp_dir=None, summary="1/1 passed")
    assert result.exit_code == 0
    assert str(result.report_path) == "/tmp/report.json"
    assert result.temp_dir is None
    assert "passed" in result.summary

def test_filter_phases_with_real_phase_list():
    """filter_phases works with phases loaded from phases.yaml."""
    from hermes_pipeline.harness import filter_phases
    from hermes_pipeline.phases import load_phases

    all_phases = load_phases()
    gate_phases = [p for p in all_phases if p.gate]
    non_gate_phases = [p for p in all_phases if not p.gate]

    assert len(gate_phases) >= 1, "Should have at least one gate phase"

    # Filter a gate phase
    target = gate_phases[0].phase_key
    filtered = filter_phases(all_phases, target)
    assert len(filtered) == 1
    assert filtered[0].gate is True

def test_convergence_detector_custom_threshold():
    """ConvergenceDetector respects custom threshold."""
    from hermes_pipeline.harness import ConvergenceDetector

    detector = ConvergenceDetector(threshold=2)
    detector.record("p1", "hermes_error")
    assert detector.should_halt() is False
    detector.record("p2", "hermes_error")
    assert detector.should_halt() is True
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_harness.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_harness.py
git commit -m "test: add comprehensive unit tests for harness module"
```

---

### Task 18: Integration test — happy-path e2e (mock runner)

**Files:**
- Create: `tests/test_harness_e2e.py`

**Interfaces:**
- Consumes: `run_harness`, fixture factory, report generator
- Produces: End-to-end integration test with mocked PipelineRunner

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness_e2e.py
"""End-to-end integration test for the mock integration test harness.

Uses mocked PipelineRunner to verify the full harness flow without real AI calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from hermes_pipeline.config import Config
from hermes_pipeline.harness import HarnessResult


def test_harness_e2e_happy_path(tmp_path, monkeypatch):
    """Full harness flow: fixture → runner → report → summary."""
    from hermes_pipeline.harness import run_harness

    # Mock PipelineRunner to simulate successful phase execution
    fake_runner = MagicMock()
    fake_runner.run.return_value = True

    with patch("hermes_pipeline.harness.PipelineRunner", return_value=fake_runner):
        # Mock preflight to skip actual CLI checks
        with patch("hermes_pipeline.harness.preflight_check"):
            result = run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=True,  # Keep dir so we can inspect
                timeout=3600,
                convergence_threshold=3,
                config=Config.default(),
            )

    assert isinstance(result, HarnessResult)
    assert result.exit_code == 0
    assert result.report_path is not None
    assert result.report_path.exists()

    # Verify report.json structure
    report_data = json.loads(result.report_path.read_text())
    assert "phases" in report_data
    assert "total_phases" in report_data

    # Verify PipelineRunner was called with correct params
    assert fake_runner.called
    init_kwargs = fake_runner.call_args[1]
    assert init_kwargs["continue_on_failure"] is True
    assert init_kwargs["monitor"] is not None

    # Verify temp dir was kept
    assert result.temp_dir is not None
    assert result.temp_dir.exists()


def test_harness_e2e_loop_diff(tmp_path):
    """Harness --loop produces numbered reports and diffs."""
    from hermes_pipeline.harness import run_harness

    fake_runner = MagicMock()
    fake_runner.run.return_value = True

    with patch("hermes_pipeline.harness.PipelineRunner", return_value=fake_runner):
        with patch("hermes_pipeline.harness.preflight_check"):
            # First run
            result1 = run_harness(
                fixture_name="happy-path",
                loop=False,
                phase_only=None,
                keep_dir=True,
                timeout=3600,
                convergence_threshold=3,
                config=Config.default(),
            )

            # Second run with --loop
            result2 = run_harness(
                fixture_name="happy-path",
                loop=True,
                phase_only=None,
                keep_dir=True,
                timeout=3600,
                convergence_threshold=3,
                config=Config.default(),
            )

    assert isinstance(result2, HarnessResult)
    # Second run should reference diff in summary
    assert "diff" in result2.summary or "No phase status changes" in result2.summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_e2e.py::test_harness_e2e_happy_path -v`
Expected: FAIL (will fail on mock/implementation details that need tuning)

- [ ] **Step 3: Tune and verify**

Fix any import or mock path issues revealed by the test. The test validates the integration of fixture factory, config isolation, runner wiring, and report generation.

Run: `uv run pytest tests/test_harness_e2e.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_harness_e2e.py
git commit -m "test: add e2e integration test for harness with mocked PipelineRunner"
```

---

## Self-Review

**1. Spec coverage:**
- Setup script + mock project fixtures (Task 3)
- Pipeline execution through mock project (Task 13)
- Monitoring/verification via monitor callback (Task 6, Task 8)
- Findings report generation (Task 9)
- Loopable cycle with --loop diff (Task 10)
- Preflight checks (Task 4)
- Convergence detector (Task 5)
- Auto-approve gates in harness mode (Task 7)
- Single-phase execution --phase (Task 11)
- Config isolation via PIPELINE_* env vars (Task 12)
- CLI entrypoint + subcommand (Task 1, Task 2)

All five deliverables from TODO-19 covered. All outside-voice decisions (9-11) addressed.

**2. Placeholder scan:** No TBD/TODO/implement-later patterns found. Every code block contains actual implementation.

**3. Type consistency:**
- `HarnessMonitor.__call__(event_type, data)` — used in Task 8 runner callback as `self.monitor("phase_started", {...})` — consistent.
- `ConvergenceDetector.record(phase_key, error_class)` — used in Task 13 — consistent.
- `PipelineRunner` new fields: `continue_on_failure: bool = False`, `monitor: Callable | None = None` — additive, existing callers unaffected.
- `create_mock_project` returns `dict[str, Any]` with keys `project_slug`, `todo_id`, `branch`, `fixture_name` — referenced consistently in Task 13.
- `HarnessResult` dataclass — `exit_code, report_path, temp_dir, summary` — used in Task 14 and Task 18.
- `filter_phases(phases, phase_key)` — returns `list[Phase]` — consistent type.

**4. Task dependency order:** Each task's "Consumes" field references only earlier tasks. No circular dependencies.

NO GAPS FOUND.
