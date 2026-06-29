"""Ship-gate domain logic: the deterministic merge-to-main path.

A completed TODO is held in-flight by a blocked `phase_9_ship` kanban task.
`maybe_ship_ready` detects that state, records a sidecar, and alerts once.
`approve_ship` runs an all-deterministic guard set, bumps the version inside
the PR, squash-merges, and completes the gate task.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SHIP_SIDECAR_SUFFIX = "-ship.json"


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

GH_TIMEOUT = 60
GIT_TIMEOUT = 60
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
