"""Immutable run registration and conservative linked-worktree creation."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from .state import _atomic_write_text
from .todos_md import TodoEntry, parse_todo_entries

REGISTRATION_SCHEMA_VERSION = 1
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


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


def _validate_branch(project_dir: Path, entry: TodoEntry) -> str:
    if len(entry.branch_values) != 1 or not entry.branch_values[0]:
        raise RunRegistrationError("branch_invalid", "exactly one Branch is required")
    branch = entry.branch_values[0]
    result = _git(project_dir, "check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0 or branch.startswith("refs/"):
        raise RunRegistrationError("branch_invalid", "unsafe Branch value")
    return branch


def _slug(entry: TodoEntry) -> str:
    title = _SLUG_UNSAFE_RE.sub("-", entry.title.lower()).strip("-")
    todo = entry.todo_id.lower()
    return f"{todo}-{title}"[:100].rstrip("-")


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
        args = ("worktree", "add", str(target), registration.branch)
    else:
        args = (
            "worktree",
            "add",
            "-b",
            registration.branch,
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
    selected_entry: TodoEntry,
    plan_path: str,
    profile: str,
    prompt_client: str,
    assignee: str,
    review_assignee: str | None,
    step_keys: Iterable[str],
) -> RunRegistration:
    """Pin tracked execution authority, checkpoint it, and ensure its worktree."""
    project_dir = project_dir.resolve()
    repository = _repository_root(project_dir)
    base_sha = _git(project_dir, "rev-parse", "HEAD").stdout.strip()
    todos_bytes = _tracked_bytes(
        project_dir, base_sha, "TODOS.md", require_unchanged=False
    )
    tracked_entry = next(
        (
            entry
            for entry in parse_todo_entries(todos_bytes.decode("utf-8"))
            if entry.todo_id == selected_entry.todo_id
        ),
        None,
    )
    if tracked_entry is None or tracked_entry.raw != selected_entry.raw:
        raise RunRegistrationError("authority_drift", selected_entry.todo_id)
    plan_bytes = _tracked_bytes(project_dir, base_sha, plan_path)
    branch = _validate_branch(project_dir, tracked_entry)
    worktree = (repository / ".worktrees" / _slug(tracked_entry)).resolve()
    registration = RunRegistration(
        schema_version=REGISTRATION_SCHEMA_VERSION,
        tick_id=tick_id,
        todo_id=tracked_entry.todo_id,
        repository=repository,
        base_sha=base_sha,
        selected_entry_hash=_sha256(tracked_entry.raw.encode("utf-8")),
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
