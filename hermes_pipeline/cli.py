"""Hermes pipeline orchestrator CLI.

Subcommands: merge, status, kill.
Scheduling is owned by the Hermes command repo.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import signal
import sys
from hermes_pipeline import __version__
import time
from pathlib import Path
from typing import Optional

import subprocess as _cli_sp

from .circuit import CircuitBreaker
from .config import Config, CircuitBreakerConfig
from .decision import run_selection, store as _cli_dec_store
from .decision.context import build_context
from .kanban import NullKanbanAdapter, HermesKanbanAdapter
from .kanban_tasks import all_phases_complete, observe_outcomes, register_todo_phases
from .logging_setup import configure as configure_logging
from .outcomes import CURRENT_TICK_ID_FILE, OUTCOME_PICKED_NONE
from .logging_setup import new_tick_id as _new_tick_id
from .merge import run_phase9, make_default_bump_fn
from .phases import load_phases
from .status import collect_pending, format_table
from .tick import TickLock, TickLockHeld

log = logging.getLogger(__name__)
vlog = logging.getLogger("pipeline.verbose")


def _hermes_run_kill(job_id: str) -> int:
    """Send hermes run kill for a job."""
    try:
        r = _cli_sp.run(["hermes", "run", "kill", job_id], timeout=10, check=False)
        return r.returncode
    except (_cli_sp.TimeoutExpired, FileNotFoundError):
        return 1

def _signal_pid(pid: int) -> bool:
    """SIGTERM a phase subprocess. Returns True if signal delivered (or already gone)."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True  # already exited
    except (PermissionError, OSError):
        return False

def _process_alive(pid: int) -> bool:
    """Return True iff pid names a live process this user can signal."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user; treat as alive (we can't
        # confirm exit). Caller will surface this as kill-unconfirmed.
        return True
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        return True

def _kill_session_group(pid: int, sig: int) -> bool:
    """Signal the entire session group rooted at pid (phases.py uses start_new_session)."""
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return True
    except OSError:
        pgid = pid
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False

def _confirm_pid_exited(pid: int, *, term_grace_s: float = 5.0, kill_grace_s: float = 2.0) -> bool:
    """SIGTERM → poll → SIGKILL → poll. Return True iff the pid is gone afterwards.

    Targets the session group so children spawned by Claude don't survive the
    parent's death. The marker stays on disk until this returns True; if it
    returns False the caller MUST leave the marker in place so future ticks
    still see the TODO as in-flight.
    """
    if not _process_alive(pid):
        return True
    _signal_pid(pid)
    _kill_session_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + term_grace_s
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.1)
    # Escalate.
    _kill_session_group(pid, signal.SIGKILL)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + kill_grace_s
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.1)
    return not _process_alive(pid)

def _release_tick_lock_if_owned_by(state_dir: Path, tick_ids: set[str]) -> None:
    """Release tick.lock only if its holder's tick_id is in tick_ids.

    Refuses to release a lock held by a different tick — a mistyped kill must
    not be able to break an unrelated in-flight tick's critical section.
    """
    lock_dir = state_dir / "tick.lock"
    holder = lock_dir / "holder.json"
    if not lock_dir.exists() or not holder.exists():
        return
    try:
        data = json.loads(holder.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if data.get("tick_id") not in tick_ids:
        return
    try:
        holder.unlink()
    except FileNotFoundError:
        pass
    try:
        lock_dir.rmdir()
    except OSError:
        pass

def cmd_kill(*, state_dir: Path, all_: bool = False, todo: str | None = None) -> int:
    """Kill in-flight phase(s) and write killed_by_operator outcome sidecars.

    - Reads phase_started/* markers
    - SIGTERMs the recorded child_pid (and/or sends hermes run kill <job_id>)
    - Writes killed_by_operator outcome sidecars
    - Deletes markers
    - Releases tick.lock ONLY if its holder is one of the killed ticks
    """
    ps_dir = state_dir / "phase_started"
    if not ps_dir.exists():
        print("no in-flight phases")
        return 0

    targets = []
    if todo:
        p = ps_dir / f"{todo}.json"
        if p.exists():
            targets.append(p)
        else:
            print(f"no in-flight phase for {todo}")
            return 2
    elif all_:
        targets = [f for f in ps_dir.iterdir() if f.is_file() and f.suffix == ".json"]
    else:
        print("error: specify --all or --todo TODO-N")
        return 2

    killed_tick_ids: set[str] = set()
    unconfirmed: list[str] = []
    for p in targets:
        try:
            data = json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"warning: unreadable marker {p.name}: {e}")
            continue
        job_id = data.get("job_id")
        child_pid = data.get("child_pid")
        tick_id = data.get("tick_id", "")

        # Verify the phase actually died before we declare it killed. A
        # SIGTERM-ignoring or already-detached Claude process must NOT be
        # recorded as killed_by_operator while it keeps mutating the repo;
        # if confirmation fails, leave the marker in place so future ticks
        # still see the TODO as in-flight.
        exit_confirmed = True
        if child_pid:
            exit_confirmed = _confirm_pid_exited(int(child_pid))
        elif job_id:
            # hermes run kill is the only handle we have; trust its return code.
            exit_confirmed = (_hermes_run_kill(job_id) == 0)
        else:
            print(f"warning: no child_pid or job_id on marker {p.name}; cannot confirm kill")
            exit_confirmed = False

        # Best-effort secondary kill via hermes runner even when we had a pid.
        if child_pid and job_id:
            _hermes_run_kill(job_id)

        if not exit_confirmed:
            print(
                f"error: failed to confirm exit for {p.name} "
                f"(pid={child_pid} job={job_id}); leaving marker in place"
            )
            unconfirmed.append(p.name)
            continue

        if tick_id:
            try:
                _cli_dec_store.append_outcome(
                    state_dir, tick_id,
                    outcome="killed_by_operator",
                    detail={"todo_id": p.stem},
                )
            except FileExistsError:
                # Outcome already terminal — fine, marker cleanup still proceeds.
                pass
            killed_tick_ids.add(tick_id)

        p.unlink()

    _release_tick_lock_if_owned_by(state_dir, killed_tick_ids)
    return 1 if unconfirmed else 0

def _parse_todo_id(value: str) -> int:
    """Parse todo_id argument with helpful error message."""
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"todo_id must be a number (you provided '{value}'). "
            f"Example: pipeline-watch merge myproject 123"
        )


def _strip_global_flags(argv: list[str]) -> tuple[bool, bool, list[str]]:
    """Strip --verbose/--debug from argv, returning (verbose, debug, remaining).

    This avoids the argparse subparser namespace overwrite: if --verbose lives
    on both the root parser and a subparser, the subparser's default (False)
    overwrites the root's True. By stripping the flags upfront we configure
    logging before argparse ever runs.
    """
    verbose = False
    debug = False
    remaining = []
    for arg in argv:
        if arg in ("--verbose",):
            verbose = True
        elif arg in ("--debug",):
            debug = True
        else:
            remaining.append(arg)
    return verbose, debug, remaining

def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="pipeline-watch",
        description="Hermes pipeline orchestrator: merge, status, and kill commands.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

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

    # status: List pending records
    status_parser = subparsers.add_parser(
        "status",
        help="Display pending ready-for-review records",
    )
    status_parser.set_defaults(func=_cmd_status)

    # kill: Kill in-flight phases
    kill_parser = subparsers.add_parser(
        "kill",
        help="Kill in-flight phase(s)",
    )
    kill_group = kill_parser.add_mutually_exclusive_group(required=True)
    kill_group.add_argument("--all", dest="all_", action="store_true", help="Kill all in-flight phases")
    kill_group.add_argument("--todo", help="Kill a specific TODO (e.g., TODO-1)")
    kill_parser.set_defaults(func=_cmd_kill)

    # tick: Pipeline tick — select TODO, register kanban phases
    tick_parser = subparsers.add_parser(
        "tick",
        help="Run one pipeline tick: scan all projects and select TODOs",
    )
    tick_parser.set_defaults(func=_cmd_tick)

    # recover-counter: Scan TODOS.md and initialize counter file
    rc_parser = subparsers.add_parser(
        "recover-counter",
        help="Scan TODOS.md and initialize .hermes/todo_id_counter",
    )
    rc_parser.add_argument("project", help="Project name/slug")
    rc_parser.set_defaults(func=_cmd_recover_counter)

    return parser


def _cmd_merge(args, config: Config) -> int:
    """Handle 'merge' subcommand."""
    try:
        project = args.project
        todo_id = args.todo_id
        abandon = args.abandon

        # Find the project directory
        project_dir = config.projects_dir / project
        if not project_dir.exists():
            log.error(f"project not found: {project}")
            return 2

        # Build kanban adapter
        if config.kanban_adapter == "hermes":
            kanban = HermesKanbanAdapter()
        else:
            kanban = NullKanbanAdapter()

        # Prepare State for reading ready-for-review record
        checkpoint_dir = config.state_dir / "pipeline_checkpoints"
        ready_dir = config.state_dir / "ready_for_review"

        from .state import State
        state = State(
            project=project,
            lock_dir=config.lock_dir,
            checkpoint_dir=checkpoint_dir,
            ready_dir=ready_dir,
        )

        # Run Phase 9
        if abandon:
            # Abandon: reject without confirmation
            def confirm_fn(tid: int) -> bool:
                return False
        else:
            # Normal: use default confirmation
            from .merge import default_confirm_fn
            confirm_fn = default_confirm_fn

        bump_fn = make_default_bump_fn(project_dir)

        run_phase9(
            state=state,
            project_dir=project_dir,
            todo_id=todo_id,
            kanban=kanban,
            confirm_fn=confirm_fn,
            bump_fn=bump_fn,
        )
        return 0
    except Exception as e:
        log.error(f"merge command failed: {e}", exc_info=True)
        return 2


def _cmd_status(args, config: Config) -> int:
    """Handle 'status' subcommand."""
    try:
        rows = collect_pending(config.projects_dir, config.lock_dir)
        table = format_table(rows)
        print(table, end="")
        return 0
    except Exception as e:
        log.error(f"status command failed: {e}", exc_info=True)
        return 2

def _cmd_kill(args, config: Config) -> int:
    """Handle 'kill' subcommand."""
    return cmd_kill(
        state_dir=config.state_dir,
        all_=args.all_,
        todo=args.todo,
    )


def _read_prior_tick_id(state_dir: Path) -> str | None:
    """Read the prior tick_id from current_tick_id.txt.

    Returns None if the file doesn't exist (cold start).
    """
    path = state_dir / CURRENT_TICK_ID_FILE
    if not path.exists():
        return None
    try:
        return path.read_text().strip()
    except OSError:
        return None

def _generate_tick_id() -> str:
    """Generate a new tick ID."""
    try:
        return _new_tick_id()
    except Exception:
        import datetime as _dt
        import secrets as _secrets
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        rand = format(_secrets.randbelow(900000) + 100000, "06d")
        return f"{ts}{rand}"

def _load_toml_overlay(state_dir: Path, config: Config):
    """Load circuit breaker + selection config from .hermes/config.toml.

    Returns a tuple of (FullConfig or None, CircuitBreakerConfig).
    FullConfig is the complete overlay (selection, circuit_breaker).
    CircuitBreakerConfig is extracted for early use (before lock acquisition).
    On TOML error the overlay falls back to defaults with a warning.
    """
    from .config import FullConfig, load_toml_overlay as _load_toml

    toml_path = state_dir / "config.toml"
    try:
        full_cfg: FullConfig = _load_toml(config, toml_path)
        return (full_cfg, full_cfg.circuit_breaker)
    except FileNotFoundError:
        # No config.toml — use defaults silently
        return (None, CircuitBreakerConfig())
    except Exception as e:
        log.warning("failed to load %s: %s — using defaults", toml_path, e)
        return (None, CircuitBreakerConfig())

def _make_circuit_breaker(state_dir: Path, cb_cfg, slack_channel: str):
    """Create a CircuitBreaker instance from config."""
    return CircuitBreaker(
        state_path=state_dir / "circuit.json",
        no_progress_threshold=cb_cfg.no_progress_threshold,
        backoff_interval_min=cb_cfg.backoff_interval_min,
        alert_dedup_hours=cb_cfg.alert_dedup_hours,
        slack_channel=slack_channel,
    )

def _validate_project_slug(slug: str) -> bool:
    """Reject project slugs that could inject CLI flags or traverse paths.

    Rules:
    - Must start with a letter or digit (no leading dash, dot, or underscore)
    - Only alphanumeric, single dash, single underscore, single dot (no consecutive
      dots that could form '..' path traversal)
    - No consecutive dots (blocks '..' path traversal)
    - No leading dash (blocks CLI flag injection)
    - Not a bare '.' or '..'
    """
    if not slug or slug in (".", ".."):
        return False
    if slug.startswith(("-", ".")):
        return False
    if ".." in slug:
        return False
    import re as _re
    return bool(_re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]+$', slug))

def _persist_tick_id(state_dir: Path, tick_id: str) -> None:
    """Persist tick_id atomically for the next tick's prior check.

    Uses tmp+rename so a crash mid-write doesn't leave a partial file.
    Also writes a sentinel file so all_phases_complete can distinguish
    "persisted but never registered" from "persisted and registered".
    """
    from .state import _atomic_write_text

    try:
        _atomic_write_text(state_dir / CURRENT_TICK_ID_FILE, tick_id)
    except OSError as e:
        log.warning("failed to persist current_tick_id: %s", e)
        return

    # Write sentinel so the next tick's all_phases_complete knows this
    # tick was legitimate even if registration crashed before creating
    # any kanban tasks.  picked=None writes its own outcome later.
    try:
        outcomes_dir = state_dir / "outcomes"
        outcomes_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            outcomes_dir / f"{tick_id}-phases.json",
            '{"outcome": "tick_started"}\n',
        )
    except OSError as e:
        log.warning("failed to write tick_started sentinel: %s", e)

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
    from .project_config import _resolve_slack_channel
    from .state_migration import _migrate_global_state

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
    tick_id = _generate_tick_id()
    todos_path = project_dir / "TODOS.md"
    if not todos_path.exists():
        raise FileNotFoundError(f"TODOS.md not found in {project_dir}")

    ctx = build_context(
        tick_id=tick_id,
        state_dir=project_state,
        todos_path=todos_path,
        project_slug=project_slug,
        max_phase_timeout_min=cb_cfg.max_phase_timeout_min,
    )

    # Build full config for selection
    from .config import FullConfig, SelectionConfig
    from .config import load_toml_overlay as _load_toml_inline

    try:
        toml_cfg = _load_toml_inline(config, project_state / "config.toml")
    except (FileNotFoundError, ValueError):
        toml_cfg = None

    if toml_cfg is not None:
        full_cfg = toml_cfg
    else:
        full_cfg = FullConfig(
            base=config,
            selection=SelectionConfig(),
            circuit_breaker=cb_cfg,
        )

    decision = run_selection(
        tick_id=tick_id,
        ctx=ctx,
        cfg=full_cfg,
    )
    picked = decision.picked

    vlog.info("project %s: selection result: picked=%s rationale=%s",
              project_slug, picked, decision.rationale[:200])

    if picked is None:
        log.info("project %s: selection picked None, observing circuit breaker",
                 project_slug)
        cb.observe(picked=None, counts_as_no_progress=True)
        _persist_tick_id(project_state, tick_id)
        try:
            observe_outcomes(
                state_dir=project_state,
                tick_id=tick_id,
                status_map={},
            )
            # Write picked_none sentinel
            outcomes_dir = project_state / "outcomes"
            outcomes_dir.mkdir(exist_ok=True)
            sentinel = outcomes_dir / f"{tick_id}-phases.json"
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
            tick_id=tick_id,
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
    _persist_tick_id(project_state, tick_id)

def _cmd_recover_counter(args, config: Config) -> int:
    """Handle 'recover-counter' subcommand."""
    project = args.project

    # Validate project slug
    if not _validate_project_slug(project):
        log.error("invalid project slug: %r (must be alphanumeric, dot, dash, underscore)", project)
        return 2

    project_dir = config.projects_dir / project
    if not project_dir.exists():
        log.error("project not found: %s", project)
        return 2

    from .counter import recover_counter

    try:
        result = recover_counter(project_dir)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 2
    except (ValueError, OSError) as e:
        log.error("recover-counter failed: %s", e)
        return 2

    log.info("recover-counter: set counter to %d for project %s", result, project)
    print(f"Counter set to {result} for project {project}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        argv: Command-line arguments (default: sys.argv[1:]).

    Returns:
        Exit code (0 on success, 2 on error).
    """
    # Strip --verbose/--debug before argparse to avoid the subparser namespace
    # overwrite issue (subparser defaults overwrite root-level True values).
    verbose, debug, remaining = _strip_global_flags(argv or [])

    # Load config
    config = Config.from_env()

    # Configure logging based on flags
    log_path = config.state_dir / config.log_file_subpath
    if debug:
        configure_logging(log_path, config.log_retention_days, level=logging.DEBUG)
        vlog.setLevel(logging.INFO)
    elif verbose:
        configure_logging(log_path, config.log_retention_days, level=logging.INFO)
        vlog.setLevel(logging.INFO)
    else:
        configure_logging(log_path, config.log_retention_days)

    parser = build_parser()
    args = parser.parse_args(remaining)

    # Dispatch to subcommand
    if hasattr(args, "func"):
        return args.func(args, config)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
