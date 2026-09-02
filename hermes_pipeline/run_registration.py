"""Immutable run registration and conservative linked-worktree creation."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from . import github_issues
from .github_issues import (
    IN_PROGRESS_LABEL,
    MAX_ISSUE_SNAPSHOT_CHARS,
    REGISTRATION_SCHEMA_VERSION,
    SUPPORTED_REGISTRATION_SCHEMA_VERSIONS,
    GitHubIssuesError,
    IssueTodo,
    canonical_issue_snapshot,
    snapshot_hash,
)
from .plan_manifest import PlanSource

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
    plan_source_kind: Literal["embedded", "legacy_path"]
    plan_path: str | None
    plan_artifact: str | None
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


def _validate_issue_authority(
    issue: IssueTodo, repo: str, plan_path: str | None
) -> PlanSource:
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
    if issue.plan_source is not None:
        if plan_path is not None or issue.plan_values:
            raise RunRegistrationError("plan_invalid", "embedded Plan cannot have plan_path")
        try:
            _snapshot_repo, _number, _title, snapshot_body = (
                github_issues.split_canonical_snapshot(issue.snapshot)
            )
            pinned_source = github_issues.embedded_plan_source(
                snapshot_body + "\n", expected_todo_id=issue.todo_id
            )
        except (github_issues.SnapshotFormatError, ValueError) as exc:
            raise RunRegistrationError("plan_invalid", "pinned embedded Plan") from exc
        if pinned_source is None or pinned_source != issue.plan_source:
            raise RunRegistrationError("plan_invalid", "embedded Plan does not match snapshot")
        return pinned_source
    if plan_path is None or issue.plan_values != (plan_path,):
        raise RunRegistrationError("plan_invalid", "issue Plan does not match plan_path")
    from .plan_manifest import legacy_plan_source

    try:
        return legacy_plan_source(Path.cwd(), plan_path, expected_todo_id=issue.todo_id)
    except Exception:
        # The caller resolves legacy bytes at its pinned base SHA below.  This
        # source object carries only the kind/path contract here.
        return PlanSource("legacy_path", "", "", None, plan_path)


def _open_verified_directory(path: Path) -> int:
    """Open ``path`` without following its final component and bind its identity."""
    try:
        before = path.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
    except OSError as exc:
        raise RunRegistrationError("plan_artifact_invalid", "run directory") from exc
    if not stat.S_ISDIR(before.st_mode) or (
        before.st_dev,
        before.st_ino,
    ) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        raise RunRegistrationError("plan_artifact_invalid", "run directory identity")
    return fd


def _open_or_create_directory_at(parent_fd: int, name: str, mode: int) -> int:
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise RunRegistrationError("plan_artifact_invalid", "unsafe directory name")
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(fd)
    except OSError as exc:
        raise RunRegistrationError("plan_artifact_invalid", "directory") from exc
    if not stat.S_ISDIR(before.st_mode) or (
        before.st_dev,
        before.st_ino,
    ) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        raise RunRegistrationError("plan_artifact_invalid", "directory identity")
    return fd


def _open_run_directory(state_dir: Path, tick_id: str) -> tuple[Path, int]:
    if not tick_id or tick_id in (".", "..") or "/" in tick_id or "\\" in tick_id:
        raise RunRegistrationError("registration_invalid", "unsafe tick id")
    state_parent = state_dir.parent
    parent_fd = _open_verified_directory(state_parent)
    try:
        state_fd = _open_or_create_directory_at(parent_fd, state_dir.name, 0o700)
    finally:
        os.close(parent_fd)
    try:
        runs_fd = _open_or_create_directory_at(state_fd, "runs", 0o700)
    finally:
        os.close(state_fd)
    try:
        run_fd = _open_or_create_directory_at(runs_fd, tick_id, 0o700)
    finally:
        os.close(runs_fd)
    return state_dir / "runs" / tick_id, run_fd


def _read_verified_artifact_at(run_fd: int, expected_hash: str) -> bytes:
    """Read a private regular artifact without following links or identity races."""
    try:
        before = os.stat("plan.md", dir_fd=run_fd, follow_symlinks=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open("plan.md", flags, dir_fd=run_fd)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(before.st_mode) != 0o600
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise RunRegistrationError("plan_artifact_invalid", "unsafe identity or mode")
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(fd)
    except RunRegistrationError:
        raise
    except OSError as exc:
        raise RunRegistrationError("plan_artifact_invalid", "unreadable") from exc
    data = b"".join(chunks)
    if _sha256(data) != expected_hash:
        raise RunRegistrationError("plan_artifact_invalid", "digest drift")
    return data


def _read_verified_artifact(path: Path, expected_hash: str) -> bytes:
    run_fd = _open_verified_directory(path.parent)
    try:
        return _read_verified_artifact_at(run_fd, expected_hash)
    finally:
        os.close(run_fd)


def _write_durable_at(run_fd: int, name: str, data: bytes) -> None:
    temp = f".{name}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=run_fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, name, src_dir_fd=run_fd, dst_dir_fd=run_fd)
        os.fsync(run_fd)
    finally:
        try:
            os.unlink(temp, dir_fd=run_fd)
        except FileNotFoundError:
            pass


def _materialize_embedded_artifact(run_fd: int, source: PlanSource) -> None:
    data = source.document.encode("utf-8")
    if _sha256(data) != source.plan_hash:
        raise RunRegistrationError("plan_artifact_invalid", "source digest")
    try:
        _read_verified_artifact_at(run_fd, source.plan_hash)
        return
    except RunRegistrationError:
        try:
            os.stat("plan.md", dir_fd=run_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise
    try:
        _write_durable_at(run_fd, "plan.md", data)
    except OSError as exc:
        raise RunRegistrationError("plan_artifact_invalid", "write failed") from exc
    _read_verified_artifact_at(run_fd, source.plan_hash)


def _write_registration_at(run_fd: int, payload: Mapping[str, object]) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    try:
        _write_durable_at(run_fd, "registration.json", data)
    except OSError as exc:
        raise RunRegistrationError("registration_invalid", "write failed") from exc


def _load_registration_at(run_fd: int) -> dict[str, object] | None:
    try:
        fd = os.open(
            "registration.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=run_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunRegistrationError("registration_invalid") from exc
    try:
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunRegistrationError("registration_invalid") from exc
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise RunRegistrationError("registration_invalid")
    return value


def _json_payload(registration: RunRegistration) -> dict[str, object]:
    payload = asdict(registration)
    payload["repository"] = str(registration.repository)
    payload["worktree"] = str(registration.worktree)
    payload["step_keys"] = list(registration.step_keys)
    return payload


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
    plan_path: str | None,
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
    source = _validate_issue_authority(selected_issue, repo, plan_path)
    base_sha = _git(project_dir, "rev-parse", "HEAD").stdout.strip()
    if source.kind == "embedded":
        plan_bytes = source.document.encode("utf-8")
        effective_hash = source.plan_hash
    else:
        assert plan_path is not None
        plan_bytes = _tracked_bytes(project_dir, base_sha, plan_path)
        effective_hash = _sha256(plan_bytes)
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
        plan_source_kind=source.kind,
        plan_path=plan_path,
        plan_artifact="plan.md" if source.kind == "embedded" else None,
        plan_hash=effective_hash,
        branch=branch,
        worktree=worktree,
        profile=profile,
        prompt_client=prompt_client,
        assignee=assignee,
        review_assignee=review_assignee,
        step_keys=tuple(step_keys),
    )
    payload = _json_payload(registration)
    _run_dir, run_fd = _open_run_directory(state_dir, tick_id)
    try:
        existing = _load_registration_at(run_fd)
        if existing is not None and existing != payload:
            raise RunRegistrationError("registration_mismatch")
        if source.kind == "embedded":
            _materialize_embedded_artifact(run_fd, source)
        if existing is None:
            _write_registration_at(run_fd, payload)
    finally:
        os.close(run_fd)
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


def _active_registrations(state_dir: Path) -> Iterator[tuple[Path, int]]:
    """Yield ``(run_dir, issue_number)`` for every schema-v2 registration still ``active``.

    Malformed or unsupported registrations are skipped with a WARNING; they never
    widen or narrow the eligible set silently.
    """
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        return
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
            or payload.get("schema_version") not in SUPPORTED_REGISTRATION_SCHEMA_VERSIONS
            or number is None
        ):
            _warn_once(path.parent, "skipping malformed or unsupported registration %s", path)
            continue
        yield path.parent, number


def active_registration_issue_numbers(state_dir: Path) -> frozenset[int]:
    """Issue numbers pinned by every schema-v2 ``registration.json`` still ``active``."""
    return frozenset(number for _run_dir, number in _active_registrations(state_dir))


def active_runs_for_issue(state_dir: Path, issue_number: int) -> tuple[str, ...]:
    """Tick ids of active schema-v2 registrations pinning ``issue_number`` (same predicate)."""
    return tuple(
        run_dir.name
        for run_dir, number in _active_registrations(state_dir)
        if number == issue_number
    )


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
