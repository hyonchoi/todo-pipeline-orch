"""Immutable run registration and conservative linked-worktree creation."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from . import github_issues
from .github_issues import (
    IN_PROGRESS_LABEL,
    MAX_ISSUE_SNAPSHOT_CHARS,
    REGISTRATION_SCHEMA_VERSION,
    GitHubIssuesError,
    IssueTodo,
    canonical_issue_snapshot,
    snapshot_hash,
)
from .state import _atomic_write_text

log = logging.getLogger(__name__)
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")
# Run dirs already reported as malformed in this process (WARNING once, DEBUG after).
_WARNED_RUN_DIRS: set[Path] = set()


def _warn_once(run_dir: Path, message: str, *args: object) -> None:
    if run_dir in _WARNED_RUN_DIRS:
        log.debug(message, *args)
        return
    _WARNED_RUN_DIRS.add(run_dir)
    log.warning(message, *args)


class RunRegistrationError(RuntimeError):
    """Pinned run authority or linked-worktree state is unsafe."""

    def __init__(self, code: str, detail: str = ""):
        message = f"{code}: {detail}" if detail else code
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RunRegistration:
    schema_version: int
    tick_id: str
    todo_id: str
    repository: Path
    base_sha: str
    issue_number: int
    issue_url: str
    issue_snapshot: str
    selected_entry_hash: str
    plan_path: str
    plan_hash: str
    branch: str
    worktree: Path
    profile: str
    prompt_client: str
    assignee: str
    review_assignee: str | None
    step_keys: tuple[str, ...]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise RunRegistrationError("git_error", f"git {args[0]} failed")
    return result


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _repository_root(project_dir: Path) -> Path:
    common = _git(
        project_dir, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).stdout.strip()
    common_path = Path(common).resolve()
    if common_path.name != ".git":
        raise RunRegistrationError("repository_invalid", "unsupported git directory")
    return common_path.parent


def _tracked_bytes(
    project_dir: Path,
    base_sha: str,
    relative_path: str,
    *,
    require_unchanged: bool = True,
) -> bytes:
    check = _git(project_dir, "cat-file", "-e", f"{base_sha}:{relative_path}", check=False)
    if check.returncode != 0:
        raise RunRegistrationError("authority_untracked", relative_path)
    if require_unchanged:
        diff = _git(
            project_dir, "diff", "--quiet", base_sha, "--", relative_path, check=False
        )
        if diff.returncode != 0:
            raise RunRegistrationError("authority_drift", relative_path)
    result = subprocess.run(
        ["git", "show", f"{base_sha}:{relative_path}"],
        cwd=project_dir,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RunRegistrationError("authority_untracked", relative_path)
    return result.stdout


def _default_branch(project_dir: Path) -> str | None:
    result = _git(
        project_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().removeprefix("origin/")


def _ref_exists(project_dir: Path, ref: str) -> bool:
    return _git(project_dir, "show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def _branch_registered_for_issue(state_dir: Path, number: int, branch: str) -> bool:
    """True when an earlier registration of the same issue already pinned ``branch``."""
    for path in (state_dir / "runs").glob("*/registration.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, Mapping)
            and payload.get("issue_number") == number
            and payload.get("branch") == branch
        ):
            return True
    return False


def _validate_branch(project_dir: Path, issue: IssueTodo, *, state_dir: Path) -> str:
    if len(issue.branch_values) != 1 or not issue.branch_values[0]:
        raise RunRegistrationError("branch_invalid", "exactly one Branch is required")
    branch = issue.branch_values[0]
    result = _git(project_dir, "check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0 or branch.startswith("refs/") or branch.startswith("-"):
        raise RunRegistrationError("branch_invalid", "unsafe Branch value")
    default = _default_branch(project_dir)
    if branch == default or (default is None and branch in ("main", "master")):
        raise RunRegistrationError("branch_invalid", "default_branch")
    exists = _ref_exists(project_dir, f"refs/heads/{branch}") or _ref_exists(
        project_dir, f"refs/remotes/origin/{branch}"
    )
    if exists and not _branch_registered_for_issue(state_dir, issue.number, branch):
        raise RunRegistrationError("branch_exists", branch)
    return branch


def _slug(issue: IssueTodo) -> str:
    title = _SLUG_UNSAFE_RE.sub("-", issue.title.lower()).strip("-")
    todo = issue.todo_id.lower()
    return f"{todo}-{title}"[:100].rstrip("-")


def expected_issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _validate_issue_authority(issue: IssueTodo, repo: str, plan_path: str) -> None:
    if issue.repo.lower() != repo.lower():
        raise RunRegistrationError("authority_repo_mismatch", issue.repo)
    if len(issue.snapshot) > MAX_ISSUE_SNAPSHOT_CHARS:
        raise RunRegistrationError("authority_invalid", "issue snapshot too large")
    if issue.todo_id != f"TODO-{issue.number}":
        raise RunRegistrationError("authority_invalid", "todo_id does not match issue number")
    if issue.url.lower() != expected_issue_url(issue.repo, issue.number).lower():
        raise RunRegistrationError("authority_invalid", "issue url does not match issue")
    if issue.snapshot != canonical_issue_snapshot(
        issue.repo, issue.number, issue.title, issue.body
    ):
        raise RunRegistrationError("authority_invalid", "issue snapshot is not canonical")
    if issue.plan_values != (plan_path,):
        raise RunRegistrationError("plan_invalid", "issue Plan does not match plan_path")


def _json_payload(registration: RunRegistration) -> dict[str, object]:
    payload = asdict(registration)
    payload["repository"] = str(registration.repository)
    payload["worktree"] = str(registration.worktree)
    payload["step_keys"] = list(registration.step_keys)
    return payload


def _load_registration(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunRegistrationError("registration_invalid") from exc
    if not isinstance(value, dict):
        raise RunRegistrationError("registration_invalid")
    return value


def _validate_or_create_worktree(registration: RunRegistration) -> None:
    target = registration.worktree
    if target.exists():
        common = _git(
            target,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            check=False,
        )
        branch = _git(target, "branch", "--show-current", check=False)
        head = _git(target, "rev-parse", "HEAD", check=False)
        expected_common = registration.repository / ".git"
        if (
            common.returncode != 0
            or Path(common.stdout.strip()).resolve() != expected_common.resolve()
            or branch.returncode != 0
            or branch.stdout.strip() != registration.branch
            or head.returncode != 0
            or head.stdout.strip() != registration.base_sha
        ):
            raise RunRegistrationError("worktree_mismatch", str(target))
        status = _git(
            target,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "-z",
            check=False,
        )
        if status.returncode != 0:
            raise RunRegistrationError("worktree_mismatch", str(target))
        if status.stdout:
            raise RunRegistrationError("worktree_dirty", str(target))
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    branch_ref = _git(
        registration.repository,
        "show-ref",
        "--verify",
        f"refs/heads/{registration.branch}",
        check=False,
    )
    if branch_ref.returncode == 0:
        branch_sha = branch_ref.stdout.split(maxsplit=1)[0]
        if branch_sha != registration.base_sha:
            raise RunRegistrationError("branch_mismatch", registration.branch)
        args = ("worktree", "add", "--", str(target), registration.branch)
    else:
        args = (
            "worktree",
            "add",
            "-b",
            registration.branch,
            "--",
            str(target),
            registration.base_sha,
        )
    result = _git(registration.repository, *args, check=False)
    if result.returncode != 0:
        raise RunRegistrationError("worktree_create_failed", str(target))


def register_pinned_run(
    *,
    project_dir: Path,
    state_dir: Path,
    tick_id: str,
    selected_issue: IssueTodo,
    plan_path: str,
    profile: str,
    prompt_client: str,
    assignee: str,
    review_assignee: str | None,
    step_keys: Iterable[str],
    repo: str | None = None,
) -> RunRegistration:
    """Pin the issue snapshot and tracked Plan as authority, checkpoint, ensure worktree.

    ``repo`` defaults to the live ``origin`` identity; tests inject it.
    """
    project_dir = project_dir.resolve()
    repository = _repository_root(project_dir)
    if repo is None:
        try:
            repo = github_issues.repository_identity(project_dir)
        except GitHubIssuesError as exc:
            raise RunRegistrationError(exc.code, "origin") from exc
    _validate_issue_authority(selected_issue, repo, plan_path)
    base_sha = _git(project_dir, "rev-parse", "HEAD").stdout.strip()
    plan_bytes = _tracked_bytes(project_dir, base_sha, plan_path)
    branch = _validate_branch(project_dir, selected_issue, state_dir=state_dir)
    worktree = (repository / ".worktrees" / _slug(selected_issue)).resolve()
    registration = RunRegistration(
        schema_version=REGISTRATION_SCHEMA_VERSION,
        tick_id=tick_id,
        todo_id=selected_issue.todo_id,
        repository=repository,
        base_sha=base_sha,
        issue_number=selected_issue.number,
        issue_url=selected_issue.url,
        issue_snapshot=selected_issue.snapshot,
        selected_entry_hash=snapshot_hash(selected_issue.snapshot),
        plan_path=plan_path,
        plan_hash=_sha256(plan_bytes),
        branch=branch,
        worktree=worktree,
        profile=profile,
        prompt_client=prompt_client,
        assignee=assignee,
        review_assignee=review_assignee,
        step_keys=tuple(step_keys),
    )
    path = state_dir / "runs" / tick_id / "registration.json"
    payload = _json_payload(registration)
    existing = _load_registration(path)
    if existing is not None and existing != payload:
        raise RunRegistrationError("registration_mismatch")
    if existing is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _validate_or_create_worktree(registration)
    return registration


# -- run state -----------------------------------------------------------------

RegistrationState = Literal["active", "delivered", "abandoned"]


def registration_state(run_dir: Path) -> RegistrationState:
    """``delivered`` once ``issue-closed`` exists, ``abandoned`` once ``abandoned`` does."""
    if (run_dir / "issue-closed").exists():
        return "delivered"
    if (run_dir / "abandoned").exists():
        return "abandoned"
    return "active"


def _registration_issue_number(payload: object) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    number = payload.get("issue_number")
    if type(number) is not int or number <= 0:
        return None
    return number


def active_registration_issue_numbers(state_dir: Path) -> frozenset[int]:
    """Issue numbers pinned by every schema-v2 ``registration.json`` still ``active``.

    Malformed or unsupported registrations are skipped with a WARNING; they never
    widen or narrow the eligible set silently.
    """
    numbers: set[int] = set()
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        return frozenset()
    for path in sorted(runs_dir.glob("*/registration.json")):
        if registration_state(path.parent) != "active":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            _warn_once(path.parent, "skipping unreadable registration %s", path)
            continue
        number = _registration_issue_number(payload)
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != REGISTRATION_SCHEMA_VERSION
            or number is None
        ):
            _warn_once(path.parent, "skipping malformed or unsupported registration %s", path)
            continue
        numbers.add(number)
    return frozenset(numbers)


def ensure_in_progress_label(
    project_dir: Path,
    registration_payload: Mapping,
    *,
    repo: str | None = None,
    live: IssueTodo | None = None,
) -> bool:
    """Re-add ``tpo:in-progress`` to the pinned issue if the live issue lacks it.

    ``live`` is an already-fetched issue (avoids a second ``gh`` read). Idempotent
    and best-effort: returns True only when the label was added; any ``gh``
    failure is logged as a WARNING (Kanban in-flight state and the registration
    remain the hard guards against double selection).
    """
    number = _registration_issue_number(registration_payload)
    if number is None:
        return False
    try:
        if live is None:
            live = github_issues.fetch_issue(project_dir, number, repo=repo)
        if IN_PROGRESS_LABEL in live.labels:
            return False
        github_issues.add_label(project_dir, number, IN_PROGRESS_LABEL, repo=repo)
    except GitHubIssuesError as exc:
        log.warning("could not ensure %s on #%d: %s", IN_PROGRESS_LABEL, number, exc.code)
        return False
    log.info(
        "re-added %s on #%d; use %s to pause this run",
        IN_PROGRESS_LABEL, number, github_issues.ON_HOLD_LABEL,
    )
    return True
