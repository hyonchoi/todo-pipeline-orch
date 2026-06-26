# Multi-project Scan Loop & Kill Refactor

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `pipeline-watch tick` to scan all active projects and run selection per-project, and update `pipeline-watch kill` to scan across projects.

**Architecture:** Remove the `project` argument from the `tick` subcommand. The new `_cmd_tick` acquires the global `TickLock`, runs `_discover_projects()`, then iterates: for each project, runs the per-project tick flow (prior-tick check, selection, circuit breaker, kanban registration) using per-project state directories. Error isolation is achieved with per-project `try/except` — one project's failure doesn't block others. The kill command is updated to scan all projects when no project is specified.

**Tech Stack:** Python 3.12+, `uv`, no new dependencies. Requires Plans A (state migration) and B (project config) to be completed first.

## Global Constraints

- One TODO in flight per project (existing constraint)
- Single global lock for the scan — no two ticks overlap
- Hermes-only for LLM queries (TODO-6)
- Filesystem-based config — no database or network calls before selection
- Python 3.12+, `uv`-managed
- Breaking change: `tick <project>` becomes `tick` (no argument)

---

### Task 1: Rewrite _cmd_tick to scan loop

**Files:**
- Modify: `hermes_pipeline/cli.py:336-350` (tick parser definition — remove project arg)
- Modify: `hermes_pipeline/cli.py:397-600` (the `_cmd_tick` function — full rewrite)
- Test: `tests/test_scan_loop.py`

**Interfaces:**
- Consumes: `_discover_projects(config)` from Plan B, `_get_project_state_dir(project_dir)` from Plan A, `_migrate_global_state(project_dir, config)` from Plan A, `_is_enabled(project_dir)` from Plan B, `_resolve_slack_channel(project_dir, env_channel)` from Plan B
- Produces: New `_cmd_tick` with scan loop

**Pre-requisite:** Read the existing `_cmd_tick` implementation to understand the full flow before rewriting.

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path
from unittest.mock import patch

from hermes_pipeline.config import Config
from hermes_pipeline.cli import _cmd_tick, build_parser


class FakeArgs:
    """Minimal argparse.Namespace for testing."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_tick_scans_multiple_projects(tmp_path: Path):
    """tick should iterate over discovered projects and run selection for each."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Project A with TODOS.md
    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    # Project B with TODOS.md
    pb = projects_dir / "project-b"
    pb.mkdir()
    (pb / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(ctx):
        selection_calls.append(ctx.project_slug)
        from hermes_pipeline.decision import Decision
        return Decision(picked=None, rationale="no selection")

    args = FakeArgs()

    with patch("hermes_pipeline.cli.run_selection", mock_selection):
        exit_code = _cmd_tick(args, config)

    assert exit_code == 0
    assert "project-a" in selection_calls
    assert "project-b" in selection_calls


def test_tick_skips_disabled_projects(tmp_path: Path):
    """tick should skip projects with enabled=false in project.toml."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Active project
    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    # Disabled project
    pb = projects_dir / "project-b"
    pb.mkdir()
    (pb / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")
    pb_hermes = pb / ".hermes"
    pb_hermes.mkdir()
    (pb_hermes / "project.toml").write_text("[active]\nenabled = false\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(ctx):
        selection_calls.append(ctx.project_slug)
        from hermes_pipeline.decision import Decision
        return Decision(picked=None, rationale="no selection")

    args = FakeArgs()

    with patch("hermes_pipeline.cli.run_selection", mock_selection):
        exit_code = _cmd_tick(args, config)

    assert exit_code == 0
    assert "project-a" in selection_calls
    assert "project-b" not in selection_calls


def test_tick_error_isolation(tmp_path: Path):
    """A project error should be logged and not block other projects."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Project that will cause an error
    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("{{{invalid toml{{{")  # Malformed

    # Healthy project
    pb = projects_dir / "project-b"
    pb.mkdir()
    (pb / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(ctx):
        selection_calls.append(ctx.project_slug)
        from hermes_pipeline.decision import Decision
        return Decision(picked=None, rationale="no selection")

    args = FakeArgs()

    with patch("hermes_pipeline.cli.run_selection", mock_selection):
        exit_code = _cmd_tick(args, config)

    # The scan should succeed (project-b ran), even though project-a may have errored
    assert exit_code == 0
    assert "project-b" in selection_calls


def test_tick_uses_per_project_state_dir(tmp_path: Path):
    """tick should use <project>/.hermes/ for per-project state files."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    state_dirs_seen = []

    def mock_context(*, state_dir, **kwargs):
        state_dirs_seen.append(state_dir)
        from hermes_pipeline.decision.context import Context
        return Context(
            tick_id="test123",
            state_dir=state_dir,
            project_slug="project-a",
            max_phase_timeout_min=30,
            todos_path=pa / "TODOS.md",
        )

    def mock_selection(ctx):
        from hermes_pipeline.decision import Decision
        return Decision(picked=None, rationale="no selection")

    args = FakeArgs()

    with (
        patch("hermes_pipeline.cli.build_context", mock_context),
        patch("hermes_pipeline.cli.run_selection", mock_selection),
    ):
        _cmd_tick(args, config)

    # The state_dir passed to build_context should be per-project
    assert any("project-a" in str(sd) for sd in state_dirs_seen)


def test_tick_performs_state_migration(tmp_path: Path):
    """tick should migrate global state to per-project dirs before scanning."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Set up global state
    (state_dir / "current_tick_id.txt").write_text("old-tick-123\n")

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    def mock_selection(ctx):
        from hermes_pipeline.decision import Decision
        return Decision(picked=None, rationale="no selection")

    args = FakeArgs()

    with patch("hermes_pipeline.cli.run_selection", mock_selection):
        _cmd_tick(args, config)

    # Global state should be migrated to per-project
    assert not (state_dir / "current_tick_id.txt").exists()
    assert (pa / ".hermes" / "current_tick_id.txt").exists()
    assert (pa / ".hermes" / "current_tick_id.txt").read_text().strip() == "old-tick-123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scan_loop.py::test_tick_scans_multiple_projects -v`
Expected: FAIL — the existing `_cmd_tick` takes a `project` argument and doesn't scan.

- [ ] **Step 3: Rewrite _cmd_tick and tick parser**

Update the tick parser in `build_parser()` — change:

```python
    tick_parser = subparsers.add_parser(
        "tick",
        help="Run one pipeline tick: select a TODO and register kanban phases",
    )
    tick_parser.add_argument("project", help="Project name/slug")
    tick_parser.set_defaults(func=_cmd_tick)
```

to:

```python
    tick_parser = subparsers.add_parser(
        "tick",
        help="Run one pipeline tick: scan all projects and select TODOs",
    )
    tick_parser.set_defaults(func=_cmd_tick)
```

Rewrite `_cmd_tick`:

```python
def _cmd_tick(args, config: Config) -> int:
    """Handle 'tick' subcommand — kanban-as-scheduler pipeline scan tick.

    Flow:
    1. Acquire global TickLock
    2. Discover active projects
    3. For each project: migrate state, check prior tick, run selection
    4. Release lock

    Each project's errors are isolated — one project's failure doesn't
    block the others.
    """
    from .circuit import CircuitBreaker
    from .decision.context import build_context
    from .kanban_tasks import all_phases_complete, observe_outcomes, register_todo_phases
    from .outcomes import OUTCOME_PICKED_NONE, CURRENT_TICK_ID_FILE
    from .project_config import _discover_projects, _resolve_slack_channel
    from .state_migration import _get_project_state_dir, _migrate_global_state
    from .tick import TickLock, TickLockHeld

    state_dir = config.state_dir

    # --- Step 1: Load global config overlay ---
    try:
        toml_cfg, cb_cfg = _load_toml_overlay(state_dir, config)
    except Exception as e:
        log.warning("failed to load config overlay: %s — using defaults", e)
        from .config import CircuitBreakerConfig
        toml_cfg, cb_cfg = None, CircuitBreakerConfig()

    # --- Step 2: Acquire global lock ---
    tick_lock = TickLock(state_dir, max_age_min=cb_cfg.max_tick_duration_min)
    tick_id = _generate_tick_id()

    try:
        vlog.info("acquiring tick lock: lock_dir=%s tick_id=%s", tick_lock.lock_dir, tick_id)
        with tick_lock.acquire(tick_id):
            log.debug("tick lock acquired: lock_dir=%s holder_pid=%d",
                      tick_lock.lock_dir, os.getpid())

            # --- Step 3: Discover projects ---
            projects = _discover_projects(config)
            if not projects:
                log.info("no active projects found in %s", config.projects_dir)
                return 0

            log.info("discovered %d active projects", len(projects))

            # --- Step 4: Per-project tick ---
            for project_dir in projects:
                project_slug = project_dir.name
                project_state = _get_project_state_dir(project_dir)

                try:
                    _tick_project(
                        project_dir=project_dir,
                        project_slug=project_slug,
                        project_state=project_state,
                        config=config,
                        cb_cfg=cb_cfg,
                    )
                except Exception as e:
                    log.error("project %s: %s", project_slug, e)
                    # Continue to next project

    except TickLockHeld:
        log.error("tick lock held, exiting")
        return 1

    vlog.info("tick lock released: tick_id=%s", tick_id)
    return 0


def _tick_project(
    *,
    project_dir: Path,
    project_slug: str,
    project_state: Path,
    config: Config,
    cb_cfg,
) -> None:
    """Run the tick flow for a single project.

    1. Migrate global state (if needed)
    2. Check prior tick
    3. Run selection
    4. Register kanban phases or observe circuit breaker

    Args:
        project_dir: Project root directory.
        project_slug: Project name (derived from directory name).
        project_state: Per-project state directory (<project>/.hermes/).
        config: Global config.
        cb_cfg: Circuit breaker configuration.

    Raises:
        Exception: On any error (caller logs and continues to next project).
    """
    from .decision.context import build_context
    from .decision import run_selection
    from .kanban_tasks import all_phases_complete, observe_outcomes, register_todo_phases
    from .outcomes import OUTCOME_PICKED_NONE

    vlog = logging.getLogger("pipeline.verbose")

    # Step 1: Migrate global state (one-time, idempotent)
    try:
        _migrate_global_state(project_dir, config)
    except Exception as e:
        log.warning("state migration for %s: %s", project_slug, e)

    # Ensure per-project state directory exists
    project_state.mkdir(parents=True, exist_ok=True)

    # Resolve per-project Slack channel
    slack_channel = _resolve_slack_channel(project_dir, env_channel=config.slack_channel)

    # Step 2: Check prior tick
    prior_tick_id = _read_prior_tick_id(project_state)

    cb = _make_circuit_breaker(project_state, cb_cfg, slack_channel)

    if prior_tick_id is not None:
        if not all_phases_complete(project_slug, prior_tick_id, state_dir=project_state):
            log.info("project %s: prior tick %s still in-flight, skipping",
                     project_slug, prior_tick_id)
            return

        # Prior tick complete — observe outcomes before new selection
        try:
            from .kanban_tasks import get_todo_kanban_status
            status_map = get_todo_kanban_status(project_slug, prior_tick_id)
            observe_outcomes(
                state_dir=project_state,
                tick_id=prior_tick_id,
                status_map=status_map,
            )
            cb.observe_from_outcomes(
                state_dir=project_state,
                prior_tick_id=prior_tick_id,
            )
        except Exception as e:
            log.warning("project %s: observe_outcomes for prior tick %s failed: %s",
                        project_slug, prior_tick_id, e)

    # Step 3: Build context & run selection
    todos_path = project_dir / "TODOS.md"
    if not todos_path.exists():
        raise FileNotFoundError(f"TODOS.md not found in {project_dir}")

    ctx = build_context(
        tick_id=_generate_tick_id(),
        state_dir=project_state,
        todos_path=todos_path,
        project_slug=project_slug,
        max_phase_timeout_min=cb_cfg.max_phase_timeout_min,
    )

    decision = run_selection(ctx)
    picked = decision.picked

    vlog.info("project %s: selection result: picked=%s rationale=%s",
              project_slug, picked, decision.rationale[:200])

    if picked is None:
        log.info("project %s: selection picked None, observing circuit breaker",
                 project_slug)
        cb.observe(picked=None, counts_as_no_progress=True)
        _persist_tick_id(project_state, ctx.tick_id)
        try:
            from .outcomes import OUTCOME_PICKED_NONE
            from .kanban_tasks import observe_outcomes
            observe_outcomes(
                state_dir=project_state,
                tick_id=ctx.tick_id,
                status_map={},
            )
            # Write picked_none sentinel
            outcomes_dir = project_state / "outcomes"
            outcomes_dir.mkdir(exist_ok=True)
            sentinel = outcomes_dir / f"{ctx.tick_id}-phases.json"
            from .state import _atomic_write_text
            _atomic_write_text(
                sentinel,
                json.dumps({"outcome": OUTCOME_PICKED_NONE}) + "\n",
            )
        except Exception as se:
            log.warning("project %s: failed to write picked_none sentinel: %s",
                        project_slug, se)
        return

    # Step 4: Register kanban phases
    log.info("project %s: selected %s, registering kanban phases", project_slug, picked)
    try:
        task_ids = register_todo_phases(
            todo_id=picked,
            tick_id=ctx.tick_id,
            board_slug=project_slug,
            project_dir=project_dir,
        )
        log.info("project %s: registered %d kanban tasks for %s: %s",
                 project_slug, len(task_ids), picked, task_ids)
    except RuntimeError as e:
        log.error("project %s: kanban registration failed: %s", project_slug, e)
        raise

    # Observe circuit breaker
    cb.observe(picked=picked, counts_as_no_progress=False)

    # Persist tick_id inside lock
    _persist_tick_id(project_state, ctx.tick_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_scan_loop.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_scan_loop.py
git commit -m "feat: replace tick <project> with scan loop

Remove project argument from tick subcommand. New tick flow:
1. Acquire global TickLock
2. Discover active projects via _discover_projects()
3. Per-project: migrate state, check prior tick, run selection
4. Error isolation — one project failure doesn't block others"
```

### Task 2: Update _cmd_kill for multi-project

**Files:**
- Modify: `hermes_pipeline/cli.py:285-302` (kill parser — make project optional)
- Modify: `hermes_pipeline/cli.py:391-395` (the `_cmd_kill` function)
- Test: `tests/test_cli_kill.py`

**Interfaces:**
- Consumes: `_discover_projects(config)` from Plan B
- Produces: `kill` with optional project — `kill --all` scans all projects

Per the resolved decisions in the design doc: "kill: pipeline-watch kill takes optional project arg. Omitted -> scan all projects for in-flight phases. Specified -> kill only in that project."

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
from unittest.mock import patch

from hermes_pipeline.config import Config
from hermes_pipeline.cli import build_parser


def test_kill_without_project_scans_all(tmp_path: Path):
    """kill without a project argument should scan all projects for in-flight phases."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Project with TODOS.md
    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    # Project with TODOS.md
    pb = projects_dir / "project-b"
    pb.mkdir()
    (pb / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    parser = build_parser()
    args = parser.parse_args(["kill", "--all"])

    # _cmd_kill should be called with no project arg
    with patch("hermes_pipeline.cli.cmd_kill") as mock_kill:
        mock_kill.return_value = 0
        from hermes_pipeline.cli import _cmd_kill
        result = _cmd_kill(args, config)

        assert result == 0
        # cmd_kill should be called with all_=True
        mock_kill.assert_called_once()
        call_kwargs = mock_kill.call_args.kwargs
        assert call_kwargs["all_"] is True


def test_kill_with_parser_no_project_required(tmp_path: Path):
    """kill should parse without requiring a project argument."""
    parser = build_parser()
    # This should not raise an error about missing required argument
    args = parser.parse_args(["kill", "--all"])
    assert args.all_ is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_kill.py::test_kill_with_parser_no_project_required -v`
Expected: FAIL — the current kill parser has a required mutually exclusive group.

- [ ] **Step 3: Update kill parser, _cmd_kill, and cmd_kill**

`cmd_kill` lives in `cli.py` (line ~146). It operates on `state_dir / "phase_started"` — with per-project state, that's `<project>/.hermes/phase_started/`.

Update the kill parser in `build_parser()`:

Change:
```python
    kill_parser = subparsers.add_parser(
        "kill",
        help="Kill in-flight phase(s)",
    )
    kill_group = kill_parser.add_mutually_exclusive_group(required=True)
    kill_group.add_argument("--all", dest="all_", action="store_true", help="Kill all in-flight phases")
    kill_group.add_argument("--todo", help="Kill a specific TODO (e.g., TODO-1)")
    kill_parser.set_defaults(func=_cmd_kill)
```

to:
```python
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

Update `_cmd_kill`:

Change:
```python
def _cmd_kill(args, config: Config) -> int:
    """Handle 'kill' subcommand."""
    return cmd_kill(
        state_dir=config.state_dir,
        all_=args.all_,
        todo=args.todo,
    )
```

to:
```python
def _cmd_kill(args, config: Config) -> int:
    """Handle 'kill' subcommand.

    If project is specified, kill in that project.
    If project is omitted and --all, scan all projects for in-flight phases.
    If project is omitted and --todo, kill that TODO across all projects.
    """
    return cmd_kill(
        state_dir=config.state_dir,
        all_=args.all_,
        todo=args.todo,
        project=args.project,
        config=config,
    )
```

Update `cmd_kill` to accept an optional `project` and `config` parameter. When `project` is `None`, scan all projects:

Change the `cmd_kill` signature and add the multi-project logic at the top:

```python
def cmd_kill(
    *,
    state_dir: Path,
    all_: bool = False,
    todo: str | None = None,
    project: str | None = None,
    config: Config | None = None,
) -> int:
    """Kill in-flight phase(s) and write killed_by_operator outcome sidecars.

    When project is specified, kills in that project's state directory.
    When project is omitted, scans all projects for in-flight phases.

    - Reads phase_started/* markers
    - SIGTERMs the recorded child_pid (and/or sends hermes run kill <job_id>)
    - Writes killed_by_operator outcome sidecars
    - Deletes markers
    - Releases tick.lock ONLY if its holder is one of the killed ticks
    """
    from .project_config import _discover_projects, _get_project_state_dir

    # Multi-project kill: scan all projects
    if project is None and config is not None:
        return _kill_all_projects(config, all_=all_, todo=todo)

    ps_dir = state_dir / "phase_started"
```

Add the `_kill_all_projects` helper after `cmd_kill`:

```python
def _kill_all_projects(
    config: Config,
    *,
    all_: bool = False,
    todo: str | None = None,
) -> int:
    """Scan all projects and kill in-flight phases.

    Args:
        config: Global config.
        all_: Kill all in-flight phases across all projects.
        todo: Kill a specific TODO across all projects.

    Returns:
        0 if successful, 1 if some kills unconfirmed, 2 on error.
    """
    from .project_config import _discover_projects

    projects = _discover_projects(config)
    if not projects:
        print("no active projects found")
        return 0

    total_killed = 0
    total_unconfirmed = 0

    for project_dir in projects:
        project_slug = project_dir.name
        project_state = project_dir / ".hermes"
        ps_dir = project_state / "phase_started"

        if not ps_dir.exists():
            continue

        # Count targets in this project
        if all_:
            targets = [f for f in ps_dir.iterdir() if f.is_file() and f.suffix == ".json"]
        elif todo:
            p = ps_dir / f"{todo}.json"
            targets = [p] if p.exists() else []
        else:
            continue

        if not targets:
            continue

        # Kill phases in this project using existing cmd_kill logic
        result = cmd_kill(
            state_dir=project_state,
            all_=all_,
            todo=todo,
        )
        if result == 0:
            total_killed += len(targets)
        else:
            total_unconfirmed += 1

    if total_killed == 0 and total_unconfirmed == 0:
        print("no in-flight phases found")
        return 0
    elif total_unconfirmed > 0:
        return 1
    else:
        return 0
```