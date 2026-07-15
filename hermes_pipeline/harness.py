"""Mock integration test harness — fixture factory, preflight, runner."""

from __future__ import annotations

import json as _json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


def create_mock_project(path: Path, fixture_name: str) -> dict[str, Any]:
    """Create a mock project in *path* for integration testing."""
    path.mkdir(parents=True, exist_ok=True)

    _env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, env=_env)
    subprocess.run(["git", "config", "user.email", "test@localhost"], cwd=path, check=True, capture_output=True, env=_env)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True, env=_env)

    todos_content = _get_todos_for_fixture(fixture_name)
    (path / "TODOS.md").write_text(todos_content)
    (path / "README.md").write_text(f"# Mock Project — {fixture_name}\n")

    hermes_dir = path / ".hermes"
    hermes_dir.mkdir()
    (path / ".hermes" / "config.toml").write_text(_get_hermes_config())
    (path / ".hermes" / "todo_id_counter").write_text("0")

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
    return 1


def preflight_check() -> None:
    """Verify required CLI tools are available."""
    from .hermes_adapter import ClaudeDependencyError, HermesDependencyError

    if shutil.which("git") is None:
        raise RuntimeError(
            "Missing dependency: git — Git is not installed or not on PATH. "
            "Install: https://git-scm.com"
        )
    if shutil.which("hermes") is None:
        raise HermesDependencyError(
            "Hermes CLI is not installed or not on PATH. Install: https://hermos.dev"
        )
    if shutil.which("claude") is None:
        raise ClaudeDependencyError(
            "Claude Code CLI is not installed or not on PATH. Install: https://claude.ai/code"
        )


@dataclass
class ConvergenceDetector:
    """Track consecutive same-class phase failures within a single run."""
    threshold: int = 3
    _consecutive: int = field(default=0, repr=False)
    _last_error_class: str | None = field(default=None, repr=False)

    def record(self, phase_key: str, error_class: str | None) -> None:
        if error_class is None:
            self._consecutive = 0
            self._last_error_class = None
        elif error_class == self._last_error_class:
            self._consecutive += 1
        else:
            self._consecutive = 1
            self._last_error_class = error_class

    def should_halt(self) -> bool:
        return self._consecutive >= self.threshold


class HarnessMonitor:
    """Write pipeline events to a JSONL log file."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._start_time: float | None = None

    def __call__(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_type": event_type,
        }
        if data:
            event.update(data)
        if event_type == "phase_started" and self._start_time is None:
            self._start_time = time.time()
            event["run_start_time"] = event["timestamp"]
        with open(self.log_path, "a") as f:
            f.write(_json.dumps(event, sort_keys=True) + "\n")


from .phases import Phase


class ConvergenceHaltError(Exception):
    """Raised when the convergence detector halts a run mid-phase-loop."""


def _classify_error_class(exc: Exception) -> str:
    """Bucket an exception into a coarse error class for convergence tracking / reports."""
    from .hermes_adapter import (
        ClaudeCallError,
        ClaudeDependencyError,
        HermesCallError,
        HermesDependencyError,
    )

    if isinstance(exc, (HermesDependencyError, ClaudeDependencyError)):
        return "dependency_error"
    if isinstance(exc, HermesCallError):
        return "hermes_error"
    if isinstance(exc, ClaudeCallError):
        return "claude_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "phase_failure"


def _dispatch_phase(
    phase: Phase,
    *,
    state_dir: Path,
    todo_id: int,
    tick_id: str,
    project_slug: str,
    project_dir: Path,
    error_holder: dict[str, Any],
) -> int:
    """Run a single phase through the real pipeline entrypoint (`phases.run`).

    This dispatches actual Hermes / Claude Code subprocess calls — the same
    code path production pipeline ticks use — rather than a stub. Returns
    0 on success, 1 on failure (classified error stashed in error_holder for
    the monitor wrapper to attach to the phase_failed event).
    """
    from .phases import run as phases_run

    try:
        phases_run(
            state_dir=state_dir,
            todo_id=f"TODO-{todo_id}" if isinstance(todo_id, int) else todo_id,
            tick_id=tick_id,
            phase_key=phase.phase_key,
            project_slug=project_slug,
            project_dir=project_dir,
        )
        return 0
    except Exception as e:  # noqa: BLE001 - classify and report, don't crash the harness
        error_holder["error_class"] = _classify_error_class(e)
        error_holder["error_message"] = str(e)[:500]
        log.warning("phase %s failed: %s", phase.phase_key, e)
        return 1


class _ConvergenceMonitor:
    """Wraps a monitor callback: forwards events, feeds the convergence detector,
    and tracks the currently in-flight phase for partial-report generation on
    overall-timeout. Raises ConvergenceHaltError if the detector trips.
    """

    def __init__(
        self,
        inner: Callable[[str, dict[str, Any] | None], None],
        detector: "ConvergenceDetector",
        error_holder: dict[str, Any],
    ) -> None:
        self._inner = inner
        self._detector = detector
        self._holder = error_holder
        self.current_phase_key: str | None = None

    def __call__(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        data = dict(data or {})

        if event_type == "phase_started":
            self.current_phase_key = data.get("phase_key")
            self._inner(event_type, data)
            return

        if event_type == "phase_failed":
            error_class = self._holder.pop("error_class", "phase_failure")
            data["error_class"] = error_class
            self._inner(event_type, data)
            self._detector.record(data.get("phase_key", ""), error_class)
            if self._detector.should_halt():
                raise ConvergenceHaltError(
                    f"convergence detector: {self._detector.threshold}+ consecutive "
                    f"{error_class} failures, halting run"
                )
            return

        if event_type == "phase_completed":
            self._inner(event_type, data)
            self._detector.record(data.get("phase_key", ""), None)
            return

        self._inner(event_type, data)


def filter_phases(phases: list[Phase], phase_key: str) -> list[Phase]:
    """Return a single-element list with only the requested phase."""
    for p in phases:
        if p.phase_key == phase_key:
            return [p]
    available = ", ".join(p.phase_key for p in phases)
    raise ValueError(
        f"Unknown phase key {phase_key!r}. Available: {available}"
    )


@contextmanager
def isolate_config(*, state_dir: Path, lock_dir: Path):
    """Context manager that sets PIPELINE_* env vars for config isolation.

    HOME is left untouched — the harness invokes real hermes/claude CLI
    subprocesses, which need the real $HOME to read auth credentials.
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


@dataclass
class HarnessResult:
    """Result of a harness run."""
    exit_code: int
    report_path: Path | None
    temp_dir: Path | None
    summary: str


def _kill_hung_phase_subprocess(*, state_dir: Path, todo_id: int) -> None:
    """Kill a hermes/claude subprocess left running after overall --timeout fires.

    The phase-level timeout (hermes_adapter.py:255) eventually cleans it up,
    but --loop iterations shouldn't accumulate live subprocesses in the meantime.
    """
    import signal as _signal

    marker_path = Path(state_dir) / "phase_started" / f"TODO-{todo_id}.json"
    try:
        marker = _json.loads(marker_path.read_text())
    except (FileNotFoundError, _json.JSONDecodeError):
        return
    pid = marker.get("child_pid")
    if pid is None:
        return
    try:
        # Verify the PID is still alive before attempting to kill the session group.
        # os.kill(pid, 0) raises ProcessLookupError if the process already exited,
        # which prevents SIGKILL from hitting an unrelated process that reused the PID.
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        # Process exists but we can't signal it; skip killpg to avoid collateral damage.
        return
    try:
        # phases.py spawns subprocesses with start_new_session=True, so pid is a
        # session leader and killpg(pgid=pid) targets only that process's group.
        os.killpg(pid, _signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as e:
        log.warning("failed to kill hung phase subprocess pid=%s: %s", pid, e)


def run_harness(
    *,
    fixture_name: str,
    loop: bool,
    phase_only: str | None,
    keep_dir: bool,
    timeout: int,
    convergence_threshold: int,
    config: Any,
) -> HarnessResult:
    """Main orchestration: bootstrap fixture, run pipeline, generate report."""
    import threading

    from .runner import PipelineRunner
    from .phases import load_phases
    from .test_report import generate_report, summarize_report, diff_reports, summarize_diff
    from .state import State
    from .kanban import NullKanbanAdapter
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

        kanban = NullKanbanAdapter()
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

        tick_id = new_tick_id()

        runner = PipelineRunner(
            project=fixture["project_slug"],
            project_dir=temp_dir,
            branch=fixture["branch"],
            todo_id=fixture["todo_id"],
            title=f"Mock TODO-{fixture['todo_id']}",
            phases=phases,
            state=state,
            kanban=kanban,
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

        timed_out = False
        with isolate_config(state_dir=state_dir, lock_dir=lock_dir):
            result_box: dict[str, Any] = {}

            def _run_and_capture() -> None:
                try:
                    result_box["success"] = runner.run()
                except ConvergenceHaltError as e:
                    result_box["convergence_error"] = e
                except Exception as e:  # noqa: BLE001 - surfaced via result_box
                    result_box["exception"] = e

            worker = threading.Thread(target=_run_and_capture, daemon=True)
            worker.start()
            worker.join(timeout=timeout)

            if worker.is_alive():
                timed_out = True
                success = False
            elif "convergence_error" in result_box:
                log.warning(str(result_box["convergence_error"]))
                success = False
            elif "exception" in result_box:
                raise result_box["exception"]
            else:
                success = result_box["success"]

        if timed_out and monitor.current_phase_key:
            # Overall timeout fired mid-phase: record it so the report reflects
            # a timeout rather than silently truncating the event log.
            base_monitor("phase_timed_out", {"phase_key": monitor.current_phase_key})
            _kill_hung_phase_subprocess(state_dir=state_dir, todo_id=fixture["todo_id"])

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
