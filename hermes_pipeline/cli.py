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
from .logging_setup import new_tick_id as _new_tick_id
from .merge import run_phase9, make_default_bump_fn
from .phases import load_phases
from .status import collect_pending, format_table
from .tick import TickLock, TickLockHeld

log = logging.getLogger(__name__)


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
        help="Run one pipeline tick: select a TODO and register kanban phases",
    )
    tick_parser.add_argument("project", help="Project name/slug")
    tick_parser.set_defaults(func=_cmd_tick)

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
    path = state_dir / "current_tick_id.txt"
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

def _load_cb_config(state_dir: Path):
    """Load circuit breaker config from TOML overlay, falling back to defaults."""
    from .config import Config as AppConfig, FullConfig, load_toml_overlay

    toml_path = state_dir / "pipeline.toml"
    try:
        base_cfg = AppConfig.from_env()
        full_cfg: FullConfig = load_toml_overlay(base_cfg, toml_path)
        if hasattr(full_cfg, "circuit_breaker"):
            return full_cfg.circuit_breaker
    except Exception:
        pass
    return CircuitBreakerConfig()

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
    """Reject project slugs that could inject CLI flags into subprocess calls."""
    import re as _re
    return bool(_re.match(r'^[a-zA-Z0-9._-]+$', slug))

def _persist_tick_id(state_dir: Path, tick_id: str) -> None:
    """Persist tick_id for the next tick's prior check."""
    try:
        (state_dir / "current_tick_id.txt").write_text(tick_id)
    except OSError as e:
        log.warning("failed to persist current_tick_id: %s", e)

def _cmd_tick(args, config: Config) -> int:
    """Handle 'tick' subcommand — kanban-as-scheduler pipeline tick.

    Flow:
    1. Read prior_tick_id, check if in-flight (skip if so)
    2. Acquire TickLock, mint ULID tick_id
    3. Build context, run selection
    4. If picked=None: observe circuit breaker, exit
    5. If picked=TODO-N: register kanban tasks, persist tick_id, exit

    tick_id is persisted AFTER kanban registration (inside the lock) so that
    a concurrent tick cannot see an unregistered tick_id and skip selection.
    If picked=None the tick_id is also persisted inside the lock so the next
    tick knows this tick was the last one to run (even though no phases were
    registered).
    """
    state_dir = config.state_dir
    project = args.project

    # Validate project slug to prevent CLI flag injection
    if not _validate_project_slug(project):
        log.error("invalid project slug: %r (must be alphanumeric, dot, dash, underscore)", project)
        return 2

    # --- Step 1: Check prior tick ---
    prior_tick_id = _read_prior_tick_id(state_dir)

    # --- Load circuit breaker config early (needed for prior-tick observation) ---
    cb_cfg = _load_cb_config(state_dir)
    cb = _make_circuit_breaker(state_dir, cb_cfg, config.slack_channel)

    if prior_tick_id is not None:
        # Prior tick exists — is it complete?
        if not all_phases_complete(project, prior_tick_id, state_dir=state_dir):
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
            # Feed outcomes back to the circuit breaker
            cb.observe_from_outcomes(
                state_dir=state_dir,
                prior_tick_id=prior_tick_id,
            )
        except Exception as e:
            log.warning("observe_outcomes for prior tick %s failed: %s", prior_tick_id, e)

    # --- Step 2: Acquire lock ---
    tick_lock = TickLock(state_dir, max_age_min=cb_cfg.max_tick_duration_min)

    tick_id = _generate_tick_id()

    try:
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
            from .config import FullConfig, SelectionConfig

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

            if picked is None:
                log.info("selection picked None, observing circuit breaker")
                # Write sentinel so next tick's observe_from_outcomes doesn't
                # count this as no-progress.
                try:
                    import json as _json
                    outcomes_dir = state_dir / "outcomes"
                    outcomes_dir.mkdir(parents=True, exist_ok=True)
                    phases_file = outcomes_dir / f"{tick_id}-phases.json"
                    with open(phases_file, "w") as f:
                        f.write(_json.dumps({"outcome": "picked_none"}) + "\n")
                except OSError as e:
                    log.warning("failed to write picked_none sentinel: %s", e)
                cb.observe(picked=None, counts_as_no_progress=True)
                # Persist inside lock so next tick knows this one ran
                _persist_tick_id(state_dir, tick_id)
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

            # Persist AFTER registration — inside the lock — so a concurrent
            # tick cannot see an unregistered tick_id and skip selection.
            _persist_tick_id(state_dir, tick_id)

            # --- Step 6: Observe circuit breaker ---
            cb.observe(picked=picked, counts_as_no_progress=False)

            return 0

    except TickLockHeld:
        log.error("tick lock held, exiting")
        return 1

def main(argv: Optional[list[str]] = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        argv: Command-line arguments (default: sys.argv[1:]).

    Returns:
        Exit code (0 on success, 2 on error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Load config
    config = Config.from_env()

    # Configure logging
    log_path = config.state_dir / config.log_file_subpath
    configure_logging(log_path, config.log_retention_days)

    # Dispatch to subcommand
    if hasattr(args, "func"):
        return args.func(args, config)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
