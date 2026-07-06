"""Ship-gate domain logic: the deterministic merge-to-main path.

A completed TODO is held in-flight by a blocked `phase_9_ship` kanban task.
`maybe_ship_ready` detects that state, records a sidecar, and alerts once.
`approve_ship` runs an all-deterministic guard set, bumps the version inside
the PR, squash-merges, and completes the gate task.
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import slack
from .kanban_tasks import BLOCKED, COMPLETION_STATUSES, KanbanTaskInfo, get_todo_kanban_tasks

log = logging.getLogger(__name__)

SHIP_SIDECAR_SUFFIX = "-ship.json"

GATE_PHASE_KEY = "phase_9_ship"
PHASE_8_KEY = "phase_8_finish_branch"


@dataclass
class ShipSidecar:
    tick_id: str
    todo_id: int
    pr_number: int
    pr_head_sha: str
    base_branch: str
    work_branch: str
    phase_8_task_id: str | None = None
    bump_version: str | None = None


def _outcomes_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "outcomes"


def _sidecar_path(state_dir: Path | str, tick_id: str) -> Path:
    return _outcomes_dir(state_dir) / f"{tick_id}{SHIP_SIDECAR_SUFFIX}"


def write_sidecar(sidecar: ShipSidecar, *, state_dir: Path | str) -> Path:
    """Atomically write the ship sidecar (temp file + os.rename)."""
    target = _sidecar_path(state_dir, sidecar.tick_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(sidecar), sort_keys=True))
    os.rename(tmp, target)
    return target


def read_sidecar(state_dir: Path | str, tick_id: str) -> ShipSidecar | None:
    path = _sidecar_path(state_dir, tick_id)
    if not path.exists():
        return None
    try:
        return ShipSidecar(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("corrupt ship sidecar %s: %s", path, e)
        return None


def find_ship_sidecar(state_dir: Path | str, todo_id: int) -> ShipSidecar | None:
    out_dir = _outcomes_dir(state_dir)
    if not out_dir.exists():
        return None
    matches: list[ShipSidecar] = []
    for path in sorted(out_dir.glob(f"*{SHIP_SIDECAR_SUFFIX}")):
        try:
            sc = ShipSidecar(**json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError):
            continue
        if sc.todo_id == todo_id:
            matches.append(sc)
    return matches[-1] if matches else None


def delete_sidecar(state_dir: Path | str, tick_id: str) -> None:
    try:
        _sidecar_path(state_dir, tick_id).unlink()
    except FileNotFoundError:
        pass


def resolve_ship_task(*, project_slug: str, tick_id: str) -> KanbanTaskInfo | None:
    """Return the ship-gate KanbanTaskInfo for a tick, or None if absent."""
    tasks = get_todo_kanban_tasks(project_slug, tick_id)
    return tasks.get(GATE_PHASE_KEY)


class ApproveRefused(Exception):
    """A deterministic guard refused the approve. Not an internal error."""


@contextmanager
def approve_lock(state_dir: Path | str):
    """Serialize approve via a non-blocking exclusive flock.

    Uses a dedicated <state_dir>/approve.lock — NOT TickLock, whose
    stale-reclamation is wrong for a human-driven, possibly-slow merge.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "approve.lock"
    f = open(lock_path, "w")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ApproveRefused("another approve is already in progress")
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


GH_TIMEOUT = 60
GIT_TIMEOUT = 60
HERMES_TIMEOUT = 60  # timeout for `hermes` subprocess calls (kanban, etc.)
_GH_PR_VIEW_FIELDS = "number,state,headRefOid,baseRefName,headRefName,statusCheckRollup"


class ShipError(Exception):
    """An unexpected gh/git failure during the ship transaction."""


def gh_pr_view(branch: str, *, cwd: Path | str) -> dict:
    result = subprocess.run(
        ["gh", "pr", "view", branch, "--json", _GH_PR_VIEW_FIELDS],
        cwd=str(cwd), capture_output=True, text=True, timeout=GH_TIMEOUT,
    )
    if result.returncode != 0:
        raise ShipError(f"gh pr view {branch} failed: {result.stderr.strip()[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ShipError(f"gh pr view returned non-JSON: {e}")


def gh_pr_merge_squash(branch: str, *, match_head: str, cwd: Path | str) -> None:
    result = subprocess.run(
        ["gh", "pr", "merge", branch, "--squash", "--match-head-commit", match_head],
        cwd=str(cwd), capture_output=True, text=True, timeout=GH_TIMEOUT,
    )
    if result.returncode != 0:
        raise ShipError(f"gh pr merge {branch} failed: {result.stderr.strip()[:200]}")


def git_tree_clean(cwd: Path | str) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(cwd), capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def ci_is_green(checks: list) -> bool:
    """True if every status-rollup entry is a success.

    An empty list means no CI checks are configured for the repo — treated as
    green so approve does not deadlock on repos without required checks.
    Handles both CheckRun ({status, conclusion}) and StatusContext ({state}).
    """
    if not checks:
        return True
    for c in checks:
        state = (c.get("state") or "").upper()
        if state:  # StatusContext
            if state != "SUCCESS":
                return False
            continue
        status = (c.get("status") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        if status != "COMPLETED":
            return False
        if conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            return False
    return True


def _run_git(args: list[str], *, cwd: Path | str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    if result.returncode != 0:
        raise ShipError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def bump_in_pr(*, project_dir: Path | str, work_branch: str, todo_id: int) -> tuple[str, str]:
    """Bump VERSION/pyproject/CHANGELOG on work_branch, commit, push.

    Returns (new_version, new_head_sha). The pushed commit becomes part of the
    squash merge, so the caller MUST re-baseline the sidecar's pr_head_sha to
    the returned sha.

    The original branch is saved and restored so a crash (CI-red refusal,
    merge failure) never leaves the operator on the wrong branch.
    """
    from .merge import make_default_bump_fn

    project_dir = Path(project_dir)
    new_version, _label = make_default_bump_fn(project_dir)(None)

    orig_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_dir)
    try:
        _run_git(["checkout", work_branch], cwd=project_dir)

        (project_dir / "VERSION").write_text(f"{new_version}\n")

        pyproject = project_dir / "pyproject.toml"
        text = pyproject.read_text()
        new_text = re.sub(
            r'(?m)^version = "[^"]*"',
            f'version = "{new_version}"',
            text,
            count=1,
        )
        pyproject.write_text(new_text)

        changelog = project_dir / "CHANGELOG.md"
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        entry = (
            f"\n## [{new_version}] - {timestamp}\n"
            f"- Ship TODO-{todo_id}: bump to {new_version}\n"
        )
        if changelog.exists():
            changelog.write_text(entry + "\n" + changelog.read_text())
        else:
            changelog.write_text(f"# Changelog\n{entry}")

        _run_git(["add", "VERSION", "pyproject.toml", "CHANGELOG.md"], cwd=project_dir)
        _run_git(
            ["commit", "-m", f"chore: bump to {new_version} for TODO-{todo_id}"],
            cwd=project_dir,
        )
        _run_git(["push", "origin", work_branch], cwd=project_dir)

        new_sha = _run_git(["rev-parse", "HEAD"], cwd=project_dir)
    finally:
        # Best-effort cleanup: a failed restore must never mask the original
        # exception (CI-red refusal, push/merge failure) from the try body.
        try:
            _run_git(["checkout", orig_branch], cwd=project_dir)
        except ShipError as restore_err:
            log.warning(
                "bump_in_pr: failed to restore branch %s: %s",
                orig_branch, restore_err,
            )
    return new_version, new_sha


def _audit(state_dir: Path | str, message: str) -> None:
    """Append a timestamped audit line; also log at WARNING."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    line = f"{ts} {message}\n"
    with open(state_dir / "approve_audit.log", "a") as f:
        f.write(line)
    log.warning("AUDIT: %s", message)


def _check_ship_guards(
    *,
    sidecar: ShipSidecar,
    live_head_sha: str,
    project_dir: Path | str,
    state_dir: Path | str,
    force_count: int,
) -> None:
    """Run the deterministic pre-merge guards.

    - Dirty tree: ALWAYS refuses (force can never bypass).
    - SHA staleness: refuses unless force_count >= 2 (audited). Skipped once
      the bump has re-baselined the SHA (sidecar.bump_version set).
    """
    if not git_tree_clean(project_dir):
        raise ApproveRefused(
            f"working tree is dirty in {project_dir}; commit or clean before approving"
        )

    if sidecar.bump_version is not None:
        return  # SHA already re-baselined by the bump commit.

    if live_head_sha != sidecar.pr_head_sha:
        if force_count >= 2:
            _audit(
                state_dir,
                f"force-bypass SHA guard for TODO-{sidecar.todo_id}: "
                f"reviewed={sidecar.pr_head_sha} live={live_head_sha}",
            )
            return
        raise ApproveRefused(
            f"PR head SHA changed since review "
            f"(reviewed={sidecar.pr_head_sha}, live={live_head_sha}); "
            f"re-review, or pass --force --force to override"
        )


def _bump_and_merge(
    *,
    sidecar: ShipSidecar,
    project_dir: Path | str,
    state_dir: Path | str,
) -> None:
    """Bump-in-PR (once), gate on CI, then squash-merge at the exact SHA."""
    if sidecar.bump_version is None:
        new_version, new_sha = bump_in_pr(
            project_dir=project_dir,
            work_branch=sidecar.work_branch,
            todo_id=sidecar.todo_id,
        )
        # Re-baseline: the bump commit invalidated the reviewed SHA. Persist so
        # a retry (after CI passes) skips the bump and merges this exact SHA.
        sidecar.bump_version = new_version
        sidecar.pr_head_sha = new_sha
        write_sidecar(sidecar, state_dir=state_dir)

    view = gh_pr_view(sidecar.work_branch, cwd=project_dir)
    checks = view.get("statusCheckRollup") or []
    if not checks:
        log.warning(
            "no CI checks found for %s; proceeding (nothing to gate on)",
            sidecar.work_branch,
        )
    if not ci_is_green(checks):
        raise ApproveRefused(
            "CI is not green yet; re-run approve once checks pass "
            "(the bump commit is already pushed)"
        )

    gh_pr_merge_squash(
        sidecar.work_branch, match_head=sidecar.pr_head_sha, cwd=project_dir
    )


def complete_gate_task(task_id: str) -> None:
    """Complete the gate task in kanban so the tick can advance."""
    result = subprocess.run(
        ["hermes", "kanban", "complete", task_id],
        capture_output=True, text=True, timeout=HERMES_TIMEOUT,
    )
    if result.returncode != 0:
        raise ShipError(
            f"hermes kanban complete {task_id} failed: {result.stderr.strip()[:200]}"
        )


def approve_ship(
    *,
    project_dir: Path | str,
    project_slug: str,
    todo_id: int,
    state_dir: Path | str,
    force_count: int = 0,
) -> str:
    """Deterministically ship an approved TODO. Returns a success summary.

    Raises ApproveRefused on any guard refusal, ShipError on subprocess failure.
    """
    with approve_lock(state_dir):
        sidecar = find_ship_sidecar(state_dir, todo_id)
        if sidecar is None:
            raise ApproveRefused(
                f"no pending ship for TODO-{todo_id} "
                f"(not ready, or already shipped)"
            )

        gate = resolve_ship_task(project_slug=project_slug, tick_id=sidecar.tick_id)
        if gate is None:
            raise ApproveRefused(
                f"no gate task found for tick {sidecar.tick_id}; cannot ship"
            )

        view = gh_pr_view(sidecar.work_branch, cwd=project_dir)

        # Idempotency: if the PR is already merged (e.g. a crash after merge
        # but before completing the gate), just finish the gate and clean up.
        if (view.get("state") or "").upper() == "MERGED":
            complete_gate_task(gate.task_id)
            delete_sidecar(state_dir, sidecar.tick_id)
            return f"TODO-{todo_id} PR already merged; gate completed."

        _check_ship_guards(
            sidecar=sidecar,
            live_head_sha=view.get("headRefOid", ""),
            project_dir=project_dir,
            state_dir=state_dir,
            force_count=force_count,
        )

        _bump_and_merge(sidecar=sidecar, project_dir=project_dir, state_dir=state_dir)

        complete_gate_task(gate.task_id)
        delete_sidecar(state_dir, sidecar.tick_id)
        return (
            f"Shipped TODO-{todo_id}: merged {sidecar.work_branch} to "
            f"{sidecar.base_branch} (v{sidecar.bump_version}); gate completed."
        )


def maybe_ship_ready(
    *,
    project_dir: Path | str,
    project_slug: str,
    prior_tick_id: str,
    state_dir: Path | str,
    slack_channel: str,
) -> None:
    """Detect a ship-ready TODO, record a sidecar, and alert once.

    Best-effort: any failure is logged and swallowed so the tick continues.
    MUST be called before _tick_project's all_phases_complete early-return,
    because a blocked gate makes all_phases_complete return False.
    """
    try:
        if read_sidecar(state_dir, prior_tick_id) is not None:
            return  # already detected + alerted for this tick

        tasks = get_todo_kanban_tasks(project_slug, prior_tick_id)
        gate = tasks.get(GATE_PHASE_KEY)
        if gate is None or gate.status != BLOCKED:
            return  # no gate, or gate already moved past blocked

        non_gate = [t for k, t in tasks.items() if k != GATE_PHASE_KEY]
        if not non_gate or any(t.status not in COMPLETION_STATUSES for t in non_gate):
            return  # real work still in flight

        branch_file = Path(state_dir) / "pipeline_branch.txt"
        if not branch_file.exists():
            log.warning("ship-ready but no pipeline_branch.txt at %s", branch_file)
            return
        work_branch = branch_file.read_text().strip()
        if not work_branch:
            return

        view = gh_pr_view(work_branch, cwd=project_dir)
        todo_num = int(gate.todo_id.removeprefix("TODO-"))
        sidecar = ShipSidecar(
            tick_id=prior_tick_id,
            todo_id=todo_num,
            pr_number=int(view.get("number", 0)),
            pr_head_sha=view.get("headRefOid", ""),
            base_branch=view.get("baseRefName", "main"),
            work_branch=work_branch,
            phase_8_task_id=(
                tasks[PHASE_8_KEY].task_id
                if PHASE_8_KEY in tasks else None
            ),
            bump_version=None,
        )
        write_sidecar(sidecar, state_dir=state_dir)

        slack.notify(
            slack_channel,
            f":rocket: {project_slug} TODO-{todo_num} is ready to ship — "
            f"PR #{sidecar.pr_number} passed all phases. "
            f"Run: pipeline-watch approve {project_slug} --todo TODO-{todo_num}",
        )
    except Exception as e:  # never break the tick
        log.warning("maybe_ship_ready failed for %s tick %s: %s",
                    project_slug, prior_tick_id, e)
