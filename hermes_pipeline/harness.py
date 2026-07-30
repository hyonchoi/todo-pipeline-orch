"""Mock integration test harness — fixture factory, preflight, runner."""

from __future__ import annotations

import json as _json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PromptClient

log = logging.getLogger(__name__)


_MOCK_PROJECT_GITIGNORE = """\
# Harness runtime artifacts
.hermes/outcomes/
.hermes/tpo-config.yaml

# Agent scratch space
.superpowers/
.code-review-graph/

# Python runtime artifacts
__pycache__/
*.py[cod]
"""


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
    (path / ".gitignore").write_text(_MOCK_PROJECT_GITIGNORE)

    hermes_dir = path / ".hermes"
    hermes_dir.mkdir()

    # Create pipeline.toml contract for assignee configuration
    pipeline_toml = (
        "# Pipeline execution contract — read at tick start.\n"
        "# See docs/tutorial-getting-started.md and `tpo doctor --help`.\n"
        "schema_version = 2\n"
        'assignee = "pipeline"\n'
        'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
    )
    (path / ".hermes" / "pipeline.toml").write_text(pipeline_toml)

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
            "NEXT_TODO_ID: 2\n\n"
            "> **Format rules (enforced by `todos-manager` skill):**\n"
            "> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`\n"
            "> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold\n"
            "> - Required fields: **What:**, **Why:**, **Decisions:**\n"
            "> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Spec:**, **Reference:**\n"
            "> - **Spec:**/**Reference:** are `--revise`-only (never suggested by `--add` or auto-research); always typed verbatim\n"
            "> - ID: sequential, immutable TODO-<n>\n"
            "> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`\n\n"
            "- [ ] **TODO-1: Implement mock name normalization** — adds a deterministic Python data transformation\n"
            "  - **What:** Create `mock_transform.py` with `normalize_names(names: list[str]) -> list[str]`. For each input string, strip surrounding whitespace, discard empty strings after stripping, lowercase the remaining value, and preserve input order. Return an empty list for empty input.\n"
            "  - **Why:** Provide a small, executable feature that exercises the complete harness pipeline without external services.\n"
            "  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `feat/mock-happy-path`, Language `Python 3.12+`, Dependencies `standard library only`, Test Coverage `required`, Security Review `not-required`\n"
            "  - **Acceptance criteria:** `normalize_names([\" Alice \", \"\", \"BOB\"])` returns `[\"alice\", \"bob\"]`; `normalize_names([])` returns `[]`; tests run with `uv run pytest`.\n"
        )
    else:
        raise ValueError(f"Unknown fixture: {fixture_name}")


def _get_todo_id_for_fixture(fixture_name: str) -> int:
    return 1


def preflight_check(*, prompt_client: PromptClient = "claude") -> None:
    """Verify required CLI tools are available."""
    from .hermes_adapter import AgentClientDependencyError, HermesDependencyError

    if shutil.which("git") is None:
        raise RuntimeError(
            "Missing dependency: git — Git is not installed or not on PATH. "
            "Install: https://git-scm.com"
        )
    if shutil.which("hermes") is None:
        raise HermesDependencyError(
            "Hermes CLI is not installed or not on PATH. Install: https://hermos.dev"
        )
    selected_product = {
        "claude": "Claude Code",
        "codex": "Codex",
    }[prompt_client]
    if shutil.which(prompt_client) is None:
        raise AgentClientDependencyError(
            f"{prompt_client} CLI for the selected prompt client "
            f"({selected_product}) is not installed or not on PATH."
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
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_type": event_type,
        }
        if data:
            event.update(data)
        if event_type == "phase_started" and self._start_time is None:
            self._start_time = time.time()
            event["run_start_time"] = event["timestamp"]
        with open(self.log_path, "a") as f:
            f.write(_json.dumps(event, sort_keys=True) + "\n")


from .phases import Phase  # noqa: E402


class ConvergenceHaltError(Exception):
    """Raised when the convergence detector halts a run mid-phase-loop."""


class KanbanPreflightError(RuntimeError):
    """Raised when --kanban hermes is selected but the tenant is not accessible."""


class HarnessCleanupError(RuntimeError):
    """Raised when timeout cleanup cannot prove the workspace is quiescent."""


class PollCancellationError(RuntimeError):
    """Raised when the polling thread ignores cooperative cancellation."""


# Preflight timeout for hermes kanban list (seconds)
_PREFLIGHT_TIMEOUT = 15

# Maximum poll interval for kanban-as-scheduler phase polling (seconds)
_KANBAN_POLL_MAX_INTERVAL = 30.0

# Maximum cooperative join after the overall timeout.
_POLL_CANCELLATION_TIMEOUT = 30.0

# Maximum characters in error messages captured by the harness
_ERROR_MESSAGE_MAX = 500

_OFFLINE_TERMINAL_PHASE_KEY = "phase_8_finish_branch"
_OFFLINE_TERMINAL_PROMPT = """\
Run the harness local terminal workflow.

This disposable fixture intentionally has no Git remote. Do not invoke the
ship skill, and do not push or open a pull request.

1. Run the TODO acceptance tests and any repository tests added by prior phases.
2. Inspect the complete local diff and commit every intended fixture change.
3. Write the current branch name to `.hermes/pipeline_branch.txt` if needed and
   include that state in the final local commit.
4. Verify `git status --porcelain` is empty.

Complete this task only when the local branch is tested, clean, and fully
committed. Exit non-zero if any local verification or commit cannot complete.
"""


def _kanban_preflight(*, tenant: str) -> None:
    """Fail fast if the kanban tenant isn't accessible before constructing the real adapter.

    Runs `hermes kanban list --tenant <tenant>` and raises KanbanPreflightError with an
    actionable message on non-zero exit, rather than letting the failure surface later as a
    silent non-blocking warning deep in HermesKanbanAdapter.
    """
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--tenant", tenant],
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise KanbanPreflightError(
            f"Preflight check timed out after {_PREFLIGHT_TIMEOUT}s: `hermes kanban list --tenant {tenant}` "
            f"did not respond. Verify your --kanban tenant is correct and reachable."
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise KanbanPreflightError(
            f"--kanban hermes requires `hermes login` and access to tenant '{tenant}'. "
            f"Verify with: hermes kanban list --tenant {tenant}\n"
            f"Preflight error: {detail}"
        )


def _auto_complete_gate_tasks(
    tenant: str,
    tick_id: str,
    *,
    completed_phase_key: str,
    phases: list[Phase] | None = None,
) -> None:
    """Complete blocked gate tasks whose direct predecessor just finished.

    Gate tasks are created as blocked with --parent pointing to their
    predecessor. In kanban-as-scheduler mode, the kanban board should
    unblock them when the parent finishes. However, if the kanban board
    doesn't propagate the unblock signal, we auto-complete the gate to
    let child phases proceed.

    Only completes gates whose predecessor matches completed_phase_key,
    preventing gates from auto-completing at registration time before
    their parent phase has run.

    Best-effort: exceptions are logged, not raised.
    """
    from .kanban_tasks import BLOCKED, complete_todo_kanban_task, get_todo_kanban_tasks

    try:
        tasks = get_todo_kanban_tasks(tenant, tick_id)
    except Exception as e:
        log.warning("failed to query kanban tasks for gate auto-complete: %s", e)
        return

    # Build predecessor map from the same phase list used for registration.
    # When --phase is used, only a subset of phases is registered — using
    # load_phases() (all phases) would create predecessor mappings for
    # phases that don't exist as kanban tasks, causing gates to never match.
    if phases is None:
        from .phases import load_phases
        phases = load_phases()
    gate_predecessor = {}
    for i, phase in enumerate(phases):
        if getattr(phase, "gate", False) and i > 0:
            gate_predecessor[phase.phase_key] = phases[i - 1].phase_key

    for phase_key, info in tasks.items():
        if info.status != BLOCKED:
            continue
        # Only auto-complete gates whose predecessor just finished.
        pred = gate_predecessor.get(phase_key)
        if pred is None or pred != completed_phase_key:
            continue
        if complete_todo_kanban_task(tenant, info.task_id):
            log.info("auto-completed gate task %s (%s) after %s done", info.task_id, phase_key, completed_phase_key)
        else:
            log.warning("gate task %s (%s) remains blocked: auto-complete after %s done failed", info.task_id, phase_key, completed_phase_key)


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
    max_poll_interval: float = _KANBAN_POLL_MAX_INTERVAL,
    phases: list[Phase] | None = None,
    prompt_client: PromptClient = "claude",
    cancel_event: Any = None,
) -> bool:
    """Poll kanban-as-scheduler phases to completion.

    1. Registers all phases as kanban tasks (register_todo_phases).
    2. Auto-completes gate tasks so child phases become ready.
    3. Polls get_todo_kanban_status() until all phases terminal.
    4. Emits JSONL events via monitor.
    5. Calls observe_outcomes() to write decision store.

    Returns True if all phases completed successfully (all done), False otherwise.
    """
    from .contract import load_contract as _load_contract
    from .kanban_tasks import (
        TERMINAL_STATUSES,
        get_todo_kanban_status,
        observe_outcomes,
        register_todo_phases,
    )

    # Resolve assignee from project contract (same path as tpo tick)
    assignee = "default"
    try:
        assignee = _load_contract(state_dir).assignee
    except Exception as e:
        log.warning("failed to load pipeline contract, using assignee='default': %s", e)
    log.info("registering kanban phases for %s tick %s (assignee=%s)", todo_id, tick_id, assignee)
    register_todo_phases(
        todo_id=todo_id,
        tick_id=tick_id,
        board_slug=project_slug,
        project_dir=project_dir,
        phases_path=phases_path,
        assignee=assignee,
        prompt_client=prompt_client,
    )

    # Intentionally unguarded — fail fast before polling begins, matching
    # register_todo_phases()'s unguarded call above.
    initial_status = get_todo_kanban_status(project_slug, tick_id)
    log.info(
        "initial phase status: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(initial_status.items())) or "(none)",
    )

    # Gate tasks will be auto-completed when their parent phase finishes,
    # not at registration time — this ensures parent output exists before
    # child phases can start.

    previous_status: dict[str, str] = {}
    all_terminal = False
    current_interval = poll_interval
    phase_by_key = {phase.phase_key: phase for phase in phases or []}
    pre_run_statuses = (None, "todo", "ready", "blocked")
    unstarted_statuses = (None, "todo", "ready")

    def _is_terminal_status(phase_key: str, status: str) -> bool:
        if status in TERMINAL_STATUSES:
            return True
        phase = phase_by_key.get(phase_key)
        return status == "blocked" and not getattr(phase, "gate", False)

    while not all_terminal:
        if cancel_event is not None:
            if cancel_event.wait(current_interval):
                return False
        else:
            time.sleep(current_interval)
        current_interval = min(current_interval * 1.5, max_poll_interval)

        try:
            status_map = get_todo_kanban_status(project_slug, tick_id)
        except Exception as e:
            log.warning("kanban status poll failed: %s", e)
            continue

        if not status_map:
            continue

        try:
            for phase_key, status in status_map.items():
                prev = previous_status.get(phase_key)

                if prev in pre_run_statuses and status == "running":
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

                elif prev == "running" and status == "blocked":
                    log.info("phase %s: running -> blocked", phase_key)
                    monitor.current_phase_key = None
                    monitor("phase_blocked", {"phase_key": phase_key, "todo_id": todo_id})

                elif prev in unstarted_statuses and status == "blocked":
                    log.info("phase %s: %s -> blocked", phase_key, prev or "none")
                    monitor.current_phase_key = None
                    monitor("phase_blocked", {"phase_key": phase_key, "todo_id": todo_id})

                elif prev in pre_run_statuses and status == "done":
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

                elif prev in pre_run_statuses and status == "failed":
                    log.info("phase %s: %s -> failed", phase_key, prev or "none")
                    monitor.current_phase_key = None
                    monitor("phase_failed", {"phase_key": phase_key, "todo_id": todo_id, "duration_ms": 0})
        except ConvergenceHaltError:
            log.warning(
                "convergence detector: %d+ consecutive phase_failure, halting",
                detector.threshold,
            )
            all_terminal = True

        if status_map != previous_status:
            current_interval = poll_interval
        previous_status = dict(status_map)

        if not all_terminal:
            all_terminal = all(
                _is_terminal_status(phase_key, status)
                for phase_key, status in status_map.items()
            )

    try:
        final_status = get_todo_kanban_status(project_slug, tick_id)
        observe_outcomes(state_dir=state_dir, tick_id=tick_id, status_map=final_status)
    except Exception as e:
        log.warning("observe_outcomes failed: %s", e)

    return all(s == "done" for s in previous_status.values())


def _classify_error_class(exc: Exception) -> str:
    """Bucket an exception into a coarse error class for convergence tracking / reports."""
    from .hermes_adapter import (
        AgentClientDependencyError,
        ClaudeCallError,
        ClaudeDependencyError,
        HermesCallError,
        HermesDependencyError,
    )

    if isinstance(
        exc,
        (HermesDependencyError, AgentClientDependencyError, ClaudeDependencyError),
    ):
        return "dependency_error"
    if isinstance(exc, HermesCallError):
        return "hermes_error"
    if isinstance(exc, ClaudeCallError):
        return "claude_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "phase_failure"


class _ConvergenceMonitor:
    """Wraps a monitor callback: forwards events, feeds the convergence detector,
    and tracks the currently in-flight phase for partial-report generation on
    overall-timeout. Raises ConvergenceHaltError if the detector trips.
    """

    def __init__(
        self,
        inner: Callable[[str, dict[str, Any] | None], None],
        detector: ConvergenceDetector,
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


def _with_offline_terminal_workflow(phases: list[Phase]) -> list[Phase]:
    """Replace the network-only final phase for the no-remote harness fixture."""
    return [
        replace(phase, prompt=_OFFLINE_TERMINAL_PROMPT)
        if phase.phase_key == _OFFLINE_TERMINAL_PHASE_KEY
        else phase
        for phase in phases
    ]


@contextmanager
def isolate_config(*, state_dir: Path):
    """Context manager that points tpo at an isolated config file.

    HOME is left untouched — the harness invokes real hermes/claude CLI
    subprocesses, which need the real $HOME to read auth credentials.
    """
    saved = {}
    for key in ("TPO_CONFIG_FILE",):
        if key in os.environ:
            saved[key] = os.environ[key]

    state_dir.mkdir(parents=True, exist_ok=True)
    config_path = state_dir / "tpo-config.yaml"
    config_path.write_text(f"state_dir: {state_dir}\n")
    os.environ["TPO_CONFIG_FILE"] = str(config_path)

    try:
        yield
    finally:
        for key in ("TPO_CONFIG_FILE",):
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


def _prune_retained_state(state_dir: Path) -> None:
    """Remove only disposable harness state from a retained fixture."""
    for filename in ("pipeline_branch.txt", "tpo-config.yaml"):
        path = state_dir / filename
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not prune retained harness state %s: %s", path, exc)

    for dirname in ("outcomes", "pipeline_checkpoints", "ready_for_review"):
        path = state_dir / dirname
        try:
            if not path.exists():
                continue
            if next(path.iterdir(), None) is not None:
                continue
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("could not prune retained harness state %s: %s", path, exc)




def _run_with_timeout(
    fn: Callable[[], bool], *, timeout: int, cancel_event: Any = None
) -> tuple[bool, bool, dict[str, Any]]:
    """Run `fn` on a daemon worker thread, joined with `timeout`.

    Returns (success, timed_out, result_box). result_box carries
    "convergence_error" or "exception" keys when fn raised those instead
    of returning normally.
    """
    import threading

    result_box: dict[str, Any] = {}

    def _run_and_capture() -> None:
        try:
            result_box["success"] = fn()
        except ConvergenceHaltError as e:
            result_box["convergence_error"] = e
        except Exception as e:
            result_box["exception"] = e

    worker = threading.Thread(target=_run_and_capture, daemon=True)
    worker.start()
    worker.join(timeout=timeout)

    if worker.is_alive():
        if cancel_event is None:
            raise PollCancellationError(
                "poll worker exceeded the overall timeout without cancellation"
            )
        cancel_event.set()
        worker.join(timeout=_POLL_CANCELLATION_TIMEOUT)
        if worker.is_alive():
            raise PollCancellationError(
                "poll worker did not stop after cooperative cancellation"
            )
        return False, True, result_box
    if "convergence_error" in result_box:
        log.warning(str(result_box["convergence_error"]))
        return False, False, result_box
    if "exception" in result_box:
        raise result_box["exception"]
    return result_box["success"], False, result_box


def run_harness(
    *,
    fixture_name: str,
    loop: bool,
    phase_only: str | None,
    keep_dir: bool,
    timeout: int,
    convergence_threshold: int,
    config: Any = None,
) -> HarnessResult:
    """Main orchestration: bootstrap fixture, run pipeline, generate report."""
    from .kanban_tasks import TERMINAL_STATUSES, get_todo_kanban_status
    from .logging_setup import new_tick_id
    from .phases import load_phases
    from .test_report import (
        diff_reports,
        generate_report,
        summarize_diff,
        summarize_report,
    )

    prompt_client = getattr(config, "prompt_client", "claude")

    preflight_check(prompt_client=prompt_client)

    # Allocate under ~/.hermes/tmp rather than the OS default temp root: on
    # macOS, tempfile.mkdtemp() resolves under /var/folders/..., which is a
    # symlink to /private/var/folders/... — a prefix the Hermes agent's
    # write-tool sensitive-path guard blocks, causing every worker in
    # --kanban hermes mode to crash-loop on writes inside the mock project.
    harness_tmp_root = Path("~/.hermes/tmp").expanduser()
    harness_tmp_root.mkdir(parents=True, exist_ok=True)
    workspace_dir = Path(tempfile.mkdtemp(prefix="harness-", dir=harness_tmp_root))
    project_dir = workspace_dir / "project"
    artifacts_dir = workspace_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    workspace_quiescent = True
    try:
        fixture = create_mock_project(project_dir, fixture_name)

        state_dir = project_dir / ".hermes"

        events_log = artifacts_dir / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=convergence_threshold)
        error_holder: dict[str, Any] = {}
        monitor = _ConvergenceMonitor(base_monitor, detector, error_holder)

        all_phases = load_phases()
        phases = all_phases
        if phase_only:
            phases = filter_phases(all_phases, phase_only)
        phases = _with_offline_terminal_workflow(phases)

        tick_id = new_tick_id()

        _kanban_preflight(tenant=fixture["project_slug"])

        # Always register an explicit harness profile. The full profile keeps
        # every production phase key while substituting the offline terminal
        # workflow for the no-remote fixture.
        import yaml as _yaml

        _phases_path_override = artifacts_dir / "harness-phases.yaml"
        _phases_path_override.write_text(
            _yaml.dump({"phases": [asdict(p) for p in phases]})
        )

        checkpoint_dir = state_dir / "pipeline_checkpoints"
        ready_dir = state_dir / "ready_for_review"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        ready_dir.mkdir(parents=True, exist_ok=True)

        timed_out = False
        timed_out_phase: str | None = None
        with isolate_config(state_dir=state_dir):
            # Emit initial event so the events log file exists for report generation
            base_monitor("run_started", {"tick_id": tick_id, "kanban_mode": "hermes"})

            import threading

            cancel_event = threading.Event()

            def _poll() -> bool:
                todo_id_str = f"TODO-{fixture['todo_id']}"
                return _poll_kanban_phases(
                    project_slug=fixture["project_slug"],
                    tick_id=tick_id,
                    state_dir=state_dir,
                    todo_id=todo_id_str,
                    project_dir=project_dir,
                    phases_path=_phases_path_override,
                    monitor=monitor,
                    detector=detector,
                    phases=phases,
                    prompt_client=prompt_client,
                    cancel_event=cancel_event,
                )

            try:
                success, timed_out, result_box = _run_with_timeout(
                    _poll,
                    timeout=timeout,
                    cancel_event=cancel_event,
                )
            except PollCancellationError as exc:
                workspace_quiescent = False
                raise HarnessCleanupError(
                    f"{exc}; workspace retained at {workspace_dir}"
                ) from exc

            if timed_out:
                try:
                    in_flight = get_todo_kanban_status(fixture["project_slug"], tick_id)
                    timed_out_phase = next(
                        (k for k, v in in_flight.items()
                         if v not in TERMINAL_STATUSES),
                        None,
                    )
                except Exception:
                    pass
                from .kanban_tasks import cancel_todo_kanban_tasks

                if not cancel_todo_kanban_tasks(
                    fixture["project_slug"],
                    tick_id,
                ):
                    workspace_quiescent = False
                    raise HarnessCleanupError(
                        "Hermes task or worker termination could not be "
                        f"confirmed; workspace retained at {workspace_dir}"
                    )
                timed_out_phase = timed_out_phase or monitor.current_phase_key
                if timed_out_phase:
                    base_monitor(
                        "phase_timed_out",
                        {"phase_key": timed_out_phase},
                    )
            elif "convergence_error" in result_box:
                # Convergence-halt fired during polling. The poll loop already
                # exited with all_terminal=True, so phases are already in terminal
                # state on the kanban board — no additional cleanup needed beyond
                # surfacing the convergence error in the result.
                log.warning("convergence-halt: %s", result_box["convergence_error"])

        output_dir = artifacts_dir / "reports"
        generate_report(events_log, output_dir)
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

        status_map = get_todo_kanban_status(fixture["project_slug"], tick_id)
        print(
            f"[kanban] tenant={fixture['project_slug']} tick_id={tick_id} "
            f"phases={status_map} "
            f"report={report_json} keep={'yes' if keep_dir else 'no (temp dir will be removed)'}"
        )

        if keep_dir and not timed_out:
            _prune_retained_state(state_dir)

        exit_code = 0 if (success and not timed_out) else 1

        return HarnessResult(
            exit_code=exit_code,
            report_path=report_json,
            temp_dir=workspace_dir if keep_dir else None,
            summary=summary,
        )

    except Exception:
        raise

    finally:
        if not keep_dir and workspace_quiescent:
            shutil.rmtree(workspace_dir, ignore_errors=True)
