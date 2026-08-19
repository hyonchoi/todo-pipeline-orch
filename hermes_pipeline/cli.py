"""Hermes pipeline orchestrator CLI.

Subcommands: tick, approve, init, doctor, config, skills, recover-counter, test.
Scheduling is owned by Hermes kanban tasks.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess as _cli_sp
import sys
import tempfile
import time
import tomllib
from dataclasses import replace
from pathlib import Path

from hermes_pipeline import __version__

from .circuit import CircuitBreaker
from .config import CircuitBreakerConfig, Config, _validate_project_slug
from .decision import run_selection
from .decision.context import build_context
from .kanban_tasks import all_phases_complete, observe_outcomes
from .logging_setup import configure as configure_logging
from .logging_setup import new_tick_id as _new_tick_id
from .outcomes import CURRENT_TICK_ID_FILE, OUTCOME_PICKED_NONE
from .phases import load_phases
from .profile_prerequisites import (
    HERMES_SKILL_REGISTRY_ROOT as _HERMES_SKILL_REGISTRY_ROOT,
)
from .profile_prerequisites import (
    unverified_prerequisite_ids as _unverified_prerequisite_ids,
)
from .profile_prerequisites import verify_hermes_skill_registry_prerequisite
from .tick import TickLock, TickLockHeld

log = logging.getLogger(__name__)
vlog = logging.getLogger("pipeline.verbose")

# Seconds reserved from a project's tick budget for the non-LLM work
# (kanban registration, outcome observation) so the selection call is bounded
# strictly below the per-project lock's stale-reclaim window.
_SELECTION_TIMEOUT_RESERVE_S = 30
def _resolve_project_dir(config: Config, slug: str) -> Path | None:
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


def _verify_hermes_skill_registry_prerequisite(
    *, assignee: str, skill_id: str
) -> tuple[bool, str]:
    return verify_hermes_skill_registry_prerequisite(
        assignee=assignee,
        skill_id=skill_id,
        runner=_cli_sp.run,
    )


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


def _confirm_pid_exited(
    pid: int, *, term_grace_s: float = 5.0, kill_grace_s: float = 2.0
) -> bool:
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


def _parse_todo_id(value: str) -> int:
    """Parse todo_id argument with helpful error message."""
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"todo_id must be a number (you provided '{value}'). "
            f"Example: tpo merge myproject 123"
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


def _strip_global_flags(argv: list[str] | None) -> tuple[bool, bool, list[str]]:
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
    for arg in argv if argv is not None else sys.argv[1:]:
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
        prog="tpo",
        description="Hermes pipeline orchestrator: tick projects, manage pipeline setup, and handle legacy approval gates.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    # approve: legacy ship gate — bump-in-PR, merge, complete gate
    approve_parser = subparsers.add_parser(
        "approve",
        help="Legacy helper: ship a TODO from an existing ship-gate sidecar",
    )
    approve_parser.add_argument("project", help="Project name")
    approve_parser.add_argument(
        "--todo",
        required=True,
        type=_parse_todo_id_flag,
        help="TODO to ship (e.g. TODO-5)",
    )
    approve_parser.add_argument(
        "--force",
        action="count",
        default=0,
        help="Pass twice (--force --force) to bypass ONLY the SHA-staleness guard (audited)",
    )
    approve_parser.set_defaults(func=_cmd_approve)

    # tick: Pipeline tick — select TODO, register kanban phases
    tick_parser = subparsers.add_parser(
        "tick",
        help="Run one pipeline tick: scan all projects and select TODOs",
    )
    tick_parser.add_argument(
        "project",
        nargs="?",
        default=None,
        help="Project name (optional — omit to scan all projects)",
    )
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
        "--force",
        action="store_true",
        help="Overwrite an existing contract with the current default",
    )
    init_parser.add_argument(
        "--assignee",
        default=None,
        help="Set the assignee field (e.g., --assignee pipeline)",
    )
    init_parser.add_argument(
        "--profile",
        default="gstack",
        help="Pipeline skill-set profile (e.g., gstack, agent-skills). Default: gstack. "
        "Each profile defines a different set of phases and required capabilities.",
    )
    init_parser.set_defaults(func=_cmd_init)

    # doctor: Verify the pipeline execution contract
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Verify a project's pipeline execution contract against phases.yaml",
    )
    doctor_parser.add_argument("project", help="Project name")
    doctor_parser.set_defaults(func=_cmd_doctor)

    # plan validate: validate the selected TODO's attached Plan manifest
    plan_parser = subparsers.add_parser("plan", help="Inspect and validate TODO Plans")
    plan_subparsers = plan_parser.add_subparsers(dest="plan_command", required=True)
    plan_validate_parser = plan_subparsers.add_parser(
        "validate", help="Validate a TODO's attached Plan manifest"
    )
    plan_validate_parser.add_argument("project", help="Project name")
    plan_validate_parser.add_argument(
        "--todo", required=True, type=_parse_todo_id_flag, help="TODO to validate"
    )
    plan_validate_parser.add_argument(
        "--require-manifest",
        action="store_true",
        help="Reject a valid legacy Plan that has no tpo-plan block",
    )
    plan_validate_parser.set_defaults(func=_cmd_plan_validate)

    # install-profile: Install the bundled pipeline Hermes profile
    install_profile_parser = subparsers.add_parser(
        "install-profile",
        help="Install the bundled pipeline Hermes profile",
    )
    install_profile_parser.add_argument(
        "--force",
        action="store_true",
        help="Force reinstall even if the profile already exists",
    )
    install_profile_parser.set_defaults(func=_cmd_install_profile)

    # test: Mock integration test harness
    test_parser = subparsers.add_parser(
        "test",
        help="Run mock integration test harness against mock project data",
    )
    test_parser.add_argument(
        "--fixture",
        required=True,
        help="Fixture name to use (e.g., happy-path)",
    )
    test_parser.add_argument(
        "--profile",
        default="gstack",
        help="Bundled phase profile to test (default: gstack)",
    )
    test_parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Write a numbered report snapshot in the current workspace artifacts; "
            "cross-invocation auto-diff is unavailable"
        ),
    )
    test_parser.add_argument(
        "--phase",
        default=None,
        help="Run only a single phase by key (e.g., phase_2_autoplan)",
    )
    test_parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep temp directory after run for inspection",
    )
    test_parser.add_argument(
        "--timeout",
        type=int,
        default=86400,
        help="Overall run timeout in seconds (default: 86400 = 24h)",
    )
    test_parser.add_argument(
        "--convergence-threshold",
        type=int,
        default=3,
        help="Consecutive same-class failures to halt run (default: 3)",
    )
    test_parser.set_defaults(func=_cmd_test)

    # skills: bootstrap bundled skills into user/project skill directories
    skills_parser = subparsers.add_parser(
        "skills",
        help="Manage bundled agent skills (e.g. todos-manager)",
    )
    skills_subparsers = skills_parser.add_subparsers(
        dest="skills_command", required=True
    )

    skills_install_parser = skills_subparsers.add_parser(
        "install",
        help="Install the bundled todos-manager skill",
    )
    skills_install_parser.add_argument(
        "--target",
        choices=["codex", "claude", "all"],
        default="claude",
        help="Which skill directory convention to install into (default: claude)",
    )
    skills_install_parser.add_argument(
        "--scope",
        choices=["user", "project"],
        default="user",
        help="Install under the user's home directory or the current project (default: user)",
    )
    skills_install_parser.add_argument(
        "--reinstall",
        action="store_true",
        help="Replace an existing installed todos-manager skill after explicit review",
    )
    skills_install_parser.set_defaults(func=_cmd_skills_install)

    skills_uninstall_parser = skills_subparsers.add_parser(
        "uninstall",
        help="Remove the bundled todos-manager skill from selected skill directories",
    )
    skills_uninstall_parser.add_argument(
        "--target", choices=["codex", "claude", "all"], default="claude"
    )
    skills_uninstall_parser.add_argument(
        "--scope", choices=["user", "project"], default="user"
    )
    skills_uninstall_parser.add_argument(
        "-y", "--yes", action="store_true", help="Confirm deletion without prompting"
    )
    skills_uninstall_parser.set_defaults(func=_cmd_skills_uninstall)

    # config: read/write global tpo configuration
    config_parser = subparsers.add_parser(
        "config",
        help="Read and write global tpo configuration",
    )
    config_subparsers = config_parser.add_subparsers(
        dest="config_command", required=True
    )

    config_init_parser = config_subparsers.add_parser(
        "init",
        help="Create a global config file with documented defaults",
    )
    config_init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing config file",
    )
    config_init_parser.set_defaults(func=_cmd_config_init)

    config_get_parser = config_subparsers.add_parser(
        "get",
        help="Get the effective value of a config key",
    )
    config_get_parser.add_argument("key", help="Config key name")
    config_get_parser.set_defaults(func=_cmd_config_get)

    config_set_parser = config_subparsers.add_parser(
        "set",
        help="Set a config key in the global config file",
    )
    config_set_parser.add_argument("key", help="Config key name")
    config_set_parser.add_argument("value", help="New value")
    config_set_parser.set_defaults(func=_cmd_config_set)

    config_path_parser = config_subparsers.add_parser(
        "path",
        help="Show the path to the global config file",
    )
    config_path_parser.set_defaults(func=_cmd_config_path)

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

        ts = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d%H%M%S")
        rand = format(_secrets.randbelow(900000) + 100000, "06d")
        return f"{ts}{rand}"


def _load_toml_overlay(state_dir: Path, config: Config):
    """Load circuit breaker + selection config from .hermes/config.toml.

    Returns a tuple of (FullConfig or None, CircuitBreakerConfig).
    FullConfig is the complete overlay (selection, circuit_breaker).
    CircuitBreakerConfig is extracted for early use (before lock acquisition).
    On TOML error the overlay falls back to defaults with a warning.
    """
    from .config import FullConfig
    from .config import load_toml_overlay as _load_toml

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


def _has_pending_pr_handoff(
    project_dir: Path, state_dir: Path, *, work_branch: str | None = None
) -> tuple[bool, bool]:
    """Return (pending, counts_as_no_progress) for a Phase 8 PR handoff."""
    branch_file = state_dir / "pipeline_branch.txt"
    if work_branch is None and not branch_file.exists():
        log.warning(
            "phase 8 handoff completed but %s is missing; leaving project in handoff",
            branch_file,
        )
        return (True, True)

    if work_branch is None:
        work_branch = branch_file.read_text().strip()
    if not work_branch:
        log.warning(
            "phase 8 handoff completed but %s is empty; leaving project in handoff",
            branch_file,
        )
        return (True, True)

    try:
        result = _cli_sp.run(
            ["gh", "pr", "view", work_branch, "--json", "state,baseRefName,headRefName"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, _cli_sp.TimeoutExpired) as e:
        log.info(
            "project has PR handoff branch %s but merge state could not be "
            "verified (%s); leaving project in handoff",
            work_branch,
            e,
        )
        return (True, True)

    if result.returncode != 0:
        log.warning(
            "project has PR handoff branch %s but gh pr view failed: %s; "
            "leaving project in handoff",
            work_branch,
            result.stderr.strip()[:200],
        )
        return (True, True)

    try:
        view = json.loads(result.stdout)
        state = (view.get("state") or "").upper()
        head_ref = view.get("headRefName") or ""
    except json.JSONDecodeError:
        log.warning(
            "project has PR handoff branch %s but gh pr view returned "
            "non-JSON; leaving project in handoff",
            work_branch,
        )
        return (True, True)

    if head_ref != work_branch:
        log.warning(
            "project has PR handoff branch %s but gh resolved head branch %s; "
            "leaving project in handoff",
            work_branch,
            head_ref or "unknown",
        )
        return (True, True)

    if state == "MERGED":
        base_branch = view.get("baseRefName") or "main"
        if _sync_project_to_base_after_handoff(project_dir, base_branch):
            _clear_pr_handoff_state(state_dir)
            return (False, False)
        return (True, True)
    if state == "OPEN":
        return (True, False)
    log.warning(
        "project has PR handoff branch %s but PR state is %s; leaving project in handoff",
        work_branch,
        state or "unknown",
    )
    return (True, True)


def _sync_project_to_base_after_handoff(project_dir: Path, base_branch: str) -> bool:
    """Move a clean project checkout to the merged PR's updated base branch."""
    try:
        status = _cli_sp.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, _cli_sp.TimeoutExpired) as e:
        log.warning("cannot verify project checkout cleanliness after PR handoff: %s", e)
        return False

    if status.returncode != 0:
        log.warning(
            "cannot verify project checkout cleanliness after PR handoff: %s",
            status.stderr.strip()[:200],
        )
        return False
    if status.stdout.strip():
        log.warning(
            "project checkout has uncommitted changes after PR handoff; "
            "leaving project in handoff"
        )
        return False

    commands = [
        ["git", "fetch", "origin", base_branch],
        ["git", "checkout", base_branch],
        ["git", "merge", "--ff-only", f"origin/{base_branch}"],
    ]
    for cmd in commands:
        try:
            result = _cli_sp.run(
                cmd,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (FileNotFoundError, _cli_sp.TimeoutExpired) as e:
            log.warning("failed to sync project checkout after PR handoff: %s", e)
            return False
        if result.returncode != 0:
            log.warning(
                "failed to sync project checkout after PR handoff (%s): %s",
                " ".join(cmd),
                result.stderr.strip()[:200],
            )
            return False
    return True


def _clear_pr_handoff_state(state_dir: Path) -> None:
    """Clear completed PR-handoff markers after the checkout is synced to base."""
    for filename in ("pipeline_branch.txt", CURRENT_TICK_ID_FILE):
        path = state_dir / filename
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("failed to clear completed PR handoff marker %s: %s", path, e)


def _status_map_has_successful_pr_handoff(status_map: dict[str, str]) -> bool:
    """True only after the default finish-branch phase completed successfully."""
    return status_map.get("phase_8_finish_branch") == "done"


def _persist_tick_id(
    state_dir: Path, tick_id: str, *, write_sentinel: bool = True
) -> None:
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


def _record_failed_to_spawn(
    project_state: Path,
    tick_id: str,
    todo_id: str,
    error: Exception,
    *,
    reason: str,
) -> None:
    """Record a failed phase spawn without masking the primary failure."""
    try:
        from .decision.store import append_outcome

        append_outcome(
            project_state,
            tick_id,
            outcome="failed_to_spawn",
            detail={
                "todo_id": todo_id,
                "reason": reason,
                "error_type": type(error).__name__,
            },
        )
    except Exception as sidecar_exc:
        log.warning(
            "failed to write outcome sidecar: error_type=%s",
            type(sidecar_exc).__name__,
        )


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
            log.error(
                "project is disabled: %s — remove .hermes/project.toml or set enabled = true",
                args.project,
            )
            return 2
        explicit_toml = _read_project_toml(project_dir)
        projects = [(project_dir, explicit_toml)]
    else:
        # Missing projects_dir is a configuration error, not "no projects to
        # process".  Distinguish so the cron doesn't silently run forever on a
        # misconfigured setup.
        if not config.projects_dir.is_dir():
            log.error(
                "projects_dir %s does not exist — check config", config.projects_dir
            )
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
            log.warning("one-time state migration to %s: %s", first_project.name, e)
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
            log.info(
                "project %s: tick already in flight (lock held), skipping", project_slug
            )
        except Exception as e:
            log.error(
                "project %s: tick failed: error_type=%s",
                project_slug,
                type(e).__name__,
            )
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
        CONTRACT_SCHEMA_VERSION,
        CapabilityMismatchError,
        ContractMissingError,
        ContractSchemaError,
        ContractVersionMismatchError,
        PipelineContract,
        contract_path,
        load_contract,
        missing_capabilities,
        required_capabilities,
    )
    from .phases import (
        PhasePromptRenderError,
        load_phase_profile,
        load_profile_prerequisites,
        resolve_profile_phases_path,
    )

    try:
        contract = load_contract(project_state)
        phases_path = resolve_profile_phases_path(contract.profile)
        phase_profile = load_phase_profile(phases_path)
        phases = list(phase_profile.phases)
        prerequisites = load_profile_prerequisites(contract.profile)
    except ContractMissingError:
        # Auto-compute capabilities from phases.yaml so a fresh project
        # doesn't break when a future phase requires a tool not in the
        # hardcoded DEFAULT_CAPABILITIES tuple.
        phases_path = resolve_profile_phases_path("gstack")
        phase_profile = load_phase_profile(phases_path)
        phases = list(phase_profile.phases)
        prerequisites = load_profile_prerequisites("gstack")
        contract = PipelineContract(
            schema_version=CONTRACT_SCHEMA_VERSION,
            assignee="pipeline",
            capabilities=tuple(sorted(required_capabilities(phases))),
        )
        try:
            result = _cli_sp.run(
                ["hermes", "profile", "show", contract.assignee],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            profile_rc = result.returncode
        except FileNotFoundError:
            profile_rc = 127
        if profile_rc != 0:
            log.warning(
                "project %s has no pipeline contract at %s; falling back to "
                "assignee='pipeline', but `hermes profile show pipeline` failed "
                "(rc=%d). Run `tpo install-profile`, then `tpo init %s --assignee pipeline`.",
                project_slug,
                contract_path(project_state),
                profile_rc,
                project_slug,
            )
    except (ContractSchemaError, ContractVersionMismatchError) as e:
        log.error(
            "project %s: pipeline contract invalid: %s — run `tpo doctor %s` for details",
            project_slug,
            e,
            project_slug,
        )
        raise

    missing = missing_capabilities(contract, phases)
    if missing:
        log.error(
            "project %s: pipeline contract at %s is missing capabilities %s required by "
            "phases.yaml — edit the contract to add them, or run `tpo doctor %s` for details",
            project_slug,
            contract_path(project_state),
            sorted(missing),
            project_slug,
        )
        raise CapabilityMismatchError(
            f"contract missing capabilities: {sorted(missing)}"
        )

    from .kanban_tasks import reconcile_pending_task_create

    if not reconcile_pending_task_create(project_dir):
        log.warning(
            "project %s: unresolved Hermes task creation; skipping",
            project_slug,
        )
        return

    unverified = _unverified_prerequisite_ids(prerequisites, config.prompt_client)
    if unverified:
        log.error(
            "project %s: profile '%s' has Unverified prerequisites for prompt "
            "client '%s': %s — run `tpo doctor %s` for details",
            project_slug,
            contract.profile,
            config.prompt_client,
            ", ".join(unverified),
            project_slug,
        )
        raise RuntimeError(
            f"profile '{contract.profile}' has Unverified prerequisites for "
            f"prompt client '{config.prompt_client}': {', '.join(unverified)}"
        )

    from .project_config import _resolve_slack_channel

    # Resolve per-project Slack channel
    slack_channel = _resolve_slack_channel(
        project_dir, env_channel=config.slack_channel, toml_data=project_toml
    )

    # Step 1: Check prior tick
    prior_tick_id = _read_prior_tick_id(project_state)

    cb = _make_circuit_breaker(project_state, cb_cfg, slack_channel)

    if prior_tick_id is not None:
        pr_handoff_resolved = False
        # Legacy ship-gate compatibility: custom/older profiles can still have
        # a blocked phase_9_ship. Detect and write the sidecar before the
        # in-flight early return so `tpo approve` can finish those ticks.
        from . import ship

        ship.maybe_ship_ready(
            project_dir=project_dir,
            project_slug=project_slug,
            prior_tick_id=prior_tick_id,
            state_dir=project_state,
            slack_channel=slack_channel,
        )

        ship_sidecar = ship.read_sidecar(project_state, prior_tick_id)
        if ship_sidecar is not None:
            pending, counts_as_no_progress = _has_pending_pr_handoff(
                project_dir, project_state, work_branch=ship_sidecar.work_branch
            )
            if pending:
                cb.observe(
                    picked=None,
                    counts_as_no_progress=counts_as_no_progress,
                )
                log.info(
                    "project %s: prior tick %s is waiting on PR handoff, skipping",
                    project_slug,
                    prior_tick_id,
                )
                return
            pr_handoff_resolved = True

        if not pr_handoff_resolved and not all_phases_complete(
            project_slug, prior_tick_id, state_dir=project_state
        ):
            log.info(
                "project %s: prior tick %s still in-flight, skipping",
                project_slug,
                prior_tick_id,
            )
            return

        # Prior tick complete — fail closed if status/outcome observation breaks.
        try:
            from .kanban_tasks import get_todo_kanban_status

            status_map = get_todo_kanban_status(project_slug, prior_tick_id)
            observe_outcomes(
                state_dir=project_state,
                tick_id=prior_tick_id,
                status_map=status_map,
            )
            if (
                not pr_handoff_resolved
                and _status_map_has_successful_pr_handoff(status_map)
            ):
                pending, counts_as_no_progress = _has_pending_pr_handoff(
                    project_dir, project_state
                )
                if pending:
                    cb.observe(
                        picked=None,
                        counts_as_no_progress=counts_as_no_progress,
                    )
                    log.info(
                        "project %s: prior tick %s is waiting on PR handoff, skipping",
                        project_slug,
                        prior_tick_id,
                    )
                    return

            cb.observe_from_outcomes(
                state_dir=project_state,
                prior_tick_id=prior_tick_id,
            )
        except Exception as e:
            log.warning(
                "project %s: observe_outcomes for prior tick %s failed: %s",
                project_slug,
                prior_tick_id,
                e,
            )
            cb.observe(picked=None, counts_as_no_progress=True)
            return

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

    project_base_config = replace(config, state_dir=project_state)
    if toml_cfg is not None:
        full_cfg = FullConfig(
            base=project_base_config,
            selection=toml_cfg.selection,
            circuit_breaker=toml_cfg.circuit_breaker,
        )
    else:
        full_cfg = FullConfig(
            base=project_base_config,
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

    vlog.info(
        "project %s: selection result: picked=%s rationale=%s",
        project_slug,
        picked,
        decision.rationale[:200],
    )

    if picked is None:
        log.info(
            "project %s: selection picked None, observing circuit breaker: %s",
            project_slug,
            decision.rationale[:200],
        )
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
            log.warning(
                "project %s: failed to write picked_none sentinel: %s", project_slug, se
            )

        # Only persist tick_id if the sentinel was actually written.
        # Persisting without the sentinel would permanently stall the
        # project on the next tick.
        if sentinel_written:
            # Persist tick_id without the tick_started sentinel — we already
            # wrote a picked_none sentinel to the same file above.
            _persist_tick_id(project_state, tick_id, write_sentinel=False)
        return

    plan_path = None
    if phase_profile.requires_plan:
        from .todos_md import TodoPlanValidationError, resolve_todo_plan

        try:
            plan_path = resolve_todo_plan(project_dir, todos_path, picked)
        except TodoPlanValidationError as exc:
            _record_failed_to_spawn(
                project_state,
                tick_id,
                picked,
                exc,
                reason="plan_validation_failed",
            )
            cb.observe(picked=None, counts_as_no_progress=True)
            log.error(
                "project %s: selected TODO failed Plan validation: code=%s",
                project_slug,
                exc.code,
            )
            return

    # Step 4: Render every prompt before persisting the tick ID or mutating Hermes.
    from .kanban_tasks import create_prepared_todo_phases, prepare_todo_phases

    log.info("project %s: selected %s, registering kanban phases", project_slug, picked)
    try:
        prepared = prepare_todo_phases(
            todo_id=picked,
            tick_id=tick_id,
            board_slug=project_slug,
            phases_path=phases_path,
            prompt_client=config.prompt_client,
            plan_path=plan_path,
        )
    except PhasePromptRenderError as exc:
        _record_failed_to_spawn(
            project_state,
            tick_id,
            picked,
            exc,
            reason="phase_prompt_preparation_failed",
        )
        cb.observe(picked=None, counts_as_no_progress=True)
        log.error(
            "project %s: phase prompt preparation failed: error_type=%s",
            project_slug,
            type(exc).__name__,
        )
        return

    # Step 5: Persist immediately before the first Hermes mutation. The
    # tick_started sentinel preserves the existing registration-crash recovery.
    _persist_tick_id(project_state, tick_id)

    # Step 6: Create the already-rendered kanban phases.
    try:
        task_ids = create_prepared_todo_phases(
            prepared=prepared,
            tick_id=tick_id,
            board_slug=project_slug,
            project_dir=project_dir,
            assignee=contract.assignee,
        )
        log.info(
            "project %s: registered %d kanban tasks for %s: %s",
            project_slug,
            len(task_ids),
            picked,
            task_ids,
        )
    except RuntimeError as e:
        log.error(
            "project %s: kanban registration failed: error_type=%s",
            project_slug,
            type(e).__name__,
        )
        _record_failed_to_spawn(
            project_state,
            tick_id,
            picked,
            e,
            reason="kanban_registration_failed",
        )
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

    from .contract import (
        PROFILE_NAME_RE,
        ContractSchemaError,
        contract_path,
        write_default_contract,
    )
    from .phases import resolve_profile_phases_path
    from .state_migration import _get_project_state_dir

    profile = getattr(args, "profile", "gstack") or "gstack"
    if not PROFILE_NAME_RE.match(profile):
        msg = (
            f"invalid profile {profile!r}: must be a lowercase alphanumeric/hyphen "
            "string, 1-64 chars"
        )
        log.error(msg)
        print(f"ERROR: {msg}")
        return 2
    try:
        resolve_profile_phases_path(profile)
    except ContractSchemaError as e:
        log.error("invalid profile: %s", e)
        print(f"ERROR: {e}")
        return 2

    project_state = _get_project_state_dir(project_dir)
    path = contract_path(project_state)

    try:
        if args.force and path.exists():
            path.unlink()
        written = write_default_contract(project_state, profile)
    except OSError as e:
        log.error("failed to write pipeline contract at %s: %s", path, e)
        return 1

    # If --assignee was provided, patch the assignee field in the written file
    # by using the contract module's TOML renderer so future schema fields
    # are preserved automatically.
    assignee = getattr(args, "assignee", None)
    if assignee is not None and path.exists():
        try:
            data = tomllib.loads(path.read_text())
            from .contract import (
                DEFAULT_CAPABILITIES,
                PipelineContract,
                _render_contract_toml,
            )

            contract = PipelineContract(
                schema_version=data["schema_version"],
                assignee=assignee,
                capabilities=tuple(
                    data.get("capabilities", list(DEFAULT_CAPABILITIES))
                ),
                profile=data.get("profile", "gstack"),
            )
            path.write_text(_render_contract_toml(contract))
        except (tomllib.TOMLDecodeError, KeyError) as e:
            log.error("failed to patch assignee in %s: %s", path, e)
            return 1

    if written:
        print(f"Wrote pipeline execution contract: {path}")
    else:
        print(
            f"Pipeline execution contract already exists: {path} (use --force to regenerate)"
        )
    return 0


def _cmd_plan_validate(args, config: Config) -> int:
    """Validate the Plan attachment and optional manifest for one TODO."""
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2
    todo_id = f"TODO-{args.todo}"
    from .plan_manifest import PlanManifestValidationError, parse_plan_manifest
    from .todos_md import TodoPlanValidationError, resolve_todo_plan

    try:
        relative_plan = resolve_todo_plan(project_dir, project_dir / "TODOS.md", todo_id)
        document = (project_dir / relative_plan).read_text()
        manifest = parse_plan_manifest(document, expected_todo_id=todo_id)
    except TodoPlanValidationError as exc:
        print(f"Plan validation failed for {todo_id}: attachment_{exc.code}")
        return 1
    except (OSError, UnicodeError):
        print(f"Plan validation failed for {todo_id}: unreadable")
        return 1
    except PlanManifestValidationError as exc:
        print(f"Plan validation failed for {todo_id}: {exc.code}")
        return 1

    if manifest is None:
        if args.require_manifest:
            print(f"Plan validation failed for {todo_id}: --require-manifest requires a tpo-plan block")
            return 1
        print(f"Plan is valid legacy Markdown for {todo_id}; warning: no tpo-plan manifest")
        return 0
    suffix = "task" if len(manifest.tasks) == 1 else "tasks"
    print(f"Plan has a valid manifest for {todo_id}: {len(manifest.tasks)} {suffix}")
    return 0


def _cmd_doctor(args, config: Config) -> int:
    """Handle 'doctor' subcommand — verify the pipeline execution contract.

    Exit codes: 0 clean, 1 drift (capability mismatch), 2 missing/invalid
    contract, unknown project, or missing profile.
    """
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    from .contract import (
        ContractMissingError,
        ContractSchemaError,
        ContractVersionMismatchError,
        contract_path,
        load_contract,
        missing_capabilities,
    )
    from .state_migration import _get_project_state_dir

    project_state = _get_project_state_dir(project_dir)

    try:
        contract = load_contract(project_state)
    except ContractMissingError as e:
        print(f"MISSING: {e}")
        return 2
    except (ContractSchemaError, ContractVersionMismatchError) as e:
        print(f"INVALID: {e}")
        return 2

    # Load phases and prerequisite metadata from the selected profile.
    from .phases import load_profile_prerequisites, resolve_profile_phases_path

    try:
        profile_path = resolve_profile_phases_path(contract.profile)
        phases = load_phases(profile_path)
        prerequisites = load_profile_prerequisites(contract.profile)
    except ContractSchemaError as e:
        print(f"MISSING: {e}")
        return 2
    except Exception as e:
        print(
            f"INVALID: failed to load profile data for '{contract.profile}': {e}"
        )
        return 2

    missing = missing_capabilities(contract, phases)
    if missing:
        print(
            f"DRIFT: contract capabilities {sorted(contract.capabilities)} at "
            f"{contract_path(project_state)} are missing {sorted(missing)} "
            f"required by profile '{contract.profile}' — edit the contract to add them"
        )
        return 1

    # Verify the assigned profile is actually installed (non-default assignee only)
    if contract.assignee != "default":
        try:
            verify_result = _cli_sp.run(
                ["hermes", "profile", "show", contract.assignee],
                text=True,
                capture_output=True,
            )
        except FileNotFoundError:
            print(
                f"MISSING: Hermes is not on PATH, but contract assignee is set to "
                f"'{contract.assignee}'"
            )
            print("Cause: Hermes is not installed or not on PATH.")
            print("Fix: Install Hermes (https://hermos.dev) and ensure it is on PATH.")
            return 2
        if verify_result.returncode != 0:
            print(
                f"MISSING: Hermes profile '{contract.assignee}' is not installed, "
                f"but contract assignee is set to '{contract.assignee}'"
            )
            print(
                "Cause: The profile was never installed, or it was removed after install."
            )
            print(
                f"Fix: Install the bundled profile with `tpo install-profile`, "
                f"or create a custom profile named '{contract.assignee}' "
                f"with `hermes profile create {contract.assignee}`."
            )
            return 2

    print(
        f"prompt client: {config.prompt_client} "
        "(global for all projects under projects_dir)"
    )
    print(
        "Mixed-client fleets require separate project roots; per-project "
        "selection is deferred to TODO-42."
    )
    print(f"Prerequisites for profile '{contract.profile}':")
    has_unverified_prerequisites = False
    for prerequisite in prerequisites.skills:
        client = prerequisite.clients[config.prompt_client]
        if prerequisite.support == "Conditional":
            if (
                client.discovery_root == _HERMES_SKILL_REGISTRY_ROOT
            ):
                verified, detail = _verify_hermes_skill_registry_prerequisite(
                    assignee=contract.assignee,
                    skill_id=prerequisite.skill_id,
                )
                if not verified:
                    print(
                        f"MISSING: Hermes skill '{prerequisite.skill_id}' is not "
                        f"enabled for profile '{contract.assignee}'"
                    )
                    print(f"Cause: {detail}")
                    print(
                        "Fix: Install the bundled profile with `tpo install-profile`, "
                        "or enable the skill in the assigned Hermes profile."
                    )
                    return 2
                print(
                    f"- {prerequisite.skill_id} [Conditional]: "
                    f"discovery root {client.discovery_root}; "
                    f"invoke as {client.invocation}; verified locally"
                )
                continue
            print(
                f"- {prerequisite.skill_id} [Conditional]: "
                f"discovery root {client.discovery_root}; "
                f"invoke as {client.invocation}; worker provisioning is required"
            )
        else:
            has_unverified_prerequisites = True
            print(
                f"- {prerequisite.skill_id} [Unverified]: compatibility is "
                "not advertised as supported pending evidence"
            )

    unverified = _unverified_prerequisite_ids(prerequisites, config.prompt_client)
    if has_unverified_prerequisites:
        print(
            f"UNSUPPORTED: profile '{contract.profile}' has Unverified "
            f"prerequisites for prompt client '{config.prompt_client}': "
            f"{', '.join(unverified)}"
        )
        return 2

    print(
        f"OK: schema_version={contract.schema_version} assignee={contract.assignee} "
        f"profile={contract.profile} capabilities={sorted(contract.capabilities)}"
    )
    return 0


def _cmd_install_profile(args, config: Config) -> int:
    """Handle 'install-profile' subcommand — create the pipeline profile.

    Clones the active Hermes profile (`hermes profile create pipeline --clone`)
    to inherit a working config.yaml/.env/skills baseline, then overlays the
    bundled pipeline-specific SOUL.md on top. With --force, an existing
    `pipeline` profile is deleted first.

    Exit codes: 0 success, 1 SOUL.md missing / copy / show failure,
    2 hermes not found or `create` failed.
    """
    from .contract import bundled_profile_dir

    profile_name = "pipeline"
    soul_src = bundled_profile_dir() / "SOUL.md"

    if not soul_src.exists():
        log.error("bundled pipeline SOUL.md not found at %s", soul_src)
        return 1

    if args.force:
        print(f"Removing any existing '{profile_name}' profile...")
        try:
            delete_result = _cli_sp.run(
                ["hermes", "profile", "delete", profile_name, "-y"],
                text=True,
                capture_output=True,
            )
        except FileNotFoundError:
            print("Problem: `hermes` command not found.")
            print("Cause: Hermes is not installed or not on PATH.")
            print("Fix: Install Hermes (https://hermos.dev) and ensure it is on PATH.")
            return 2
        if delete_result.returncode != 0:
            detail = (
                delete_result.stderr.strip()
                if delete_result.stderr
                else f"exit {delete_result.returncode}"
            )
            print(
                "Problem: `hermes profile delete` failed. Profile was removed but may not be recreated."
            )
            print(f"Details: {detail}")
            print(
                "Cause: The delete succeeded in removing the old profile, but the delete command"
            )
            print(
                "         itself reported an error — the profile may still exist, or it may be gone."
            )
            print(
                "Fix: Run `hermes profile list` to check the current state, then retry."
            )
            return 2

    print(f"Creating '{profile_name}' profile cloned from the active profile...")
    cmd = ["hermes", "profile", "create", profile_name, "--clone"]
    try:
        result = _cli_sp.run(cmd, text=True, capture_output=True)
    except FileNotFoundError:
        print("Problem: `hermes` command not found.")
        print("Cause: Hermes is not installed or not on PATH.")
        print("Fix: Install Hermes (https://hermos.dev) and ensure it is on PATH.")
        return 2
    if result.returncode != 0:
        print(f"Problem: `hermes profile create` failed (exit {result.returncode})")
        if result.stderr:
            print(f"Details: {result.stderr.strip()}")
        print(
            f"Cause: A '{profile_name}' profile may already exist, "
            "or Hermes may not be installed."
        )
        print("Fix: Re-run with --force to replace the existing profile, or")
        print(f"     run `hermes profile delete {profile_name}` manually.")
        return 2

    # Locate the newly-created profile directory so we can overlay SOUL.md.
    print("Locating profile directory...")
    try:
        show = _cli_sp.run(
            ["hermes", "profile", "show", profile_name], text=True, capture_output=True
        )
    except FileNotFoundError:
        print("Problem: `hermes` command not found.")
        print("Cause: Hermes is not installed or not on PATH.")
        print("Fix: Install Hermes (https://hermos.dev) and ensure it is on PATH.")
        return 2
    if show.returncode != 0:
        print(
            f"Problem: Profile created but `hermes profile show {profile_name}` failed."
        )
        if show.stderr:
            print(f"Details: {show.stderr.strip()}")
        print(
            f"Cause: Profile name '{profile_name}' may not match what Hermes expects, or caching issue."
        )
        print("Fix: Run `hermes profile list` to check installed profiles.")
        return 1

    profile_path = None
    for line in show.stdout.splitlines():
        if line.strip().startswith("Path:"):
            profile_path = line.split(":", 1)[1].strip()
            break
    if not profile_path or not Path(profile_path).is_dir():
        print(
            f"Problem: Could not determine the profile path from `hermes profile show {profile_name}` output."
        )
        print("Cause: Hermes CLI output format may have changed.")
        print(
            "Fix: Run `hermes profile show pipeline` manually to inspect the profile."
        )
        return 1

    soul_dst = Path(profile_path) / "SOUL.md"
    try:
        tmp_dst = soul_dst.with_suffix(".tmp")
        shutil.copyfile(soul_src, tmp_dst)
        tmp_dst.rename(soul_dst)
    except OSError as exc:
        print(f"Problem: Failed to copy pipeline SOUL.md into {soul_dst}.")
        print(f"Details: {exc}")
        return 1

    print("Pipeline profile installed successfully.")
    print()
    print("Next step: set the assignee in your project contract:")
    print("  tpo init <project> --assignee pipeline")
    print("Then verify with:")
    print("  tpo doctor <project>")
    return 0


_SKILLS_INSTALL_TARGET_DIRNAMES = {
    "claude": ".claude/skills",
    "codex": ".agents/skills",
}


def _skills_install_targets(target: str, scope: str) -> list[tuple[str, Path]]:
    """Resolve (target_name, install_dir) pairs for --target/--scope."""
    base = Path.home() if scope == "user" else Path.cwd()
    names = ["claude", "codex"] if target == "all" else [target]
    return [(name, base / _SKILLS_INSTALL_TARGET_DIRNAMES[name]) for name in names]


def _preflight_skill_replacement(
    name: str, dest: Path, *, allow_symlink: bool = False
) -> str | None:
    def probe_writable(directory: Path, prefix: str) -> None:
        fd, probe_name = tempfile.mkstemp(prefix=prefix, dir=directory)
        try:
            os.close(fd)
            Path(probe_name).unlink()
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                Path(probe_name).unlink()
            except OSError:
                pass
            raise

    is_symlink = dest.is_symlink()
    if is_symlink and not allow_symlink:
        return "the destination is a symlink"
    if not is_symlink and dest.exists() and not dest.is_dir():
        return "the destination exists but is not a directory"
    if not is_symlink and dest.exists():
        try:
            probe_writable(dest, f".tpo-delete-probe-{name}-")
        except OSError as e:
            return f"the destination is not writable ({e})"
    parent = dest.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe_writable(parent, f".tpo-install-probe-{name}-")
    except OSError as e:
        return f"the install directory is not writable ({e})"
    return None


def _remove_skill_path(path: Path) -> None:
    """Remove a staged skill path without following symlinks."""
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)


def _skill_path_identity(path: Path) -> tuple[int, int, int] | None:
    """Return a no-follow identity used to detect replacement races."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _skill_backup_path(dest: Path) -> Path:
    """Reserve a same-directory backup name without leaving a directory behind."""
    backup = Path(tempfile.mkdtemp(prefix=".tpo-skill-backup-", dir=dest.parent))
    backup.rmdir()
    return backup


def _cmd_skills_uninstall(args, config: Config | None) -> int:
    targets = _skills_install_targets(args.target, args.scope)
    if not bool(getattr(args, "yes", False)):
        for name, install_dir in targets:
            dest = install_dir / "todos-manager"
            print(f"Problem ({name}): uninstall requires confirmation.")
            print(f"Cause: deleting {dest} removes the installed todos-manager skill.")
            print(
                f"Fix: rerun with `tpo skills uninstall --target {name} --scope {args.scope} --yes` "
                "to confirm deletion."
            )
        return 1

    preflight_errors: list[tuple[str, Path, str]] = []
    for name, install_dir in targets:
        dest = install_dir / "todos-manager"
        if dest.exists() or dest.is_symlink():
            reason = _preflight_skill_replacement(name, dest, allow_symlink=True)
            if reason is not None:
                preflight_errors.append((name, dest, reason))
    if preflight_errors:
        for name, dest, reason in preflight_errors:
            print(f"Problem ({name}): cannot replace todos-manager at {dest}.")
            print(f"Cause: {reason}.")
            print("Fix: make the destination removable, or uninstall it manually after reviewing local changes.")
        return 1

    existing_targets = [
        (name, install_dir / "todos-manager")
        for name, install_dir in targets
        if (install_dir / "todos-manager").exists()
        or (install_dir / "todos-manager").is_symlink()
    ]
    staged: list[tuple[str, Path, Path]] = []
    try:
        for name, dest in existing_targets:
            backup = _skill_backup_path(dest)
            dest.rename(backup)
            staged.append((name, dest, backup))
    except OSError as e:
        rollback_failures: list[tuple[str, Path, Path, OSError]] = []
        for name, dest, backup in reversed(staged):
            try:
                backup.rename(dest)
            except OSError as rollback_error:
                rollback_failures.append((name, dest, backup, rollback_error))
        print("Problem: could not stage every todos-manager destination for removal.")
        print(f"Cause: {e}")
        for name, dest, backup, rollback_error in rollback_failures:
            print(f"Problem ({name}): rollback could not restore {dest}.")
            print(f"Details: preserved backup at {backup}: {rollback_error}")
        print("Fix: inspect the listed destinations and preserved backups, then retry.")
        return 1

    cleanup_warnings: list[tuple[str, Path, OSError]] = []
    for name, _dest, backup in staged:
        try:
            _remove_skill_path(backup)
        except OSError as e:
            cleanup_warnings.append((name, backup, e))

    staged_destinations = {(name, dest) for name, dest, _backup in staged}
    for name, install_dir in targets:
        dest = install_dir / "todos-manager"
        if (name, dest) in staged_destinations:
            print(f"OK ({name}): removed todos-manager from {dest}")
        else:
            print(f"OK ({name}): todos-manager is not installed at {dest}")

    for name, backup, e in cleanup_warnings:
        print(f"Warning ({name}): removal could not clean the staged backup at {backup}.")
        print(f"Details: {e}")
        print("Fix: review the leftover path and remove it manually when its contents are safe to delete.")
    return 1 if cleanup_warnings else 0


def _cmd_skills_install(args, config: Config | None) -> int:
    """Handle 'skills install' subcommand — copy the bundled todos-manager skill.

    Copies hermes_pipeline/data/skills/todos-manager/ to one or both of
    ~/.claude/skills/todos-manager/ and ~/.agents/skills/todos-manager/
    (or their project-scoped equivalents). Existing destinations require
    explicit --reinstall.

    Exit codes: 0 all targets installed, 1 source missing / any target failed.
    """
    from .contract import _resolve_bundled_dir

    source = _resolve_bundled_dir("skills", "todos-manager")
    if not source.is_dir():
        print(f"Problem: bundled todos-manager skill not found at {source}.")
        print("Cause: the installed package is missing its bundled skill data.")
        print(
            "Fix: reinstall with `uv tool install hermes-pipeline` (or `uv sync` in a checkout)."
        )
        return 1

    targets = _skills_install_targets(args.target, args.scope)
    any_failed = False
    reinstall = bool(getattr(args, "reinstall", False))
    preflight_errors: list[tuple[str, Path, str]] = []
    preflight_identities: dict[str, tuple[int, int, int] | None] = {}
    for name, install_dir in targets:
        dest = install_dir / "todos-manager"
        preflight_identities[name] = _skill_path_identity(dest)
        if reinstall:
            reason = _preflight_skill_replacement(name, dest)
            if reason is not None:
                preflight_errors.append((name, dest, reason))
        elif dest.exists() or dest.is_symlink():
            preflight_errors.append((name, dest, "todos-manager is already installed"))
    if preflight_errors:
        for name, dest, reason in preflight_errors:
            if not reinstall and reason == "todos-manager is already installed":
                print(f"Problem ({name}): todos-manager is already installed at {dest}.")
                print("Cause: reinstalling without --reinstall would overwrite local changes.")
                print(
                    f"Fix: rerun with `tpo skills install --target {name} --scope {args.scope} --reinstall` "
                    "after reviewing the destination."
                )
                continue
            print(f"Problem ({name}): cannot replace todos-manager at {dest}.")
            print(f"Cause: {reason}.")
            print(
                "Fix: make the destination removable, or uninstall it manually "
                "after reviewing local changes."
            )
        return 1

    if reinstall:
        return _reinstall_skills_transactionally(
            source, targets, preflight_identities
        )

    for name, install_dir in targets:
        dest = install_dir / "todos-manager"
        if dest.exists() or dest.is_symlink():
            any_failed = True
            print(f"Problem ({name}): todos-manager is already installed at {dest}.")
            print("Cause: reinstalling without --reinstall would overwrite local changes.")
            print(
                f"Fix: rerun with `tpo skills install --target {name} --scope {args.scope} --reinstall` "
                "after reviewing the destination."
            )
            continue
        try:
            install_dir.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                raise FileExistsError(f"destination appeared before copy: {dest}")
            shutil.copytree(source, dest)
            print(f"OK ({name}): installed todos-manager to {dest}")
        except PermissionError as e:
            any_failed = True
            print(f"Problem ({name}): permission denied writing to {dest}.")
            print(f"Details: {e}")
            print(f"Cause: the current user lacks write access to {install_dir}.")
            print(
                f"Fix: check permissions on {install_dir}, or rerun with --scope project."
            )
        except OSError as e:
            any_failed = True
            print(f"Problem ({name}): failed to install todos-manager to {dest}.")
            print(f"Details: {e}")
            print("Cause: an OS-level error occurred during copy.")
            print(f"Fix: inspect {install_dir} and retry.")

    return 1 if any_failed else 0


def _reinstall_skills_transactionally(
    source: Path,
    targets: list[tuple[str, Path]],
    preflight_identities: dict[str, tuple[int, int, int] | None],
) -> int:
    """Replace every selected skill target or restore all original targets."""
    prepared: list[tuple[str, Path, Path, Path | None]] = []
    staged_paths: list[Path] = []
    swapped: list[tuple[str, Path, Path | None]] = []
    try:
        for name, install_dir in targets:
            install_dir.mkdir(parents=True, exist_ok=True)
            dest = install_dir / "todos-manager"
            staged = Path(
                tempfile.mkdtemp(prefix=".tpo-skill-stage-", dir=install_dir)
            )
            staged.rmdir()
            staged_paths.append(staged)
            shutil.copytree(source, staged)
            if _skill_path_identity(dest) != preflight_identities[name]:
                raise OSError(f"destination changed after preflight: {dest}")
            backup = _skill_backup_path(dest) if dest.exists() else None
            prepared.append((name, dest, staged, backup))

        for name, dest, staged, backup in prepared:
            if _skill_path_identity(dest) != preflight_identities[name]:
                raise OSError(f"destination changed before replacement: {dest}")
            if backup is not None:
                dest.rename(backup)
            try:
                staged.rename(dest)
            except OSError:
                if backup is not None:
                    backup.rename(dest)
                raise
            swapped.append((name, dest, backup))
    except OSError as error:
        rollback_failures: list[tuple[str, Path, Path | None, OSError]] = []
        for name, dest, backup in reversed(swapped):
            try:
                if dest.exists() or dest.is_symlink():
                    _remove_skill_path(dest)
                if backup is not None:
                    backup.rename(dest)
            except OSError as rollback_error:
                rollback_failures.append((name, dest, backup, rollback_error))
        for staged in staged_paths:
            if staged.exists() or staged.is_symlink():
                try:
                    _remove_skill_path(staged)
                except OSError:
                    pass
        print("Problem: could not replace every todos-manager destination.")
        print(f"Cause: {error}")
        for name, dest, backup, rollback_error in rollback_failures:
            preserved = f"; preserved backup at {backup}" if backup is not None else ""
            print(f"Problem ({name}): rollback could not restore {dest}{preserved}.")
            print(f"Details: {rollback_error}")
        print("Fix: inspect the listed destinations and preserved backups, then retry.")
        return 1

    cleanup_warnings: list[tuple[str, Path, OSError]] = []
    for name, _dest, backup in swapped:
        if backup is None:
            continue
        try:
            _remove_skill_path(backup)
        except OSError as error:
            cleanup_warnings.append((name, backup, error))

    for name, dest, _backup in swapped:
        print(f"OK ({name}): installed todos-manager to {dest}")
    for name, backup, error in cleanup_warnings:
        print(f"Warning ({name}): reinstall left the prior version at {backup}.")
        print(f"Details: {error}")
        print("Fix: review the backup and remove it manually when safe.")
    return 1 if cleanup_warnings else 0


def _cmd_config_init(args, config: Config | None) -> int:
    """Handle 'config init' — create a default config file at the default path."""
    from .config_loader import SKELETON, default_config_path

    path = default_config_path()
    if path.is_symlink():
        print(
            f"Error: config file {path} is a symlink — refused for security.",
            file=sys.stderr,
        )
        return 2
    if path.is_file() and not args.force:
        print(f"Config file already exists at {path}")
        print("Use --force to overwrite.")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_config(path, SKELETON)
    print(f"OK: created {path}")
    return 0


def _cmd_config_path(args, config: Config | None) -> int:
    """Handle 'config path' — show the effective config file location."""
    from .config_loader import default_config_path, find_config_file

    existing = find_config_file()
    if existing:
        print(f"Using: {existing}")
    else:
        print("No config file found.")
        print(f"Default path: {default_config_path()}")
        print("Run `tpo config init` to create one.")
    return 0


def _cmd_config_get(args, config: Config | None) -> int:
    """Handle 'config get <key>' — show effective value with source attribution."""
    from .config import Config
    from .config_loader import (
        find_config_file,
        load_global_config_with_active_keys,
        validate_config_key,
    )

    try:
        key = validate_config_key(args.key)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    try:
        cfg, active_keys = load_global_config_with_active_keys()
    except ValueError as e:
        print(f"Warning: config file has errors: {e}")
        print("Falling back to defaults.")
        cfg = Config.default()
        active_keys = set()

    value = getattr(cfg, key)

    # Determine source attribution
    default_cfg = Config.default()
    if key in active_keys:
        cfg_file = find_config_file()
        source = f" (from file: {cfg_file})" if cfg_file else " (from config file)"
    elif key == "projects_dir" and "PIPELINE_PROJECTS_DIR" in os.environ:
        source = " (from env: PIPELINE_PROJECTS_DIR)"
        value = Config.from_env().projects_dir
    elif value != getattr(default_cfg, key):
        cfg_file = find_config_file()
        source = f" (from file: {cfg_file})" if cfg_file else " (from config file)"
    else:
        source = " (from default)"

    print(f"{key}: {value}{source}")
    return 0


def _cmd_config_set(args, config: Config | None) -> int:
    """Handle 'config set <key> <value>' — write a key to the config file."""
    from .config_loader import (
        _format_value,
        default_config_path,
        find_config_file,
        validate_config_key,
        validate_config_value,
    )

    try:
        key = validate_config_key(args.key)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    try:
        coerced = validate_config_value(args.value, key)
    except (TypeError, ValueError) as e:
        print(f"Error: invalid value for {args.key!r}: {e}", file=sys.stderr)
        return 2

    config_file = find_config_file()
    if config_file is None:
        config_file = default_config_path()
        config_file.parent.mkdir(parents=True, exist_ok=True)

    if config_file.is_symlink():
        print(
            f"Error: config file {config_file} is a symlink — refused for security.",
            file=sys.stderr,
        )
        return 2

    config_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = config_file.with_name(f"{config_file.name}.lock")
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        text = (
            config_file.read_text()
            if config_file.is_file()
            else "# tpo global configuration\n\n"
        )
        lines = text.split("\n")

        # Prefer the effective active key. YAML parsers keep the last duplicate,
        # so update that before falling back to a commented skeleton placeholder.
        active_idx = None
        commented_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") and f"{key}:" in stripped:
                uncommented = stripped.lstrip("#").lstrip()
                if uncommented.startswith(f"{key}:") and commented_idx is None:
                    commented_idx = i
            elif stripped.startswith(f"{key}:"):
                active_idx = i
        found_idx = active_idx if active_idx is not None else commented_idx

        formatted = _format_value(coerced, key)
        new_line = f"{key}: {formatted}"

        if found_idx is not None:
            lines[found_idx] = new_line
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(new_line)
            lines.append("")

        new_text = "\n".join(lines)
        _atomic_write_config(config_file, new_text)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    print(f"OK: set {key} = {coerced}")
    print(f"File: {config_file}")
    return 0


def _atomic_write_config(path: Path, text: str) -> None:
    """Atomically write config text without following symlink targets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _cmd_test(args, config: Config) -> int:
    """Handle 'test' subcommand — mock integration test harness."""
    from .harness import HarnessProfileError, run_harness

    try:
        result = run_harness(
            fixture_name=args.fixture,
            loop=args.loop,
            phase_only=args.phase,
            keep_dir=args.keep,
            timeout=args.timeout,
            convergence_threshold=args.convergence_threshold,
            config=config,
            profile_name=args.profile,
        )
        if result.exit_code != 0:
            return result.exit_code
        return 0
    except HarnessProfileError as e:
        log.error(
            "test harness profile setup failed: code=%s profile=%s",
            e.code,
            e.profile_name,
        )
        return 2
    except Exception as e:
        log.error("test harness failed: error_type=%s", type(e).__name__)
        return 2


def main(argv: list[str] | None = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        argv: Command-line arguments (default: sys.argv[1:]).

    Returns:
        Exit code (0 on success, 2 on error).
    """
    verbose, debug, remaining = _strip_global_flags(argv)

    parser = build_parser()
    args = parser.parse_args(remaining)

    # Bootstrap subcommands (file-copy only) don't need pipeline runtime
    # config (state dir, projects dir) — skip Config.from_env()
    # so they work even when that env isn't configured yet.
    if getattr(args, "command", None) in ("skills", "config"):
        if hasattr(args, "func"):
            return args.func(args, None)
        parser.parse_args([*remaining, "--help"])
        return 0

    config = Config.from_env()

    log_path = config.state_dir / config.log_file_subpath
    if debug:
        configure_logging(log_path, config.log_retention_days, level=logging.DEBUG)
        vlog.setLevel(logging.INFO)
    elif verbose:
        configure_logging(log_path, config.log_retention_days, level=logging.INFO)
        vlog.setLevel(logging.INFO)
    else:
        configure_logging(log_path, config.log_retention_days)

    if hasattr(args, "func"):
        return args.func(args, config)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
