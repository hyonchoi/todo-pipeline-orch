"""Hermes pipeline orchestrator CLI.

Subcommands: merge, approve, status, kill.
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
from .config import Config, CircuitBreakerConfig, _validate_project_slug
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

# Seconds reserved from a project's tick budget for the non-LLM work
# (kanban registration, outcome observation) so the selection call is bounded
# strictly below the per-project lock's stale-reclaim window.
_SELECTION_TIMEOUT_RESERVE_S = 30


def _resolve_project_dir(config: Config, slug: str) -> Optional[Path]:
    """Validate *slug* and resolve it to an existing project directory.

    Returns the resolved Path, or None if the slug is invalid or the directory
    doesn't exist — in which case the reason is logged and the caller should
    return exit code 2. Centralizes the validate-then-resolve idiom so slug
    validation (CLI-flag / path-traversal defense) can't be forgotten at a
    call site.
    """
    if not _validate_project_slug(slug):
        log.error("invalid project slug: %s", slug)
        return None
    project_dir = config.projects_dir / slug
    if not project_dir.exists():
        log.error("project not found: %s", slug)
        return None
    return project_dir


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
    # Multi-project kill: scan all projects
    if project is None and config is not None:
        return _kill_all_projects(config, all_=all_, todo=todo)
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
        0 if successful, 1 if some kills unconfirmed, 2 if TODO not found.
    """
    from .project_config import _discover_projects

    projects = _discover_projects(config)
    if not projects:
        print("no active projects found")
        return 0

    # Both counters are project-scoped (a project is "unconfirmed" if its
    # cmd_kill couldn't confirm every kill). Don't mix phase counts with
    # project counts — only their zero/non-zero state drives the result below.
    projects_with_kills = 0
    projects_unconfirmed = 0
    # Track whether we checked any project for a specific TODO
    todo_checked = False
    todo_found = False

    for project_dir, _toml_data in projects:
        project_slug = project_dir.name
        project_state = project_dir / ".hermes"
        ps_dir = project_state / "phase_started"

        if not ps_dir.exists():
            log.debug("project %s: no phase_started dir, skipping", project_slug)
            continue

        # Count targets in this project
        if all_:
            targets = [f for f in ps_dir.iterdir() if f.is_file() and f.suffix == ".json"]
        elif todo:
            p = ps_dir / f"{todo}.json"
            todo_checked = True
            targets = [p] if p.exists() else []
            if p.exists():
                todo_found = True
        else:
            continue

        if not targets:
            log.debug("project %s: no targets for kill, skipping", project_slug)
            continue

        log.info("project %s: killing %d in-flight phases", project_slug, len(targets))
        # Kill phases in this project using existing cmd_kill logic
        result = cmd_kill(
            state_dir=project_state,
            all_=all_,
            todo=todo,
        )
        if result == 0:
            projects_with_kills += 1
        else:
            projects_unconfirmed += 1

    # If the user asked for a specific TODO and it wasn't found anywhere,
    # be explicit rather than hiding behind "no in-flight phases found".
    if todo and todo_checked and not todo_found:
        print(f"no in-flight phase for {todo} in any project")
        return 2

    if projects_with_kills == 0 and projects_unconfirmed == 0:
        print("no in-flight phases found")
        return 0
    elif projects_unconfirmed > 0:
        return 1
    else:
        return 0

def _parse_todo_id(value: str) -> int:
    """Parse todo_id argument with helpful error message."""
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"todo_id must be a number (you provided '{value}'). "
            f"Example: pipeline-watch merge myproject 123"
        )


def _parse_todo_id_flag(value: str) -> int:
    """Parse --todo argument, accepting 'TODO-N' or plain 'N' formats."""
    cleaned = value.removeprefix("TODO-").removeprefix("todo-")
    try:
        return int(cleaned)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--todo must be a TODO id (you provided '{value}'). "
            f"Example: --todo TODO-5 or --todo 5"
        )


def _strip_global_flags(argv: Optional[list[str]]) -> tuple[bool, bool, list[str]]:
    """Strip --verbose/--debug from argv, returning (verbose, debug, remaining).

    This avoids the argparse subparser namespace overwrite: if --verbose lives
    on both the root parser and a subparser, the subparser's default (False)
    overwrites the root's True. By stripping the flags upfront we configure
    logging before argparse ever runs.

    If argv is None, reads from sys.argv[1:] (same default as argparse).
    """
    verbose = False
    debug = False
    remaining = []
    for arg in (argv if argv is not None else sys.argv[1:]):
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
        description="Hermes pipeline orchestrator: merge, approve, status, and kill commands.",
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

    # approve: Phase 9 ship gate — bump-in-PR, merge, complete gate
    approve_parser = subparsers.add_parser(
        "approve",
        help="Ship a ready TODO: bump version in PR, merge to main, complete the gate",
    )
    approve_parser.add_argument("project", help="Project name")
    approve_parser.add_argument(
        "--todo", required=True, type=_parse_todo_id_flag,
        help="TODO to ship (e.g. TODO-5)",
    )
    approve_parser.add_argument(
        "--force", action="count", default=0,
        help="Pass twice (--force --force) to bypass ONLY the SHA-staleness guard (audited)",
    )
    approve_parser.set_defaults(func=_cmd_approve)

    # approve-plan: Phase 2b plan gate — approve/reject the decision sheet
    approve_plan_parser = subparsers.add_parser(
        "approve-plan",
        help="Approve or reject a plan-gate decision sheet for a TODO",
    )
    approve_plan_parser.add_argument("project", help="Project name")
    approve_plan_parser.add_argument(
        "--todo", required=True, type=_parse_todo_id_flag,
        help="TODO whose plan to approve/reject (e.g. TODO-5)",
    )
    ap_action = approve_plan_parser.add_mutually_exclusive_group(required=True)
    ap_action.add_argument(
        "--approve", action="store_true", help="Approve the plan",
    )
    ap_action.add_argument(
        "--reject", action="store_true", help="Reject the plan (requires --reason)",
    )
    approve_plan_parser.add_argument(
        "--override", action="append", metavar="Q_ID=LABEL", default=None,
        help="Override a recommendation (repeatable), e.g. --override q1=B. "
             "Only valid with --approve.",
    )
    approve_plan_parser.add_argument(
        "--reason", default=None,
        help="Rejection reason (required with --reject)",
    )
    approve_plan_parser.set_defaults(func=_cmd_approve_plan)

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
    kill_parser.add_argument("project", nargs="?", default=None, help="Project name (optional — omit to scan all projects)")
    kill_parser.set_defaults(func=_cmd_kill)

    # tick: Pipeline tick — select TODO, register kanban phases
    tick_parser = subparsers.add_parser(
        "tick",
        help="Run one pipeline tick: scan all projects and select TODOs",
    )
    tick_parser.add_argument("project", nargs="?", default=None, help="Project name (optional — omit to scan all projects)")
    tick_parser.set_defaults(func=_cmd_tick)

    # recover-counter: Scan TODOS.md and initialize counter file
    rc_parser = subparsers.add_parser(
        "recover-counter",
        help="Scan TODOS.md and initialize .hermes/todo_id_counter",
    )
    rc_parser.add_argument("project", help="Project name/slug")
    rc_parser.set_defaults(func=_cmd_recover_counter)

    # init: Write the default pipeline execution contract
    init_parser = subparsers.add_parser(
        "init",
        help="Write the default pipeline execution contract for a project",
    )
    init_parser.add_argument("project", help="Project name")
    init_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing contract with the current default",
    )
    init_parser.add_argument(
        "--assignee", default=None,
        help="Set the assignee field (e.g., --assignee pipeline)",
    )
    init_parser.set_defaults(func=_cmd_init)

    # doctor: Verify the pipeline execution contract
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Verify a project's pipeline execution contract against phases.yaml",
    )
    doctor_parser.add_argument("project", help="Project name")
    doctor_parser.set_defaults(func=_cmd_doctor)

    # install-profile: Install the bundled pipeline Hermes profile
    install_profile_parser = subparsers.add_parser(
        "install-profile",
        help="Install the bundled pipeline Hermes profile",
    )
    install_profile_parser.add_argument(
        "--force", action="store_true",
        help="Force reinstall even if the profile already exists",
    )
    install_profile_parser.set_defaults(func=_cmd_install_profile)

    return parser


def _cmd_approve(args, config: Config) -> int:
    """Handle 'approve' subcommand: deterministically ship a ready TODO.

    Exit codes: 0 shipped, 3 refused by a guard, 2 unexpected error.
    """
    from . import ship
    from .state_migration import _get_project_state_dir

    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    state_dir = _get_project_state_dir(project_dir)
    try:
        summary = ship.approve_ship(
            project_dir=project_dir,
            project_slug=args.project,
            todo_id=args.todo,
            state_dir=state_dir,
            force_count=args.force,
        )
        print(summary)
        return 0
    except ship.ApproveRefused as e:
        print(f"approve refused: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        log.error("approve command failed: %s", e, exc_info=True)
        return 2


def _cmd_approve_plan(args, config: Config) -> int:
    """Handle 'approve-plan' subcommand: approve or reject a plan-gate sheet.

    Exit codes: 0 success, 3 refused by a guard, 2 unexpected error.
    """
    from . import approve_plan as ap
    from .state_migration import _get_project_state_dir

    # Flag-combination guards — self-contained, no project dir needed.
    if args.reject and not args.reason:
        print(
            "approve-plan refused: --reject requires --reason "
            "(explain why the plan was rejected)",
            file=sys.stderr,
        )
        return 3
    if args.approve and args.reason:
        print(
            "approve-plan refused: --reason is only valid with --reject",
            file=sys.stderr,
        )
        return 3
    if args.reject and args.override:
        print(
            "approve-plan refused: --override is only valid with --approve",
            file=sys.stderr,
        )
        return 3

    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    state_dir = _get_project_state_dir(project_dir)
    try:
        overrides = ap._parse_overrides(args.override) if args.approve else None
        summary = ap.approve_plan(
            project_dir=project_dir,
            project_slug=args.project,
            todo_id=args.todo,
            state_dir=state_dir,
            overrides=overrides,
            reject_reason=args.reason if args.reject else None,
        )
        print(summary)
        return 0
    except ap.ApproveRefused as e:
        print(f"approve-plan refused: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        log.error("approve-plan command failed: %s", e, exc_info=True)
        return 2


def _cmd_merge(args, config: Config) -> int:
    """Handle 'merge' subcommand."""
    try:
        project = args.project
        todo_id = args.todo_id
        abandon = args.abandon

        # Find the project directory
        project_dir = _resolve_project_dir(config, project)
        if project_dir is None:
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
    """Handle 'kill' subcommand.

    If project is specified, kill in that project.
    If project is omitted and --all, scan all projects for in-flight phases.
    If project is omitted and --todo, kill that TODO across all projects.
    """
    from .state_migration import _get_project_state_dir

    # Resolve project-specific state directory when project is given
    if args.project is not None:
        project_dir = _resolve_project_dir(config, args.project)
        if project_dir is None:
            return 2
        state_dir = _get_project_state_dir(project_dir)
    else:
        state_dir = config.state_dir

    return cmd_kill(
        state_dir=state_dir,
        all_=args.all_,
        todo=args.todo,
        project=args.project,
        config=config,
    )


def _read_prior_tick_id(state_dir: Path) -> str | None:
    """Read the prior tick_id from current_tick_id.txt.

    Returns None if the file doesn't exist (cold start).
    Raises OSError if the file exists but can't be read (e.g., permissions).
    """
    path = state_dir / CURRENT_TICK_ID_FILE
    if not path.exists():
        return None
    try:
        return path.read_text().strip()
    except OSError as e:
        log.error("can't read %s: %s — aborting tick (prior state unreadable)", path, e)
        raise

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
        alert_dedup_hours=cb_cfg.alert_dedup_hours,
        slack_channel=slack_channel,
    )

def _persist_tick_id(state_dir: Path, tick_id: str, *, write_sentinel: bool = True) -> None:
    """Persist tick_id atomically for the next tick's prior check.

    Uses tmp+rename so a crash mid-write doesn't leave a partial file.
    Also writes a sentinel file so all_phases_complete can distinguish
    "persisted but never registered" from "persisted and registered".

    Args:
        state_dir: Per-project state directory.
        tick_id: The tick_id to persist.
        write_sentinel: If True (default), write tick_started sentinel.
            Set to False when the caller has already written a picked_none
            sentinel to the same file — the tick_started sentinel would
            overwrite it.
    """
    from .state import _atomic_write_text

    try:
        _atomic_write_text(state_dir / CURRENT_TICK_ID_FILE, tick_id)
    except OSError as e:
        log.error("failed to persist current_tick_id: %s — aborting tick", e)
        raise

    if not write_sentinel:
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

def _rotate_projects(
    projects: list[tuple[Path, dict | None]],
    state_dir: Path,
) -> list[tuple[Path, dict | None]]:
    """Rotate scan order by a persisted cursor for fairness.

    With per-project locks a slow or hung project no longer starves the
    others, but a fixed (alphabetical) discovery order would still always tick
    the same project first.  Rotating by a monotonically-increasing cursor
    spreads first-pick evenly across ticks and helps overlapping crons start on
    different projects, reducing lock contention.

    Best-effort: a missing/corrupt cursor restarts from 0, and a failed persist
    just means the next scan reuses the same offset (correctness is unaffected,
    only fairness).
    """
    n = len(projects)
    if n <= 1:
        return projects
    from .state import _atomic_write_text

    cursor_file = state_dir / "scan_cursor.txt"
    try:
        cursor = int(cursor_file.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        cursor = 0
    offset = cursor % n
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(cursor_file, f"{cursor + 1}\n")
    except OSError as e:
        log.warning("failed to persist scan cursor: %s — scan order may repeat", e)
    return projects[offset:] + projects[:offset]

def _cmd_tick(args, config: Config) -> int:
    """Handle 'tick' subcommand — kanban-as-scheduler pipeline scan tick.

    If a project name is provided, tick only that project.
    Otherwise, discover and tick all active projects.

    Flow:
    1. Discover active projects (or use specified project)
    2. One-time global state migration (single-project setups only)
    3. Rotate scan order for fairness
    4. For each project: acquire its own per-project lock, then run the
       per-project tick flow (prior-tick check, selection, circuit breaker,
       kanban registration) under that lock

    There is deliberately no single global lock.  Each project's tick is
    bounded independently by ``max_tick_duration_min`` (the per-project lock's
    stale-reclaim budget), so a slow or hung project cannot starve the rest of
    the scan, and an overlapping cron simply skips any project whose tick is
    already in flight.  Per-project errors are isolated — one project's failure
    (or held lock) doesn't block the others.
    """
    from .project_config import _discover_projects
    from .state_migration import _get_project_state_dir, _migrate_global_state
    from .tick import TickLock, TickLockHeld

    state_dir = config.state_dir

    # --- Step 1: Load global config overlay ---
    # Only the circuit-breaker config is needed at scan scope; the full overlay
    # is re-loaded per-project (each project may have its own config.toml).
    try:
        _, cb_cfg = _load_toml_overlay(state_dir, config)
    except Exception as e:
        log.warning("failed to load config overlay: %s — using defaults", e)
        from .config import CircuitBreakerConfig
        cb_cfg = CircuitBreakerConfig()

    scan_id = _generate_tick_id()  # scan-level id, for log correlation only
    vlog.info("starting scan: scan_id=%s state_dir=%s", scan_id, state_dir)

    # --- Step 2: Discover projects ---
    if args.project is not None:
        project_dir = _resolve_project_dir(config, args.project)
        if project_dir is None:
            return 2
        if not (project_dir / "TODOS.md").exists():
            log.error("no TODOS.md in project: %s", args.project)
            return 2
        from .project_config import _is_enabled, _read_project_toml
        if not _is_enabled(project_dir):
            log.error("project is disabled: %s — remove .hermes/project.toml or set enabled = true", args.project)
            return 2
        explicit_toml = _read_project_toml(project_dir)
        projects = [(project_dir, explicit_toml)]
    else:
        # Missing projects_dir is a configuration error, not "no projects to
        # process".  Distinguish so the cron doesn't silently run forever on a
        # misconfigured setup.
        if not config.projects_dir.is_dir():
            log.error("projects_dir %s does not exist — check config",
                      config.projects_dir)
            return 2
        projects = _discover_projects(config)
        if not projects:
            log.info("no active projects found in %s", config.projects_dir)
            return 0

    log.info("discovered %d active projects", len(projects))

    # --- Step 3: One-time global state migration ---
    # The old single-project state (~/.hermes/) belongs to whichever project
    # used it before.  Migrate it to the first project only — copying the same
    # current_tick_id.txt / circuit.json to every project would cause new
    # projects to inherit a stale tick_id they never owned, permanently
    # stalling as "prior tick in-flight".
    #
    # Only auto-migrate when there's exactly one project.  With multiple
    # projects we can't know which one owned the old state, so skip and ask the
    # operator to handle it manually.
    if len(projects) == 1:
        first_project, _ = projects[0]
        try:
            _migrate_global_state(first_project, config)
        except Exception as e:
            log.warning("one-time state migration to %s: %s",
                        first_project.name, e)
    else:
        global_src = config.state_dir / "current_tick_id.txt"
        if global_src.is_file():
            # Only warn once per session, not every tick — a persistent
            # global file is a known multi-project situation and the
            # operator is responsible for resolving it.
            warn_suppressed = state_dir / "migration_warning_suppressed"
            try:
                if not warn_suppressed.exists():
                    log.warning(
                        "global state exists at %s but %d projects were discovered — "
                        "can't determine which project owns the old state.  Migrate "
                        "manually to the correct project or remove the file.",
                        config.state_dir,
                        len(projects),
                    )
                    warn_suppressed.touch(exist_ok=True)
            except OSError:
                pass

    # --- Step 4: Fairness rotation, then per-project tick ---
    projects = _rotate_projects(projects, state_dir)

    for project_dir, project_toml in projects:
        project_slug = project_dir.name
        project_state = _get_project_state_dir(project_dir)
        project_state.mkdir(parents=True, exist_ok=True)

        # Each project takes its own lock, identified by the same tick_id the
        # selection will use, so `kill` can correlate the lock holder with the
        # phase_started markers it writes.  The lock's max_age == the
        # per-project tick budget.
        project_tick_id = _generate_tick_id()
        tick_lock = TickLock(project_state, max_age_min=cb_cfg.max_tick_duration_min)
        try:
            with tick_lock.acquire(project_tick_id):
                _tick_project(
                    project_dir=project_dir,
                    project_slug=project_slug,
                    project_state=project_state,
                    config=config,
                    cb_cfg=cb_cfg,
                    project_toml=project_toml,
                    tick_id=project_tick_id,
                )
        except TickLockHeld:
            log.info("project %s: tick already in flight (lock held), skipping",
                     project_slug)
        except Exception as e:
            log.error("project %s: %s", project_slug, e, exc_info=True)
            # Continue to next project

    vlog.info("scan complete: scan_id=%s", scan_id)
    return 0

def _tick_project(
    *,
    project_dir: Path,
    project_slug: str,
    project_state: Path,
    config: Config,
    cb_cfg,
    tick_id: str,
    project_toml: dict | None = None,
) -> None:
    """Run the tick flow for a single project.

    1. Check prior tick
    2. Run selection
    3. Register kanban phases or observe circuit breaker

    Args:
        project_dir: Project root directory.
        project_slug: Project name (derived from directory name).
        project_state: Per-project state directory (<project>/.hermes/).
        config: Global config.
        cb_cfg: Circuit breaker configuration.
        tick_id: The tick_id for this project's tick. Generated by the caller
            and used as the per-project lock holder id so `kill` can correlate
            the lock with the phase_started markers written under it.
        project_toml: Pre-parsed project.toml data (from _discover_projects).

    Raises:
        Exception: On any error (caller logs and continues to next project).

    Note:
        The caller holds this project's TickLock for the duration of this call.
    """
    from .contract import (
        CapabilityMismatchError,
        ContractMissingError,
        ContractSchemaError,
        ContractVersionMismatchError,
        CONTRACT_SCHEMA_VERSION,
        PipelineContract,
        contract_path,
        load_contract,
        missing_capabilities,
        required_capabilities,
    )

    phases = load_phases()

    try:
        contract = load_contract(project_state)
    except ContractMissingError:
        # Auto-compute capabilities from phases.yaml so a fresh project
        # doesn't break when a future phase requires a tool not in the
        # hardcoded DEFAULT_CAPABILITIES tuple.
        contract = PipelineContract(
            schema_version=CONTRACT_SCHEMA_VERSION,
            assignee="default",
            capabilities=tuple(sorted(required_capabilities(phases))),
        )
    except (ContractSchemaError, ContractVersionMismatchError) as e:
        log.error(
            "project %s: pipeline contract invalid: %s — run `pipeline-watch doctor %s` for details",
            project_slug, e, project_slug,
        )
        raise

    missing = missing_capabilities(contract, phases)
    if missing:
        log.error(
            "project %s: pipeline contract at %s is missing capabilities %s required by "
            "phases.yaml — edit the contract to add them, or run `pipeline-watch doctor %s` for details",
            project_slug, contract_path(project_state), sorted(missing), project_slug,
        )
        raise CapabilityMismatchError(f"contract missing capabilities: {sorted(missing)}")

    from .project_config import _resolve_slack_channel

    # Resolve per-project Slack channel
    slack_channel = _resolve_slack_channel(project_dir, env_channel=config.slack_channel, toml_data=project_toml)

    # Step 1: Check prior tick
    prior_tick_id = _read_prior_tick_id(project_state)

    cb = _make_circuit_breaker(project_state, cb_cfg, slack_channel)

    if prior_tick_id is not None:
        # Ship-gate: a blocked phase_9_ship makes all_phases_complete return
        # False, so detect/alert "ready to ship" BEFORE the early-return below.
        from . import ship
        ship.maybe_ship_ready(
            project_dir=project_dir,
            project_slug=project_slug,
            prior_tick_id=prior_tick_id,
            state_dir=project_state,
            slack_channel=slack_channel,
        )

        # Plan-gate: detect blocked plan-gate before all_phases_complete check.
        from . import gates
        gates.maybe_plan_gate_ready(
            project_dir=project_dir,
            project_slug=project_slug,
            prior_tick_id=prior_tick_id,
            state_dir=project_state,
            slack_channel=slack_channel,
        )

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

    # The selection LLM call is the dominant blocking step of a project tick.
    # It must finish before the per-project lock's stale-reclaim budget
    # (max_tick_duration_min) elapses — otherwise a concurrent cron could
    # reclaim the lock mid-call and double-tick the project. Bound the call by
    # the budget (less a reserve for registration/observe), clamped to the
    # agent's own sane floor/ceiling.
    from .decision.agent import MAX_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS

    budget_s = cb_cfg.max_tick_duration_min * 60
    selection_timeout_s = max(
        MIN_TIMEOUT_SECONDS,
        min(MAX_TIMEOUT_SECONDS, budget_s - _SELECTION_TIMEOUT_RESERVE_S),
    )

    decision = run_selection(
        tick_id=tick_id,
        ctx=ctx,
        cfg=full_cfg,
        timeout=selection_timeout_s,
    )
    picked = decision.picked

    vlog.info("project %s: selection result: picked=%s rationale=%s",
              project_slug, picked, decision.rationale[:200])

    if picked is None:
        log.info("project %s: selection picked None, observing circuit breaker",
                 project_slug)
        cb.observe(picked=None, counts_as_no_progress=True)

        # Write the picked_none sentinel BEFORE persisting the tick_id.
        # If we persist first and crash before writing the sentinel, the
        # next tick sees the new tick_id with no completion evidence and
        # treats the project as permanently in-flight.
        sentinel_written = False
        try:
            observe_outcomes(
                state_dir=project_state,
                tick_id=tick_id,
                status_map={},
            )
            outcomes_dir = project_state / "outcomes"
            outcomes_dir.mkdir(exist_ok=True)
            sentinel = outcomes_dir / f"{tick_id}-phases.json"
            from .state import _atomic_write_text
            _atomic_write_text(
                sentinel,
                json.dumps({"outcome": OUTCOME_PICKED_NONE}) + "\n",
            )
            sentinel_written = True
        except Exception as se:
            log.warning("project %s: failed to write picked_none sentinel: %s",
                        project_slug, se)

        # Only persist tick_id if the sentinel was actually written.
        # Persisting without the sentinel would permanently stall the
        # project on the next tick.
        if sentinel_written:
            # Persist tick_id without the tick_started sentinel — we already
            # wrote a picked_none sentinel to the same file above.
            _persist_tick_id(project_state, tick_id, write_sentinel=False)
        return

    # Step 4: Persist tick_id before registering kanban phases.
    # This prevents a crash window: if register_todo_phases succeeds but
    # persist fails (or we crash between them), the next tick has no
    # record of this tick and cold-starts → duplicate agent spawn.
    # The tick_started sentinel tells all_phases_complete that this tick
    # was legitimate even if registration crashed before creating kanban tasks.
    _persist_tick_id(project_state, tick_id)

    # Step 5: Register kanban phases
    log.info("project %s: selected %s, registering kanban phases", project_slug, picked)
    try:
        task_ids = register_todo_phases(
            todo_id=picked,
            tick_id=tick_id,
            board_slug=project_slug,
            project_dir=project_dir,
            assignee=contract.assignee,
        )
        log.info("project %s: registered %d kanban tasks for %s: %s",
                 project_slug, len(task_ids), picked, task_ids)
    except RuntimeError as e:
        log.error("project %s: kanban registration failed: %s", project_slug, e)
        # Write failure outcome so the circuit breaker knows
        try:
            from .decision.store import append_outcome
            append_outcome(
                project_state,
                tick_id,
                outcome="failed_to_spawn",
                detail={"todo_id": picked, "error": str(e)[:500]},
            )
        except Exception as se:
            log.warning("failed to write outcome sidecar: %s", se)
        raise

    # Observe circuit breaker
    cb.observe(picked=picked, counts_as_no_progress=False)

def _cmd_recover_counter(args, config: Config) -> int:
    """Handle 'recover-counter' subcommand."""
    project = args.project

    # Validate slug and resolve the project directory
    project_dir = _resolve_project_dir(config, project)
    if project_dir is None:
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


def _cmd_init(args, config: Config) -> int:
    """Handle 'init' subcommand — write the default pipeline execution contract."""
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    from .state_migration import _get_project_state_dir
    from .contract import contract_path, write_default_contract

    project_state = _get_project_state_dir(project_dir)
    path = contract_path(project_state)

    try:
        if args.force and path.exists():
            path.unlink()
        written = write_default_contract(project_state)
    except OSError as e:
        log.error("failed to write pipeline contract at %s: %s", path, e)
        return 1

    # If --assignee was provided, patch the assignee field in the written file
    assignee = getattr(args, "assignee", None)
    if assignee is not None and path.exists():
        import tomllib
        try:
            data = tomllib.loads(path.read_text())
            data["assignee"] = assignee
            # Re-render as TOML
            toml_lines = [
                "# Pipeline execution contract — read at tick start.",
                "# See docs/tutorial-getting-started.md and `pipeline-watch doctor --help`.",
            ]
            toml_lines.append(f'schema_version = {data["schema_version"]}')
            toml_lines.append(f'assignee = "{data["assignee"]}"')
            caps = data.get("capabilities", ["Read", "Write", "Edit", "Bash"])
            caps_toml = ", ".join(f'"{c}"' for c in caps)
            toml_lines.append(f"capabilities = [{caps_toml}]")
            path.write_text("\n".join(toml_lines) + "\n")
        except (tomllib.TOMLDecodeError, KeyError) as e:
            log.error("failed to patch assignee in %s: %s", path, e)
            return 1

    if written:
        print(f"Wrote pipeline execution contract: {path}")
    else:
        print(f"Pipeline execution contract already exists: {path} (use --force to regenerate)")
    return 0


def _cmd_doctor(args, config: Config) -> int:
    """Handle 'doctor' subcommand — verify the pipeline execution contract.

    Exit codes: 0 clean, 1 drift (capability mismatch), 2 missing/invalid
    contract or unknown project.
    """
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    from .state_migration import _get_project_state_dir
    from .contract import (
        ContractMissingError,
        ContractSchemaError,
        ContractVersionMismatchError,
        contract_path,
        load_contract,
        missing_capabilities,
    )

    project_state = _get_project_state_dir(project_dir)

    try:
        contract = load_contract(project_state)
    except ContractMissingError as e:
        print(f"MISSING: {e}")
        return 2
    except (ContractSchemaError, ContractVersionMismatchError) as e:
        print(f"INVALID: {e}")
        return 2

    phases = load_phases()
    missing = missing_capabilities(contract, phases)
    if missing:
        print(
            f"DRIFT: contract capabilities {sorted(contract.capabilities)} at "
            f"{contract_path(project_state)} are missing {sorted(missing)} "
            f"required by configs/phases.yaml — edit the contract to add them"
        )
        return 1

    print(
        f"OK: schema_version={contract.schema_version} assignee={contract.assignee} "
        f"capabilities={sorted(contract.capabilities)}"
    )
    return 0


def _cmd_install_profile(args, config: Config) -> int:
    """Handle 'install-profile' subcommand — install the bundled pipeline profile.

    Resolves the bundled distribution package-relative, shells
    `hermes profile install [--force]`, then verifies with
    `hermes profile show pipeline`.

    Exit codes: 0 success, 1 hermes install/show failure, 2 hermes not found.
    """
    from .contract import bundled_profile_dir

    profile_dir = bundled_profile_dir()

    if not (profile_dir / "distribution.yaml").exists():
        log.error("bundled profile distribution not found at %s", profile_dir)
        return 1

    cmd = ["hermes", "profile", "install", str(profile_dir)]
    if args.force:
        cmd.append("--force")

    print(f"Installing pipeline profile from {profile_dir}...")
    result = _cli_sp.run(cmd, text=True)
    if result.returncode != 0:
        print(f"Problem: `hermes profile install` failed (exit {result.returncode})")
        print(f"Cause: Hermes may not be installed, or the profile source is invalid.")
        if result.stderr:
            print(f"Details: {result.stderr.strip()}")
        print(f"Fix: Ensure Hermes is installed and accessible, then retry.")
        return 2

    # Post-install verification: prove the profile is resolvable
    print("Verifying profile installation...")
    verify = _cli_sp.run(
        ["hermes", "profile", "show", "pipeline"], text=True, capture_output=True
    )
    if verify.returncode != 0:
        print(f"Problem: Profile installed but `hermes profile show pipeline` failed.")
        print(f"Cause: Profile name may not match 'pipeline', or Hermes caching issue.")
        print(f"Fix: Run `hermes profile list` to check installed profiles.")
        return 1

    print("Pipeline profile installed successfully.")
    print()
    print("Next step: set the assignee in your project contract:")
    print("  pipeline-watch init <project> --assignee pipeline")
    print("Then verify with:")
    print("  pipeline-watch doctor <project>")
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
    verbose, debug, remaining = _strip_global_flags(argv)

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
