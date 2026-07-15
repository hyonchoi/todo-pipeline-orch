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
from typing import Any

log = logging.getLogger(__name__)


def create_mock_project(path: Path, fixture_name: str) -> dict[str, Any]:
    """Create a mock project in *path* for integration testing."""
    path.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@localhost"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)

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
    """Context manager that sets PIPELINE_* env vars for config isolation."""
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
    from .runner import PipelineRunner
    from .phases import load_phases
    from .test_report import generate_report, summarize_report, diff_reports, summarize_diff
    from .state import State
    from .kanban import NullKanbanAdapter

    preflight_check()

    temp_dir = Path(tempfile.mkdtemp(prefix="harness-"))
    try:
        fixture = create_mock_project(temp_dir, fixture_name)

        state_dir = temp_dir / ".hermes"
        lock_dir = temp_dir / ".hermes" / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)

        events_log = temp_dir / "events.jsonl"
        monitor = HarnessMonitor(events_log)

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

        runner = PipelineRunner(
            project=fixture["project_slug"],
            project_dir=temp_dir,
            branch=fixture["branch"],
            todo_id=fixture["todo_id"],
            title=f"Mock TODO-{fixture['todo_id']}",
            phases=phases,
            state=state,
            kanban=kanban,
            run_phase_fn=lambda phase: 0,
            continue_on_failure=True,
            monitor=monitor,
        )

        with isolate_config(state_dir=state_dir, lock_dir=lock_dir):
            success = runner.run()

        output_dir = temp_dir / "reports"
        report = generate_report(events_log, output_dir)
        report_json = output_dir / "report.json"
        summary = summarize_report(report_json)

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
