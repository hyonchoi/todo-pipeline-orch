"""Hermes pipeline orchestrator CLI.

Subcommands: tick, approve, init, doctor, config, plan validate,
todos complete, todos labels sync, test.
Scheduling is owned by Hermes kanban tasks.
"""

from __future__ import annotations

import argparse
import datetime
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
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
_MINIMUM_HERMES_VERSION = (0, 19, 0)


def _doctor_hermes_version() -> tuple[bool, str]:
    """Return whether the locally executable Hermes satisfies TPO's API floor."""
    try:
        result = _cli_sp.run(
            ["hermes", "--version"], text=True, capture_output=True
        )
    except FileNotFoundError:
        return False, "Hermes is not installed or not on PATH"
    if result.returncode != 0:
        return False, "Hermes --version failed"
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\bv?(\d+)\.(\d+)\.(\d+)\b", output)
    if match is None:
        return False, "Hermes version output was not recognized"
    version = tuple(int(part) for part in match.groups())
    rendered = ".".join(str(part) for part in version)
    if version < _MINIMUM_HERMES_VERSION:
        return False, f"Hermes {rendered} is older than required 0.19.0"
    return True, rendered


_MANIFEST_REQUIRED_HINT = (
    "Hint: an embedded Plan needs a manifest (```json tpo-plan``` block); "
    "see docs/templates/tpo-plan.md and the \"Migrating from gstack\" section of "
    "docs/howto-native-sdd-profile.md"
)


def _doctor_github_checks(
    project_dir: Path, state_dir: Path, *, project: str, requires_plan: bool
) -> bool:
    """Report GitHub auth, repository, label vocabulary, plan readiness and runs.

    Every check is offline-tolerant: a ``GitHubIssuesError`` prints one WARNING
    line and the remaining checks still run. Returns False when any WARNING or
    INVALID line was printed.
    """
    from collections.abc import Mapping

    from . import github_issues
    from .github_issues import SUPPORTED_REGISTRATION_SCHEMA_VERSIONS, GitHubIssuesError
    from .run_registration import (
        _registration_issue_number,
        active_registration_issue_numbers,
        registration_state,
    )

    ok = True
    try:
        github_issues.check_auth(project_dir)
        print("GitHub auth: ok")
    except GitHubIssuesError as exc:
        print(f"WARNING: GitHub auth unavailable ({exc.code})")
        ok = False

    repo: str | None = None
    try:
        repo = github_issues.repository_identity(project_dir)
        print(f"Repository: {repo}")
    except GitHubIssuesError as exc:
        if exc.code == "origin_identity_invalid":
            detail = exc.detail or "origin remote could not be resolved"
            print(f"INVALID: repository identity: {detail}")
        else:
            print(f"WARNING: Repository unavailable ({exc.code})")
        ok = False

    if repo is not None:
        try:
            present = {name.lower() for name in github_issues.list_labels(project_dir, repo=repo)}
            missing = [
                name for name, _color, _description in github_issues.LABEL_VOCABULARY
                if name.lower() not in present
            ]
            if missing:
                print(f"INVALID: missing {', '.join(missing)}; Fix: tpo todos labels sync {project}")
                ok = False
            else:
                print("Label vocabulary: ok")
        except GitHubIssuesError as exc:
            print(f"WARNING: Label vocabulary unavailable ({exc.code})")
            ok = False

        active_ids = active_registration_issue_numbers(state_dir)
        try:
            issues = github_issues.list_todo_issues(project_dir, repo=repo)
            readiness = github_issues.compile_eligible_issues(
                project_dir,
                issues,
                in_flight=(),
                active_registration_ids=active_ids,
                kanban_available=False,
                requires_plan=requires_plan,
            )
            summary: dict[str, int] = {}
            for reason in readiness.blocked_reasons.values():
                prefix = reason.partition(":")[0]
                summary[prefix] = summary.get(prefix, 0) + 1
            line = (
                f"Plan readiness: eligible={len(readiness.candidates)} "
                f"blocked={len(readiness.blocked_reasons)}"
            )
            if summary:
                line += " (" + " ".join(f"{key}={summary[key]}" for key in sorted(summary)) + ")"
            print(line)
            if "plan_invalid:manifest_required" in readiness.blocked_reasons.values():
                print(_MANIFEST_REQUIRED_HINT)
        except GitHubIssuesError as exc:
            print(f"WARNING: Plan readiness unavailable ({exc.code})")
            ok = False

    counts = {"active": 0, "delivered": 0, "abandoned": 0}
    unsupported: list[str] = []
    active_runs: list[tuple[str, int, Mapping]] = []
    runs_dir = state_dir / "runs"
    if runs_dir.exists() and not runs_dir.is_dir():
        print(f"WARNING: {runs_dir} is not a directory")
        ok = False
    elif runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*/registration.json")):
            tick_id = path.parent.name
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                unsupported.append(tick_id)
                continue
            number = _registration_issue_number(payload)
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema_version") not in SUPPORTED_REGISTRATION_SCHEMA_VERSIONS
                or number is None
            ):
                unsupported.append(tick_id)
                continue
            state = registration_state(path.parent)
            counts[state] += 1
            if state == "active":
                active_runs.append((tick_id, number, payload))
    line = (
        f"Runs: active={counts['active']} delivered={counts['delivered']} "
        f"abandoned={counts['abandoned']}"
    )
    if unsupported:
        line += f" unsupported={len(unsupported)}"
    print(line)
    for tick_id in unsupported:
        print(f"tick {tick_id}: unsupported or malformed registration")
    try:
        current_tick = (state_dir / CURRENT_TICK_ID_FILE).read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeError):
        current_tick = None
    for tick_id, number, payload in active_runs:
        if current_tick is None:
            print(f"tick {tick_id} → #{number} (active; no current tick)")
            continue
        print(f"tick {tick_id} → #{number}")
        if tick_id != current_tick:
            print(f"WARNING: run {tick_id} is active but is not the current tick")
            fix = (
                f"Fix (tick {tick_id}): tpo todos complete {project} --todo {number} --pr <pr> "
                f"if delivered, or touch {runs_dir / tick_id / 'abandoned'} to give up"
            )
            worktree, branch = payload.get("worktree"), payload.get("branch")
            if isinstance(worktree, str) and isinstance(branch, str) and worktree and branch:
                from .result_contract import sanitize_result_text

                fix += (
                    f", then git worktree remove --force "
                    f"{shlex.quote(sanitize_result_text(worktree, maximum=4096))} && "
                    f"git branch -D {shlex.quote(sanitize_result_text(branch, maximum=255))}"
                )
            print(fix)
            ok = False
    return ok


def _doctor_active_registration(project_dir: Path, state_dir: Path) -> bool:
    """Verify immutable base authority and report mutable lifecycle separately.

    Every non-OK verdict prints its own ``Fix (tick <id>):`` line except
    ``REGISTRATION UNSUPPORTED``, whose message is already the instruction.
    """
    tick_path = state_dir / CURRENT_TICK_ID_FILE
    if not tick_path.is_file():
        return True
    try:
        tick_id = tick_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        print("REGISTRATION DRIFT: current tick id could not be read")
        print(f"Fix (tick <unknown>): repair {tick_path} manually.")
        return False
    registration_path = state_dir / "runs" / tick_id / "registration.json"
    if not registration_path.is_file():
        print(f"Current tick {tick_id}: no registration (no TODO selected)")
        return True
    fix = f"Fix (tick {tick_id}):"
    try:
        registration = json.loads(registration_path.read_text(encoding="utf-8"))
        from .github_issues import SUPPORTED_REGISTRATION_SCHEMA_VERSIONS

        if registration.get("schema_version") not in SUPPORTED_REGISTRATION_SCHEMA_VERSIONS:
            print(
                f"REGISTRATION UNSUPPORTED: schema_version {registration.get('schema_version')}; "
                "finish or abandon this run before upgrading"
            )
            return False
        worktree = Path(registration["worktree"])
        actual: dict[str, str] = {"worktree": str(worktree.resolve())}
        branch = _cli_sp.run(
            ["git", "branch", "--show-current"],
            cwd=worktree,
            text=True,
            capture_output=True,
        )
        actual["branch"] = (
            branch.stdout.strip() if branch.returncode == 0 else "<unavailable>"
        )
        common = _cli_sp.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=worktree,
            text=True,
            capture_output=True,
        )
        actual["repository"] = (
            str(Path(common.stdout.strip()).resolve().parent)
            if common.returncode == 0
            else "<unavailable>"
        )
        base = _cli_sp.run(
            ["git", "rev-parse", f"{registration['base_sha']}^{{commit}}"],
            cwd=worktree,
            text=True,
            capture_output=True,
        )
        actual["base_sha"] = (
            base.stdout.strip() if base.returncode == 0 else "<unavailable>"
        )
        if registration.get("plan_source_kind", "legacy_path") == "embedded":
            from .run_registration import RunRegistrationError, _read_verified_artifact

            try:
                plan_bytes = _read_verified_artifact(
                    registration_path.parent / "plan.md", registration["plan_hash"]
                )
            except RunRegistrationError:
                actual["plan_hash"] = "<missing>"
            else:
                actual["plan_hash"] = hashlib.sha256(plan_bytes).hexdigest()
        else:
            plan_at_base = _cli_sp.run(
                ["git", "show", f"{registration['base_sha']}:{registration['plan_path']}"],
                cwd=worktree,
                capture_output=True,
            )
            plan_bytes = plan_at_base.stdout
            if isinstance(plan_bytes, str):
                plan_bytes = plan_bytes.encode()
            actual["plan_hash"] = (
                hashlib.sha256(plan_bytes).hexdigest()
                if plan_at_base.returncode == 0
                else "<missing>"
            )
        snapshot = registration.get("issue_snapshot")
        actual["selected_entry_hash"] = (
            hashlib.sha256(snapshot.encode()).hexdigest()
            if isinstance(snapshot, str)
            else "<missing>"
        )
        expected = {
            "repository": str(Path(registration["repository"]).resolve()),
            "worktree": str(Path(registration["worktree"]).resolve()),
            "branch": registration["branch"],
            "base_sha": registration["base_sha"],
            "plan_hash": registration["plan_hash"],
            "selected_entry_hash": registration["selected_entry_hash"],
        }
    except (
        OSError,
        UnicodeError,
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
    ):
        print("REGISTRATION DRIFT: active registration could not be inspected")
        print(f"{fix} preserve the run and repair {registration_path} manually.")
        return False

    print(
        "Registered authority: "
        + " ".join(
            f"{field} expected={expected[field]} actual={actual[field]}"
            for field in expected
        )
    )
    head = _cli_sp.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
    )
    status = _cli_sp.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=worktree,
        text=True,
        capture_output=True,
    )
    lifecycle_head = head.stdout.strip() if head.returncode == 0 else "<unavailable>"
    lifecycle_state = (
        "dirty" if status.returncode != 0 or status.stdout else "clean"
    )
    print(f"Current lifecycle: head_sha={lifecycle_head} worktree={lifecycle_state}")
    mismatches = [field for field in expected if expected[field] != actual[field]]
    for field in mismatches:
        print(
            f"REGISTRATION DRIFT: {field} expected={expected[field]} "
            f"actual={actual[field]}"
        )
    from .github_issues import check_issue_drift

    issue_state = check_issue_drift(project_dir, registration)
    if issue_state is None:
        print("Issue authority: pinned")
    elif issue_state == "issue_unavailable:registration_invalid":
        print("REGISTRATION DRIFT: registration is malformed")
        print(f"{fix} preserve the run and repair {registration_path} manually.")
        return False
    elif issue_state.startswith("issue_unavailable:"):
        print(f"WARNING: issue check unavailable ({issue_state.partition(':')[2]})")
        print(f"{fix} rerun `tpo doctor` once GitHub is reachable.")
        return False
    else:
        print(f"ISSUE DRIFT: {issue_state}")
        print(
            f"{fix} finish or abandon this run; the pinned issue changed "
            f"(touch {state_dir / 'runs' / tick_id / 'abandoned'} to give up)."
        )
        return False
    if mismatches:
        print(f"{fix} preserve the run and resolve registration drift manually.")
        return False
    return True


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


def _parse_iso_date_flag(value: str) -> str:
    """Parse --date as a calendar date in YYYY-MM-DD form."""
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--date must be YYYY-MM-DD (you provided '{value}')"
        )


def _parse_todo_id_flag(value: str) -> int:
    """Parse --todo argument, accepting 'TODO-N' or plain 'N' formats."""
    cleaned = value.removeprefix("TODO-").removeprefix("todo-")
    try:
        number = int(cleaned)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError(
            f"--todo must be a positive TODO id (you provided '{value}'). "
            f"Example: --todo TODO-5 or --todo 5"
        )
    return number


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
    from .contract import DEFAULT_PROFILE

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

    todos_parser = subparsers.add_parser("todos", help="GitHub issue completion")
    todos_subparsers = todos_parser.add_subparsers(dest="todos_command", required=True)
    create_parser = todos_subparsers.add_parser(
        "create", help="Create or resume a validated TODO with an embedded Plan"
    )
    create_parser.add_argument("project", help="Project name")
    create_parser.add_argument("--request-file", required=True, type=Path)
    create_parser.add_argument(
        "--approved-repo",
        default=None,
        help="Exact OWNER/REPO shown in the approved preview; required with --yes",
    )
    create_parser.add_argument("--issue", type=int, default=None, help="Resume this partial issue")
    create_parser.add_argument(
        "--yes", action="store_true", help="Use only after the displayed request was approved"
    )
    create_parser.set_defaults(func=_cmd_todos_create)
    complete_parser = todos_subparsers.add_parser(
        "complete", help="Close one delivered TODO issue after its pull request merged"
    )
    complete_parser.add_argument("project", help="Project name")
    complete_parser.add_argument(
        "--todo", required=True, type=_parse_todo_id_flag,
        help="Issue to close (e.g. TODO-5 or 5)",
    )
    complete_parser.add_argument("--pr", required=True, type=int, help="Merged PR number")
    complete_parser.add_argument(
        "--date", type=_parse_iso_date_flag, default=None,
        help="Completion date (YYYY-MM-DD); defaults to today (UTC)",
    )
    complete_parser.add_argument(
        "--force", action="store_true",
        help="Proceed although the PR is not merged, a run for the issue is still "
        "active, or the issue already records completion by another PR",
    )
    complete_parser.set_defaults(func=_cmd_todos_complete)

    labels_parser = todos_subparsers.add_parser(
        "labels", help="Manage the GitHub label vocabulary used by the pipeline"
    )
    labels_subparsers = labels_parser.add_subparsers(dest="labels_command", required=True)
    labels_sync_parser = labels_subparsers.add_parser(
        "sync", help="Create any missing pipeline labels in the project's GitHub repository"
    )
    labels_sync_parser.add_argument("project", help="Project name")
    labels_sync_parser.set_defaults(func=_cmd_todos_labels_sync)

    audit_parser = todos_subparsers.add_parser(
        "audit",
        help="Check TODO issue bodies against the backlog contract and their mirror labels",
    )
    audit_parser.add_argument("project", help="Project name")
    audit_parser.add_argument(
        "--todo", type=_parse_todo_id_flag, default=None,
        help="Audit one issue (e.g. TODO-5 or 5), open or closed",
    )
    audit_parser.add_argument(
        "--fix", action="store_true",
        help="Normalize mirror labels (priority/effort/phase/review) to match the body",
    )
    audit_parser.add_argument(
        "--dry-run", action="store_true",
        help="With --fix: print the label changes without applying them",
    )
    audit_parser.set_defaults(func=_cmd_todos_audit)

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
        default=DEFAULT_PROFILE,
        help=(
            "Pipeline skill-set profile (e.g., native-sdd, gstack, agent-skills). "
            f"Default: {DEFAULT_PROFILE}. Each profile defines a different set of "
            "phases and required capabilities."
        ),
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
        "--plan",
        help="Validate this repository-relative Plan candidate before TODO persistence",
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

    # test: Live integration test harness
    test_parser = subparsers.add_parser(
        "test",
        help="Run the live integration test harness against a sandbox GitHub repository",
        description="Live integration test harness against a sandbox GitHub repository.",
    )
    test_parser.add_argument(
        "--fixture",
        default="happy-path",
        help="Fixture name to use (default: happy-path)",
    )
    test_parser.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/NAME",
        help="Sandbox GitHub repository (default: TPO_HARNESS_REPO)",
    )
    test_parser.add_argument(
        "--init-sandbox",
        action="store_true",
        help="Seed the sandbox default branch once and exit (no harness run)",
    )
    test_parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"Bundled phase profile to test (default: {DEFAULT_PROFILE})",
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
        "--keep",
        action="store_true",
        help="Keep the workspace and the sandbox issue/PR/branch after the run for inspection",
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

    skills_parser = subparsers.add_parser(
        "skills", help="Install, uninstall, or recover bundled agent skills"
    )
    skills_subparsers = skills_parser.add_subparsers(
        dest="skills_command", required=True
    )

    skills_install = skills_subparsers.add_parser("install")
    skills_install.add_argument("skill", choices=["todo-manager"])
    skills_install.add_argument("--target", choices=["codex", "claude"], required=True)
    skills_install.add_argument("--scope", choices=["user", "project"], default="user")
    skills_install.add_argument("--reinstall", action="store_true")
    skills_install.set_defaults(func=_cmd_skills_install)

    skills_uninstall = skills_subparsers.add_parser("uninstall")
    skills_uninstall.add_argument("skill", choices=["todo-manager"])
    skills_uninstall.add_argument("--target", choices=["codex", "claude"], required=True)
    skills_uninstall.add_argument("--scope", choices=["user", "project"], default="user")
    skills_uninstall.add_argument("--yes", action="store_true")
    skills_uninstall.add_argument("--force", action="store_true")
    skills_uninstall.set_defaults(func=_cmd_skills_uninstall)

    skills_recover = skills_subparsers.add_parser("recover")
    skills_recover.add_argument("skill", choices=["todo-manager"])
    skills_recover.add_argument("--target", choices=["codex", "claude"], required=True)
    skills_recover.add_argument("--scope", choices=["user", "project"], default="user")
    recovery_mode = skills_recover.add_mutually_exclusive_group(required=True)
    recovery_mode.add_argument("--finish", action="store_true")
    recovery_mode.add_argument("--rollback", action="store_true")
    skills_recover.set_defaults(func=_cmd_skills_recover)

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
    from .project_config import _get_project_state_dir

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


# ULID-ish ids from ``new_tick_id`` (26 Crockford chars) and the 20-digit fallback
# from ``_generate_tick_id``; the id names a run directory, so nothing else passes.
_TICK_ID_RE = re.compile(r"[0-9A-Z]{2,26}")


def _read_prior_tick_id(state_dir: Path) -> str | None:
    """Read the prior tick_id from current_tick_id.txt.

    Returns None if the file doesn't exist (cold start).
    Raises OSError if the file exists but can't be read (e.g., permissions).
    """
    path = state_dir / CURRENT_TICK_ID_FILE
    if not path.exists():
        return None
    try:
        value = path.read_text().strip()
    except OSError as e:
        log.error("can't read %s: %s — aborting tick (prior state unreadable)", path, e)
        raise
    if not _TICK_ID_RE.fullmatch(value):
        log.error("%s holds a malformed tick id; treating as cold start", path)
        return None
    return value


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
    from .project_config import _discover_projects, _get_project_state_dir

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
        from .github_issues import GitHubIssuesError, repository_identity

        try:
            repository_identity(project_dir)
        except GitHubIssuesError as exc:
            log.error(
                "project %s: origin is not a github.com remote (%s)", args.project, exc.code
            )
            return 2
        from .project_config import _is_enabled, _read_project_toml

        if not _is_enabled(project_dir):
            log.error(
                "project is disabled: %s — remove .hermes/project.toml or set enabled = true",
                args.project,
            )
            return 2
        from .project_config import PIPELINE_TOML_PATH

        if not (project_dir / PIPELINE_TOML_PATH).is_file():
            log.warning(
                "project %s: no .hermes/pipeline.toml (run `tpo init %s`); "
                "using the default contract",
                args.project,
                args.project,
            )
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

    # --- Step 3: Fairness rotation, then per-project tick ---
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


# Tracker failures that an operator must fix; anything else is treated as transient.
_TRACKER_CONFIG_FAULT_CODES = frozenset({
    "gh_missing", "gh_auth", "gh_version", "gh_not_found", "git_unavailable",
    "origin_identity_invalid",
})


# Registration failures caused by the issue's own content (not infrastructure):
# the issue is demoted to needs-info so it stops being re-selected every tick.
# Codes that a later tick or a human may resolve without editing the issue
# (``authority_untracked``, ``branch_mismatch``, and ``authority_invalid``, which a
# repository rename redirect can trigger) never demote.
_CONTENT_REGISTRATION_CODES = frozenset({"plan_invalid", "branch_invalid", "branch_exists"})

_PLAN_TRACKED_TIMEOUT = 30.0


def _abandon_run_if_registered(state_dir: Path, tick_id: str, reason: str) -> bool:
    """Durably retire a pre-dispatch registration while preserving its evidence."""
    run_dir = state_dir / "runs" / tick_id
    if not (run_dir / "registration.json").is_file():
        return False
    from .state import _atomic_write_text

    _atomic_write_text(run_dir / "abandoned", reason + "\n")
    return True


def _plan_tracked_at_head(project_dir: Path, plan_path: str) -> bool:
    """True only when ``plan_path`` is committed at ``HEAD`` of ``project_dir``.

    Raises ``GitHubIssuesError("git_unavailable", ...)`` when git itself cannot
    run (missing binary, timeout): that is an operator fault, not a Plan fault.
    """
    from .github_issues import GitHubIssuesError

    try:
        result = _cli_sp.run(
            ["git", "cat-file", "-e", f"HEAD:{plan_path}"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PLAN_TRACKED_TIMEOUT,
        )
    except (OSError, _cli_sp.TimeoutExpired) as exc:
        raise GitHubIssuesError("git_unavailable", "git cat-file") from exc
    return result.returncode == 0


def _block_untracked_plans(project_dir: Path, eligibility):
    """Move candidates whose Plan is not tracked at HEAD to ``plan_invalid:untracked``.

    Registration would otherwise fail with ``authority_untracked`` every tick;
    an uncommitted Plan is a content fault the author fixes by committing it,
    so it is blocked before selection rather than demoted. A git outage
    propagates as ``git_unavailable`` (a tracker config fault for the tick).
    """
    from .github_issues import EligibilityResult, GitHubIssuesError

    if eligibility.candidates:
        try:
            head = _cli_sp.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=project_dir, capture_output=True, text=True, check=False,
                timeout=_PLAN_TRACKED_TIMEOUT,
            )
        except (OSError, _cli_sp.TimeoutExpired) as exc:
            raise GitHubIssuesError("git_unavailable", "git rev-parse") from exc
        if head.returncode != 0:
            # Not a repository or an unborn HEAD: the project, not its issues, is at fault.
            raise GitHubIssuesError("git_unavailable", "git rev-parse")
    kept = []
    blocked = dict(eligibility.blocked_reasons)
    for candidate in eligibility.candidates:
        if candidate.plan_path and not _plan_tracked_at_head(project_dir, candidate.plan_path):
            blocked[candidate.entry.todo_id] = "plan_invalid:untracked"
        else:
            kept.append(candidate)
    return EligibilityResult(tuple(kept), blocked)


def _demote_issue(project_dir: Path, issue, *, repo: str, project_slug: str, code: str) -> None:
    """Best-effort ``ready-for-agent`` -> ``needs-info`` after a content-caused failure."""
    from . import github_issues

    try:
        github_issues.remove_label(project_dir, issue.number, github_issues.READY_LABEL, repo=repo)
        github_issues.add_label(project_dir, issue.number, "needs-info", repo=repo)
    except github_issues.GitHubIssuesError as exc:
        log.warning(
            "project %s: could not demote #%d to needs-info after %s (%s)",
            project_slug, issue.number, code, exc.code,
        )
        return
    log.error(
        "project %s: #%d demoted to needs-info — registration failed with %s; "
        "fix the issue and re-add ready-for-agent",
        project_slug, issue.number, code,
    )


def _record_tracker_error(
    project_state: Path, tick_id: str, project_slug: str, exc, cb: CircuitBreaker
) -> None:
    """Persist a tracker_error no-pick and observe it on the circuit breaker."""
    from .decision import record_tracker_error

    config_fault = exc.code in _TRACKER_CONFIG_FAULT_CODES
    record_tracker_error(
        state_dir=project_state,
        tick_id=tick_id,
        project_slug=project_slug,
        code=exc.code,
        counts_as_no_progress=not config_fault,
    )
    cb.observe(picked=None, counts_as_no_progress=not config_fault)
    if config_fault:
        log.error(
            "project %s: GitHub Issues unavailable (%s) — fix `gh`/origin configuration",
            project_slug,
            exc.code,
        )
    else:
        log.warning(
            "project %s: GitHub Issues temporarily unavailable (%s)", project_slug, exc.code
        )


def _profile_deprecation_notices(contract, phase_profile, project_slug: str) -> list[str]:
    """Return deprecation notices for the contract's profile selection.

    Emits at most one notice: when ``pipeline.toml`` omits ``profile`` (the
    implicit legacy default is deprecated), or else when the resolved profile
    is itself marked ``deprecated`` in its phases.yaml. Informational only:
    callers must not change exit codes based on these.
    """
    from .contract import DEFAULT_PROFILE

    migrate = f"tpo init {project_slug} --force --profile {DEFAULT_PROFILE}"
    notices = []
    if not contract.profile_declared:
        notices.append(
            "pipeline.toml does not declare a profile; the implicit default "
            f"'{contract.profile}' is deprecated. Migrate with: {migrate}"
        )
    elif phase_profile.deprecated:
        notices.append(
            f"profile '{contract.profile}' is deprecated; migrate with: {migrate}"
        )
    return notices


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
        LEGACY_IMPLICIT_PROFILE,
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
        for notice in _profile_deprecation_notices(contract, phase_profile, project_slug):
            log.warning("project %s: %s", project_slug, notice)
    except ContractMissingError:
        # A contract-less project keeps resolving to the legacy implicit
        # profile — same as a contract that omits `profile` — regardless of
        # what `tpo init` now writes for new projects. Passed explicitly so a
        # change to PipelineContract.profile's default can't migrate an
        # existing project's phases underneath it.
        # Auto-compute capabilities from phases.yaml so a fresh project
        # doesn't break when a future phase requires a tool not in the
        # hardcoded DEFAULT_CAPABILITIES tuple.
        phases_path = resolve_profile_phases_path(LEGACY_IMPLICIT_PROFILE)
        phase_profile = load_phase_profile(phases_path)
        phases = list(phase_profile.phases)
        prerequisites = load_profile_prerequisites(LEGACY_IMPLICIT_PROFILE)
        contract = PipelineContract(
            schema_version=CONTRACT_SCHEMA_VERSION,
            assignee="pipeline",
            capabilities=tuple(sorted(required_capabilities(phases))),
            profile=LEGACY_IMPLICIT_PROFILE,
            # No contract declares nothing, so the profile is as implicit as it
            # gets: this is the population ADR-0004 most needs to reach with the
            # migration hint, and the default (declared) would silence it.
            profile_declared=False,
        )
        for notice in _profile_deprecation_notices(contract, phase_profile, project_slug):
            log.warning("project %s: %s", project_slug, notice)
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

    from . import github_issues
    from .github_issues import GitHubIssuesError

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

        if not pr_handoff_resolved:
            from .kanban_tasks import reconcile_plan_task_results
            from .result_contract import ResultContractError, sanitize_result_text
            from .review_reconciliation import reconcile_reviews
            from .run_registration import ensure_in_progress_label, registration_state
            from .todos_completion import reconcile_todo_completion

            run_dir = project_state / "runs" / prior_tick_id
            registration_path = run_dir / "registration.json"
            repo = None
            if registration_path.exists():
                try:
                    repo = github_issues.repository_identity(project_dir)
                except GitHubIssuesError as exc:
                    log.error(
                        "project %s: prior tick %s origin identity unavailable (%s); "
                        "delivery cannot be reconciled",
                        project_slug,
                        prior_tick_id,
                        exc.code,
                    )
                    cb.observe(picked=None, counts_as_no_progress=True)
                    return
            if registration_path.exists() and registration_state(run_dir) == "active":
                from .todos_completion import CLOSE_STARTED_MARKER

                try:
                    pinned = json.loads(registration_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    pinned = {}
                if not isinstance(pinned, dict):
                    pinned = {}
                number = pinned.get("issue_number")
                live = None
                try:
                    if type(number) is int and number > 0:
                        # Exactly one live read per resume tick; drift and the
                        # claim label are both evaluated from it.
                        live = github_issues.fetch_issue(project_dir, number, repo=repo)
                except GitHubIssuesError as exc:
                    drift = f"issue_unavailable:{exc.code}"
                else:
                    drift = github_issues.check_issue_drift(
                        project_dir, pinned, repo=repo, live=live
                    )
                if drift == "issue_closed" and (run_dir / CLOSE_STARTED_MARKER).exists():
                    # TPO itself began closing this issue; let the delivery
                    # reconciler finish (it never re-claims a closed issue).
                    log.info(
                        "project %s: prior tick %s closeout in progress; issue already closed",
                        project_slug,
                        prior_tick_id,
                    )
                    drift = None
                elif drift is None:
                    # The claim label is best-effort; re-add it only for a run
                    # whose issue is still the pinned, open, un-held issue.
                    ensure_in_progress_label(project_dir, pinned, repo=repo, live=live)
                if drift is not None and drift.startswith("issue_unavailable:"):
                    log.warning(
                        "project %s: prior tick %s pinned issue could not be verified (%s); "
                        "continuing reconciliation",
                        project_slug,
                        prior_tick_id,
                        drift,
                    )
                elif drift is not None:
                    from .todos_completion import flag_issue_drift

                    try:
                        flag_issue_drift(
                            project_dir=project_dir,
                            state_dir=project_state,
                            tenant=project_slug,
                            tick_id=prior_tick_id,
                            code=drift,
                            repo=repo,
                        )
                    except ResultContractError as exc:
                        log.error(
                            "project %s: prior tick %s registration cannot be validated: %s",
                            project_slug,
                            prior_tick_id,
                            sanitize_result_text(str(exc), maximum=1000),
                        )
                    except Exception as exc:
                        # The gate is best-effort; the block + no-progress below must run.
                        log.error(
                            "project %s: prior tick %s drift gate failed: error_type=%s",
                            project_slug,
                            prior_tick_id,
                            type(exc).__name__,
                        )
                    log.error(
                        "project %s: prior tick %s pinned issue drifted (%s); delivery blocked",
                        project_slug,
                        prior_tick_id,
                        drift,
                    )
                    cb.observe(picked=None, counts_as_no_progress=True)
                    return

            reconcilers = (
                ("result", reconcile_plan_task_results),
                ("review", reconcile_reviews),
                ("delivery", reconcile_todo_completion),
            )
            for label, reconcile in reconcilers:
                try:
                    reconciled = reconcile(
                        project_dir=project_dir,
                        state_dir=project_state,
                        tenant=project_slug,
                        tick_id=prior_tick_id,
                        repo=repo,
                    )
                except ResultContractError as exc:
                    log.error(
                        "project %s: prior tick %s registration cannot be validated: %s",
                        project_slug,
                        prior_tick_id,
                        sanitize_result_text(str(exc), maximum=1000),
                    )
                    cb.observe(picked=None, counts_as_no_progress=True)
                    return
                if not reconciled:
                    log.info(
                        "project %s: prior tick %s %s reconciliation is blocked, skipping",
                        project_slug,
                        prior_tick_id,
                        label,
                    )
                    cb.observe(picked=None, counts_as_no_progress=True)
                    return

        if not pr_handoff_resolved and not all_phases_complete(
            project_slug, prior_tick_id, state_dir=project_state
        ):
            from .kanban_tasks import get_todo_kanban_status

            if not get_todo_kanban_status(project_slug, prior_tick_id):
                # A persisted tick with no cards is a stall (crash before/during
                # card creation), not a legitimate in-flight skip.
                log.warning(
                    "project %s: prior tick %s has no kanban tasks; treating as a stall",
                    project_slug,
                    prior_tick_id,
                )
                cb.observe(picked=None, counts_as_no_progress=True)
            else:
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

    # Step 3: Build context & run selection. GitHub Issues are the sole TODO source.
    from .decision.context import build_in_flight, fetch_kanban_snapshot
    from .run_registration import active_registration_issue_numbers

    # Compile the exact candidate set server-side for every profile; the
    # selector only ever sees eligible issues and may only pick their ids.
    # The kanban snapshot is fetched once and shared with build_context; a
    # missing snapshot is not a tracker error, it only makes a claimed
    # (`tpo:in-progress`) issue unverifiable rather than stale.
    kanban_snapshot = fetch_kanban_snapshot(project_slug)
    kanban_available = kanban_snapshot is not None
    if kanban_snapshot is None:
        # Marker shared with build_context so nothing re-fetches the board.
        kanban_snapshot = {"columns": [], "_error": "kanban snapshot unavailable"}
    in_flight = build_in_flight(
        project_state,
        max_phase_timeout_min=cb_cfg.max_phase_timeout_min,
        board_slug=project_slug,
        snapshot=kanban_snapshot,
    )
    try:
        repo = github_issues.repository_identity(project_dir)
        issues = github_issues.list_todo_issues(project_dir, repo=repo)
    except GitHubIssuesError as exc:
        _record_tracker_error(project_state, tick_id, project_slug, exc, cb)
        return
    eligibility = github_issues.compile_eligible_issues(
        project_dir,
        issues,
        in_flight=set(in_flight),
        active_registration_ids=active_registration_issue_numbers(project_state),
        kanban_available=kanban_available,
        requires_plan=phase_profile.requires_plan,
    )
    if phase_profile.requires_plan:
        try:
            eligibility = _block_untracked_plans(project_dir, eligibility)
        except GitHubIssuesError as exc:
            _record_tracker_error(project_state, tick_id, project_slug, exc, cb)
            return
    ctx = build_context(
        tick_id=tick_id,
        state_dir=project_state,
        selection_markdown=eligibility.selection_markdown,
        candidate_ids=[c.entry.todo_id for c in eligibility.candidates],
        project_slug=project_slug,
        max_phase_timeout_min=cb_cfg.max_phase_timeout_min,
        snapshot=kanban_snapshot,
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

    if not eligibility.candidates:
        from .decision import record_no_candidates

        decision = record_no_candidates(
            tick_id=tick_id,
            ctx=ctx,
            cfg=full_cfg,
            blocked_reasons=eligibility.blocked_reasons,
        )
    else:
        decision = run_selection(
            tick_id=tick_id,
            ctx=ctx,
            cfg=full_cfg,
            timeout=selection_timeout_s,
            eligible_todo_ids=eligibility.todo_ids,
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

    # The selector may only return a compiled candidate; its Plan was already
    # validated (and its kind resolved) by compile_eligible_issues.
    selected = next(
        (candidate for candidate in eligibility.candidates if candidate.entry.todo_id == picked),
        None,
    )
    if selected is None:
        raise RuntimeError(f"selection returned a non-candidate id: {picked}")
    plan_path = selected.plan_path
    plan_source = selected.plan_source
    issue = selected.entry
    plan_reference = None
    if plan_source is not None and plan_source.kind == "legacy_path" and plan_source.plan_path:
        # A repository-path Plan is its own authority: the reference value is
        # the source's own validated path (see plan_manifest.validate_plan_reference).
        # The embedded kind is bound below, from the pinned registration instead.
        from .plan_manifest import PlanReference

        plan_reference = PlanReference(plan_source.plan_path, plan_source)

    # Step 4: Render every prompt before persisting the tick ID or mutating Hermes.
    from .kanban_tasks import (
        create_prepared_todo_phases,
        planned_phase_keys,
        prepare_todo_phases,
    )

    registration = None
    if phase_profile.requires_plan and plan_source is not None and plan_source.kind == "embedded":
        from .result_contract import ResultContractError, load_validated_registration
        from .run_registration import RunRegistrationError, register_pinned_run

        try:
            registration = register_pinned_run(
                project_dir=project_dir, state_dir=project_state, tick_id=tick_id,
                selected_issue=issue, plan_path=None, repo=repo, profile=contract.profile,
                prompt_client=config.prompt_client, assignee=contract.assignee,
                review_assignee=getattr(contract, "review_assignee", None),
                step_keys=planned_phase_keys(phases_path, plan_source),
            )
            validated = load_validated_registration(project_dir, project_state, tick_id, repo=repo)
            plan_source = validated.plan_source
            plan_reference = validated.plan_reference
        except (RunRegistrationError, ResultContractError) as exc:
            _abandon_run_if_registered(project_state, tick_id, "run_registration_failed")
            _record_failed_to_spawn(project_state, tick_id, picked, exc, reason="run_registration_failed")
            cb.observe(picked=None, counts_as_no_progress=True)
            code = getattr(exc, "code", "registration_invalid")
            log.error(
                "project %s: pinned embedded run registration failed: code=%s",
                project_slug, code,
            )
            if code in _CONTENT_REGISTRATION_CODES:
                _demote_issue(project_dir, issue, repo=repo, project_slug=project_slug, code=code)
            return

    log.info("project %s: selected %s, registering kanban phases", project_slug, picked)
    try:
        prepared = prepare_todo_phases(
            todo_id=picked,
            tick_id=tick_id,
            board_slug=project_slug,
            phases_path=phases_path,
            prompt_client=config.prompt_client,
            plan_path=plan_path,
            plan_source=plan_source,
            plan_reference=plan_reference,
            spec_path=issue.spec,
            reference_paths=issue.references,
            project_dir=project_dir,
            decisions=github_issues.issue_decisions(issue),
        )
    except Exception as exc:  # PhasePromptRenderError, path validation, manifest errors
        if registration is not None:
            _abandon_run_if_registered(
                project_state, tick_id, "phase_prompt_preparation_failed"
            )
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

    if registration is not None and tuple(phase.phase_key for phase in prepared) != registration.step_keys:
        _abandon_run_if_registered(project_state, tick_id, "phase_key_drift")
        _record_failed_to_spawn(
            project_state, tick_id, picked, RuntimeError("registered phase keys drifted"),
            reason="phase_prompt_preparation_failed",
        )
        cb.observe(picked=None, counts_as_no_progress=True)
        return

    if phase_profile.requires_plan and registration is None:
        from .run_registration import RunRegistrationError, register_pinned_run

        try:
            registration = register_pinned_run(
                project_dir=project_dir,
                state_dir=project_state,
                tick_id=tick_id,
                selected_issue=issue,
                plan_path=plan_path,
                repo=repo,
                profile=contract.profile,
                prompt_client=config.prompt_client,
                assignee=contract.assignee,
                review_assignee=getattr(contract, "review_assignee", None),
                step_keys=(phase.phase_key for phase in prepared),
            )
        except RunRegistrationError as exc:
            _record_failed_to_spawn(
                project_state,
                tick_id,
                picked,
                exc,
                reason="run_registration_failed",
            )
            cb.observe(picked=None, counts_as_no_progress=True)
            log.error(
                "project %s: pinned run registration failed: code=%s",
                project_slug,
                exc.code,
            )
            if exc.code in _CONTENT_REGISTRATION_CODES:
                _demote_issue(project_dir, issue, repo=repo, project_slug=project_slug, code=exc.code)
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
            project_dir=registration.worktree if registration else project_dir,
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

    # Step 7: Claim the issue for every profile: the label is the re-selection
    # guard between PR-open and merge. Closeout releases it for registered runs;
    # other profiles release it through `tpo todos complete`. Best-effort: Kanban
    # in-flight state and the registration are the hard guards; a missing label
    # is re-added next tick.
    if github_issues.IN_PROGRESS_LABEL not in issue.labels:
        try:
            github_issues.add_label(
                project_dir, issue.number, github_issues.IN_PROGRESS_LABEL, repo=repo
            )
        except GitHubIssuesError as exc:
            log.warning(
                "project %s: could not add %s to #%d (%s); continuing",
                project_slug,
                github_issues.IN_PROGRESS_LABEL,
                issue.number,
                exc.code,
            )

    # Observe circuit breaker
    cb.observe(picked=picked, counts_as_no_progress=False)


def _cmd_todos_create(args, config: Config) -> int:
    """Create or converge one transaction-marked embedded-Plan issue."""
    from . import github_issues
    from .project_config import _get_project_state_dir
    from .todos_create import (
        TodoCreateError,
        execute_create,
        load_create_request,
        render_create_preview,
        validate_create_input_path,
    )

    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None or (args.issue is not None and args.issue <= 0):
        print("Error: invalid project or issue", file=sys.stderr)
        return 2
    try:
        request = load_create_request(args.request_file)
        state_dir = _get_project_state_dir(project_dir)
        validate_create_input_path(
            args.request_file, state_dir, transaction_id=request.transaction_id
        )
    except TodoCreateError as exc:
        print(f"Error: {exc.code}", file=sys.stderr)
        return 2
    try:
        repository = github_issues.repository_identity(project_dir)
        print(
            render_create_preview(
                request,
                project=args.project,
                repository=repository,
                issue_number=args.issue,
            ),
            end="",
        )
    except (TodoCreateError, github_issues.GitHubIssuesError) as exc:
        print(f"Error: {exc.code}", file=sys.stderr)
        return 2
    if args.yes and args.approved_repo is None:
        print("Error: approved_repo_required", file=sys.stderr)
        return 2
    if args.approved_repo is not None and args.approved_repo != repository:
        print("Error: repository_drift", file=sys.stderr)
        return 1
    if not args.yes:
        try:
            confirmation = input("Type create to continue: ")
        except EOFError:
            confirmation = ""
        if confirmation != "create":
            print("Creation cancelled.")
            return 1
    try:
        number = execute_create(
            project_dir,
            state_dir,
            request,
            approved_repo=args.approved_repo or repository,
            issue_number=args.issue,
        )
    except (TodoCreateError, OSError) as exc:
        code = exc.code if isinstance(exc, TodoCreateError) else "create_io_error"
        print(f"Error: {code}", file=sys.stderr)
        return 1
    except Exception as exc:
        from .github_issues import GitHubIssuesError
        if not isinstance(exc, GitHubIssuesError):
            raise
        print(f"Error: {exc.code}", file=sys.stderr)
        return 1
    print(f"created: TODO-{number}")
    return 0


def _cmd_todos_complete(args, config: Config) -> int:
    """Manually run the idempotent issue-close state machine for a delivered TODO.

    Exit codes: 0 completed, 1 GitHub failure, 2 usage or refused (PR not merged,
    run still active; ``--force`` overrides), 3 pending (retry).
    """
    from .github_issues import GitHubIssuesError, repository_identity
    from .project_config import _get_project_state_dir
    from .result_contract import ResultContractError
    from .run_registration import active_runs_for_issue
    from .todos_completion import _pr_view, close_issue_for_delivery

    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2
    state_dir = _get_project_state_dir(project_dir)
    try:
        repo = repository_identity(project_dir)
        pr_url = f"https://github.com/{repo}/pull/{args.pr}"
        view = _pr_view(project_dir, pr_url)
        if view.get("state") != "MERGED" and not args.force:
            print(f"Error: {pr_url} is not merged (state {view.get('state')}); "
                  "use --force to close the issue anyway", file=sys.stderr)
            return 2
        ticks = active_runs_for_issue(state_dir, args.todo) if not args.force else ()
        if ticks:
            print(f"Error: run {', '.join(ticks)} is active for TODO-{args.todo}; "
                  "let the tick finish it or use --force", file=sys.stderr)
            return 2
        outcome = close_issue_for_delivery(
            project_dir=project_dir, state_dir=state_dir, tick_id="manual",
            issue_number=args.todo, pr_number=args.pr, pr_url=pr_url, repo=repo,
            date=args.date, force=args.force,
        )
    except (GitHubIssuesError, ResultContractError) as exc:
        print(f"Error: {exc.code}", file=sys.stderr)
        return 1
    if outcome == "pending":
        print("pending")
        return 3
    print("completed")
    return 0


def _cmd_todos_labels_sync(args, config: Config) -> int:
    """Create the missing pipeline label vocabulary in the project's repository.

    Exit codes: 0 synced (or already up to date), 1 GitHub failure, 2 unknown project.
    """
    from .github_issues import (
        LABEL_VOCABULARY,
        GitHubIssuesError,
        check_auth,
        ensure_labels,
        repository_identity,
    )

    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2
    total = len(LABEL_VOCABULARY)
    try:
        check_auth(project_dir)
        created = ensure_labels(project_dir, repo=repository_identity(project_dir))
    except GitHubIssuesError as exc:
        partial = tuple(getattr(exc, "created", ()))
        for name in partial:
            print(f"created: {name}")
        if exc.code == "gh_truncated":
            print("Error: gh_truncated (label list capped at 1000; sync manually)", file=sys.stderr)
        else:
            detail = f": {exc.detail}" if exc.detail else ""
            print(
                f"Error: {exc.code}{detail} ({len(partial)} of {total} labels created)",
                file=sys.stderr,
            )
        return 1
    if not created:
        print(f"labels up to date ({total} names present; color/description not compared)")
        return 0
    for name in created:
        print(f"created: {name}")
    return 0


_AUDIT_ISSUE_FORM = Path(".github") / "ISSUE_TEMPLATE" / "tpo-todo.yml"
_AUDIT_FORM_MAX_BYTES = 1_000_000
# Body decision -> mirror label prefix. Phase is handled via ``phase_label``.
_AUDIT_MIRROR_PREFIXES: dict[str, str] = {
    "Priority": "priority",
    "Effort": "effort",
    "Test Coverage": "test-coverage",
    "Security Review": "security-review",
    "UI Review": "ui-review",
}
_AUDIT_INFORMATIONAL = frozenset({"plan:missing", "plan:legacy_path", "state:closed"})
_AUDIT_FRAGMENT_MAX = 120


def _audit_phase_options(project_dir: Path) -> tuple[str, ...]:
    """Phase options from the project's issue form, else ``PHASE_OPTIONS``."""
    import yaml

    from .github_issues import PHASE_OPTIONS

    path = project_dir / _AUDIT_ISSUE_FORM
    try:
        if path.stat().st_size > _AUDIT_FORM_MAX_BYTES:
            return PHASE_OPTIONS
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError, MemoryError):
        return PHASE_OPTIONS
    if not isinstance(data, dict) or not isinstance(data.get("body"), list):
        return PHASE_OPTIONS
    for item in data["body"]:
        if not isinstance(item, dict) or item.get("type") != "dropdown":
            continue
        attributes = item.get("attributes")
        if not isinstance(attributes, dict) or attributes.get("label") != "Phase":
            continue
        options = attributes.get("options")
        if isinstance(options, list) and options and all(isinstance(o, str) for o in options):
            return tuple(options)
    return PHASE_OPTIONS


def _audit_default_branch(project_dir: Path) -> str | None:
    try:
        result = _cli_sp.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=project_dir, capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, _cli_sp.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().removeprefix("origin/")


def _audit_branch_valid(project_dir: Path, branch: str, cache: dict[str, bool]) -> bool:
    if branch in cache:
        return cache[branch]
    valid = False
    if not (branch.startswith("refs/") or branch.startswith("-")):
        try:
            result = _cli_sp.run(
                ["git", "check-ref-format", "--branch", branch],
                cwd=project_dir, capture_output=True, text=True, check=False, timeout=30,
            )
        except (OSError, _cli_sp.TimeoutExpired):
            result = None
        valid = result is not None and result.returncode == 0
    cache[branch] = valid
    return valid


def _audit_issue(
    project_dir: Path,
    issue,
    *,
    phase_options: tuple[str, ...],
    default_branch: str | None,
    branch_cache: dict[str, bool],
    require_todo_label: bool,
) -> tuple[list[str], list[str], list[str]]:
    """Return (findings, labels to add, labels to remove) for one issue.

    Mirror labels are only reconciled for decisions the body states exactly once
    with a value present in ``LABEL_VOCABULARY``; labels outside the mirror
    prefixes are never touched. Label names are compared case-insensitively
    (GitHub labels are); removals use the label's actual casing.
    """
    from .github_issues import (
        KNOWN_SECTIONS,
        LABEL_VOCABULARY,
        REQUIRED_SECTIONS,
        TODO_LABEL,
        first_lines,
        parse_issue_body,
        phase_label,
    )
    from .plan_manifest import (
        PlanManifestValidationError,
        TodoPlanValidationError,
        validate_plan_candidate,
    )
    from .result_contract import sanitize_result_text

    def safe(fragment: str) -> str:
        return sanitize_result_text(fragment, maximum=_AUDIT_FRAGMENT_MAX)

    findings: list[str] = []
    if issue.state == "closed":
        findings.append("state:closed")
    lower_labels = {name.lower() for name in issue.labels}
    if require_todo_label and TODO_LABEL not in lower_labels:
        findings.append("not-a-todo")
    sections = parse_issue_body(issue.body)
    findings.extend(f"missing-section:{name}" for name in REQUIRED_SECTIONS if name not in sections)
    findings.extend(
        f"duplicate-section:{name}" for name in KNOWN_SECTIONS if len(sections.get(name, ())) > 1
    )

    if issue.plan_error is not None:
        findings.append(f"plan:invalid:{issue.plan_error}")
    elif issue.plan_source is not None:
        pass
    elif not issue.plan_values:
        findings.append("plan:missing")
    elif len(issue.plan_values) > 1:
        findings.append("plan:duplicate")
    else:
        findings.append("plan:legacy_path")
        try:
            validate_plan_candidate(
                project_dir, issue.plan_values[0], expected_todo_id=issue.todo_id
            )
        except (TodoPlanValidationError, PlanManifestValidationError) as exc:
            findings.append(f"plan:invalid:{exc.code}")
        except (OSError, ValueError, UnicodeError):
            findings.append("plan:invalid:unreadable")

    if "Branch" in sections:
        if len(issue.branch_values) != 1 or not _audit_branch_valid(
            project_dir, issue.branch_values[0], branch_cache
        ):
            findings.append("branch:invalid")
        else:
            branch = issue.branch_values[0]
            if branch == default_branch or (default_branch is None and branch in ("main", "master")):
                findings.append("branch:default")

    vocabulary = {name.lower(): name for name, _color, _description in LABEL_VOCABULARY}
    expected: dict[str, str] = {}  # mirror prefix -> the one canonical label the body implies
    for name, prefix in _AUDIT_MIRROR_PREFIXES.items():
        values = first_lines(sections.get(name, ()))
        if len(values) != 1:
            continue
        canonical = vocabulary.get(f"{prefix}:{values[0]}".lower())
        if canonical is not None:
            expected[prefix] = canonical
        else:
            findings.append(f"decision:{name}:{safe(values[0])}")
    phases = first_lines(sections.get("Phase", ()))
    if len(phases) == 1:
        canonical = None
        if phases[0] in phase_options:
            try:
                canonical = vocabulary.get(phase_label(phases[0]).lower())
            except ValueError:
                canonical = None
        if canonical is not None:
            expected["phase"] = canonical
        else:
            findings.append(f"decision:Phase:{safe(phases[0])}")

    add: list[str] = []
    remove: list[str] = []
    for prefix, label in expected.items():
        present = [name for name in issue.labels if name.lower().startswith(f"{prefix}:")]
        if label.lower() not in {name.lower() for name in present}:
            add.append(label)
        remove.extend(name for name in present if name.lower() != label.lower())
    add.sort()
    remove.sort()
    findings.extend(f"label:missing:{safe(label)}" for label in add)
    findings.extend(f"label:extra:{safe(label)}" for label in remove)
    return findings, add, remove


def _cmd_todos_audit(args, config: Config) -> int:
    """Audit TODO issues against the backlog contract; ``--fix`` normalizes mirror labels.

    Exit codes: 0 no actionable finding (after ``--fix``: nothing left unfixed),
    1 actionable findings, skipped or failed fixes, or a GitHub failure,
    2 usage or unknown project.
    """
    from .github_issues import (
        GitHubIssuesError,
        add_label,
        fetch_issue,
        list_todo_issues,
        remove_label,
        repository_identity,
    )
    from .result_contract import sanitize_result_text

    if args.dry_run and not args.fix:
        print("Error: --dry-run requires --fix", file=sys.stderr)
        return 2
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2
    try:
        repo = repository_identity(project_dir)
        if args.todo is not None:
            issues = (fetch_issue(project_dir, args.todo, repo=repo),)
        else:
            issues = list_todo_issues(project_dir, repo=repo)
    except GitHubIssuesError as exc:
        detail = f": {exc.detail}" if exc.detail else ""
        print(f"Error: {exc.code}{detail}", file=sys.stderr)
        return 1

    phase_options = _audit_phase_options(project_dir)
    default_branch = _audit_default_branch(project_dir)
    branch_cache: dict[str, bool] = {}
    total_findings = 0
    actionable = 0
    fixable = 0
    fixes: list[tuple[int, list[str], list[str], bool]] = []
    for issue in sorted(issues, key=lambda item: item.number):
        findings, add, remove = _audit_issue(
            project_dir,
            issue,
            phase_options=phase_options,
            default_branch=default_branch,
            branch_cache=branch_cache,
            require_todo_label=args.todo is not None,
        )
        for finding in findings:
            print(f"{issue.todo_id}: {finding}")
        total_findings += len(findings)
        actionable += sum(1 for finding in findings if finding not in _AUDIT_INFORMATIONAL)
        fixable += len(add) + len(remove)
        if add or remove:
            skip = issue.state == "closed" or "not-a-todo" in findings
            fixes.append((issue.number, add, remove, skip))

    summary = f"audit: issues={len(issues)} findings={total_findings} fixable={fixable}"
    if not args.fix:
        print(summary)
        return 1 if actionable else 0

    prefix = "would fix" if args.dry_run else "fixed"
    applied = 0
    skipped = 0
    failed = False
    for number, add, remove, skip in fixes:
        if skip:
            skipped += 1
            continue
        try:
            for label in add:
                if not args.dry_run:
                    add_label(project_dir, number, label, repo=repo)
                    applied += 1
                print(f"{prefix} TODO-{number}: +{sanitize_result_text(label, maximum=_AUDIT_FRAGMENT_MAX)}")
            for label in remove:
                if not args.dry_run:
                    remove_label(project_dir, number, label, repo=repo)
                    applied += 1
                print(f"{prefix} TODO-{number}: -{sanitize_result_text(label, maximum=_AUDIT_FRAGMENT_MAX)}")
        except GitHubIssuesError as exc:
            print(f"unfixed TODO-{number}: {exc.code}")
            failed = True
    print(f"{summary} skipped={skipped} applied={applied}")
    if args.dry_run:
        return 1 if actionable else 0
    return 1 if (actionable > fixable) or skipped or failed or applied < fixable else 0

def _cmd_init(args, config: Config) -> int:
    """Handle 'init' subcommand — write the default pipeline execution contract."""
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    from .contract import (
        DEFAULT_PROFILE,
        PROFILE_NAME_RE,
        ContractSchemaError,
        contract_path,
        write_default_contract,
    )
    from .phases import load_phase_profile, resolve_profile_phases_path
    from .project_config import _get_project_state_dir

    profile = getattr(args, "profile", DEFAULT_PROFILE) or DEFAULT_PROFILE
    if not PROFILE_NAME_RE.match(profile):
        msg = (
            f"invalid profile {profile!r}: must be a lowercase alphanumeric/hyphen "
            "string, 1-64 chars"
        )
        log.error(msg)
        print(f"ERROR: {msg}")
        return 2
    try:
        phase_profile = load_phase_profile(resolve_profile_phases_path(profile))
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
                LEGACY_IMPLICIT_PROFILE,
                PipelineContract,
                _render_contract_toml,
            )

            # Re-rendering an existing contract: an absent `profile` key means
            # the legacy implicit profile, never the new authoring default.
            contract = PipelineContract(
                schema_version=data["schema_version"],
                assignee=assignee,
                capabilities=tuple(
                    data.get("capabilities", list(DEFAULT_CAPABILITIES))
                ),
                profile=data.get("profile", LEGACY_IMPLICIT_PROFILE),
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
    if written and phase_profile.deprecated:
        print(
            f"note: profile '{profile}' is deprecated; new projects default to "
            f"'{DEFAULT_PROFILE}' (see docs/howto-native-sdd-profile.md)"
        )
    return 0


def _cmd_plan_validate(args, config: Config) -> int:
    """Validate the Plan attachment and optional manifest for one TODO."""
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2
    todo_id = f"TODO-{args.todo}"
    from .github_issues import (
        GitHubIssuesError,
        fetch_issue,
        repository_identity,
        resolve_plan_source,
    )
    from .plan_manifest import (
        PlanManifestValidationError,
        TodoPlanValidationError,
        validate_plan_candidate,
    )

    closed_note = ""
    try:
        relative_plan = getattr(args, "plan", None)
        if relative_plan is None:
            issue = fetch_issue(
                project_dir, args.todo, repo=repository_identity(project_dir)
            )
            todo_id = issue.todo_id
            if issue.state != "open":
                closed_note = f"; warning: issue is closed ({issue.state_reason or 'unknown'})"
            source = resolve_plan_source(project_dir, issue)
            manifest = source.manifest
        else:
            # ``--plan`` intentionally remains the legacy filesystem-candidate path.
            manifest = validate_plan_candidate(
                project_dir,
                relative_plan,
                expected_todo_id=todo_id,
            )
    except GitHubIssuesError as exc:
        print(f"Plan validation failed for {todo_id}: {exc.code}")
        return 1
    except TodoPlanValidationError as exc:
        prefix = (
            "plan_invalid:"
            if getattr(args, "plan", None) is None and exc.code in {"missing", "duplicate"}
            else "attachment_"
        )
        print(f"Plan validation failed for {todo_id}: {prefix}{exc.code}{closed_note}")
        return 1
    except PlanManifestValidationError as exc:
        print(f"Plan validation failed for {todo_id}: {exc.code}{closed_note}")
        return 1
    except ValueError:
        # e.g. an embedded NUL in the candidate path (raised by Path.resolve)
        print(f"Plan validation failed for {todo_id}: attachment_unreadable{closed_note}")
        return 1
    except (OSError, UnicodeError):
        print(f"Plan validation failed for {todo_id}: unreadable{closed_note}")
        return 1

    if manifest is None:
        if args.require_manifest:
            print(
                f"Plan validation failed for {todo_id}: --require-manifest requires "
                f"a tpo-plan block{closed_note}"
            )
            return 1
        print(
            f"Plan is valid legacy Markdown for {todo_id}; warning: no tpo-plan manifest"
            f"{closed_note}"
        )
        return 0
    suffix = "task" if len(manifest.tasks) == 1 else "tasks"
    print(f"Plan has a valid manifest for {todo_id}: {len(manifest.tasks)} {suffix}{closed_note}")
    return 0


def _cmd_doctor(args, config: Config) -> int:
    """Handle 'doctor' subcommand — verify the pipeline execution contract.

    Exit codes: 0 clean; 1 drift (capability mismatch, registration or issue
    drift) or any ``WARNING:``/``INVALID:`` line from the GitHub checks (auth,
    repository identity, label vocabulary, plan readiness, runs); 2 missing or
    INVALID contract, unknown project, or missing profile. ``DEPRECATED:``
    lines (undeclared or deprecated profile) are informational and never
    change the exit code.
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
    from .project_config import _get_project_state_dir

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
    from .phases import (
        load_phase_profile,
        load_profile_prerequisites,
        resolve_profile_phases_path,
    )

    try:
        profile_path = resolve_profile_phases_path(contract.profile)
        phase_profile = load_phase_profile(profile_path)
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

    for notice in _profile_deprecation_notices(contract, phase_profile, args.project):
        print(f"DEPRECATED: {notice}")

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
        "selection is deferred to issue #67 (legacy TODO-42)."
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

    version_ok, version_detail = _doctor_hermes_version()
    if not version_ok:
        print(f"UNSUPPORTED: {version_detail}")
        print("Fix: install Hermes >= 0.19.0 and rerun `tpo doctor`.")
        return 2
    print(f"Hermes version: {version_detail} (minimum 0.19.0)")

    github_ok = _doctor_github_checks(
        project_dir,
        project_state,
        project=args.project,
        requires_plan=phase_profile.requires_plan,
    )

    if not _doctor_active_registration(project_dir, project_state) or not github_ok:
        return 1

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
    """Handle 'test' subcommand — live integration test harness against a sandbox repo."""
    from . import harness
    from .github_issues import GitHubIssuesError
    from .harness import (
        HarnessCleanupError,
        HarnessPreflightError,
        HarnessProfileError,
        HarnessRemoteCleanupError,
    )

    if getattr(args, "init_sandbox", False):
        try:
            sandbox = harness.resolve_sandbox_repo(args.repo)
            root = harness._harness_tmp_root()
            root.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix="harness-init-", dir=root))
            try:
                outcome = harness.init_sandbox(sandbox, workspace)
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
            print(f"sandbox {sandbox.repo}: {outcome}")
            return 0
        except HarnessPreflightError as e:
            log.error("sandbox init preflight failed: code=%s detail=%s", e.code, e.detail)
            return 2
        except HarnessRemoteCleanupError as e:
            log.error("sandbox init failed: code=%s detail=%s", e.code, e.detail)
            return 2
        except GitHubIssuesError as e:
            log.error("sandbox init failed: gh error: %s", e)
            return 2
        except Exception as e:
            log.error(
                "sandbox init failed: error_type=%s message=%s", type(e).__name__, e
            )
            return 2

    try:
        result = harness.run_harness(
            fixture_name=args.fixture,
            repo=args.repo,
            loop=args.loop,
            keep_dir=args.keep,
            timeout=args.timeout,
            convergence_threshold=args.convergence_threshold,
            config=config,
            profile_name=args.profile,
        )
    except HarnessProfileError as e:
        log.error(
            "test harness profile setup failed: code=%s profile=%s detail=%s",
            e.code,
            e.profile_name,
            e.detail,
        )
        return 2
    except HarnessPreflightError as e:
        log.error("test harness preflight failed: code=%s detail=%s", e.code, e.detail)
        return 2
    except HarnessRemoteCleanupError as e:
        log.error("test harness cleanup incomplete: code=%s detail=%s", e.code, e.detail)
        return 2
    except HarnessCleanupError as e:
        log.error("test harness cleanup failed: %s", e)
        for note in getattr(e, "__notes__", ()):
            log.error("test harness cleanup: %s", note)
        return 2
    except Exception as e:
        log.error("test harness failed: error_type=%s message=%s", type(e).__name__, e)
        return 2

    print(result.summary)
    for leftover in result.cleanup_leftovers:
        print(f"leftover: {leftover}")
    return result.exit_code


def _cmd_skills_install(args, config: Config | None) -> int:
    from .skill_installer import install

    return install(
        args.skill,
        target=args.target,
        scope=args.scope,
        reinstall=args.reinstall,
    )


def _cmd_skills_uninstall(args, config: Config | None) -> int:
    from .skill_installer import uninstall

    return uninstall(
        args.skill,
        target=args.target,
        scope=args.scope,
        yes=args.yes,
        force=args.force,
    )


def _cmd_skills_recover(args, config: Config | None) -> int:
    from .skill_installer import recover

    return recover(
        args.skill,
        target=args.target,
        scope=args.scope,
        finish=args.finish,
        rollback=args.rollback,
    )
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

    # `tpo config` runs before pipeline runtime config exists (state dir,
    # projects dir) — skip Config.from_env() so it works even when that env
    # isn't configured yet.
    if getattr(args, "command", None) in {"config", "skills"}:
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
