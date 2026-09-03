"""Strict worker-result parsing and independently checked Git evidence."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import github_issues
from .github_issues import (
    MAX_ISSUE_SNAPSHOT_CHARS,
    REGISTRATION_SCHEMA_VERSION,
    GitHubIssuesError,
    SnapshotFormatError,
    parse_issue_body,
    snapshot_hash,
    split_canonical_snapshot,
)
from .plan_manifest import PlanManifest, parse_plan_manifest

MAX_METADATA_BYTES = 64 * 1024
MAX_SUMMARY_LENGTH = 8 * 1024
MAX_COMMAND_LENGTH = 500
MAX_FINDINGS = 50
MAX_LOCATION_LENGTH = 256
MAX_FINDING_TEXT_LENGTH = 1000
SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"[0-9a-f]{40}")
# C0/DEL controls plus Unicode line/paragraph separators and bidi overrides.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_LINE_BREAK_RE = re.compile(r"[\n\r\t\u2028\u2029]+")
_SECRET_RE = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{20,}|(?:authorization|token|password|secret)\s*[:=]\s*\S+)"
)
_TOP_KEYS = {
    "schema_version",
    "tick_id",
    "todo_id",
    "step_key",
    "verdict",
    "external_session_id",
    "git",
    "tdd",
    "acceptance",
}
_OPTIONAL_TOP_KEYS = {"review", "delivery"}


class ResultContractError(RuntimeError):
    """Worker evidence is missing, malformed, or inconsistent."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int


@dataclass(frozen=True)
class GitResult:
    expected_parent_sha: str
    resulting_head_sha: str
    task_commit_sha: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True)
class WorkerResult:
    tick_id: str
    todo_id: str
    step_key: str
    external_session_id: str
    git: GitResult
    red: CommandResult
    green: CommandResult
    refactor: CommandResult
    review: ReviewEvidence | None = None
    delivery: DeliveryEvidence | None = None


@dataclass(frozen=True)
class ReviewEvidence:
    verdict: str
    findings: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class DeliveryEvidence:
    pr_url: str
    branch: str
    head_sha: str
    checks: tuple[CommandResult, ...]


@dataclass(frozen=True)
class ValidatedRegistration:
    todo_id: str
    repository: Path
    base_sha: str
    issue_number: int
    issue_url: str
    plan_path: str
    branch: str
    worktree: Path
    step_keys: tuple[str, ...]
    manifest: PlanManifest | None
    assignee: str
    review_assignee: str | None
    prompt_client: str
    # The hash verified against the Plan bytes read at ``base_sha``; callers
    # that hold the Plan text independently can re-pin it without re-reading
    # the (worker-writable) registration file.
    plan_hash: str


def sanitize_result_text(value: object, *, maximum: int) -> str:
    """Return bounded, display-safe diagnostics without credential material."""
    text = _CONTROL_RE.sub("", str(value))
    text = _LINE_BREAK_RE.sub(" ", text)
    text = _SECRET_RE.sub("[REDACTED]", text)
    return text[:maximum]


def _reject_unsafe_strings(value: object) -> None:
    if isinstance(value, str):
        if _CONTROL_RE.search(value) or _SECRET_RE.search(value):
            raise ResultContractError("unsafe_metadata")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_unsafe_strings(key)
            _reject_unsafe_strings(item)
    elif isinstance(value, list):
        for item in value:
            _reject_unsafe_strings(item)


def _mapping(value: object, *, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ResultContractError(code)
    return value


def _exact_keys(value: dict[str, object], keys: set[str], *, code: str) -> None:
    if set(value) != keys:
        raise ResultContractError(code, "unexpected or missing fields")


def _bounded_string(value: object, *, maximum: int, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ResultContractError("size_limit" if isinstance(value, str) and len(value) > maximum else code)
    if _CONTROL_RE.search(value) or _SECRET_RE.search(value):
        raise ResultContractError("unsafe_metadata")
    return value


def _command(value: object, *, name: str) -> CommandResult:
    item = _mapping(value, code="invalid_tdd")
    _exact_keys(item, {"command", "exit_code"}, code="invalid_tdd")
    command = _bounded_string(item["command"], maximum=MAX_COMMAND_LENGTH, code="invalid_tdd")
    exit_code = item["exit_code"]
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise ResultContractError("invalid_tdd", name)
    return CommandResult(command, exit_code)


def _successful_runs(payload: dict[str, object]) -> list[dict[str, object]]:
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise ResultContractError("malformed_payload")
    successful: list[dict[str, object]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        status = run.get("status")
        outcome = run.get("outcome")
        if status in {"success", "succeeded", "completed", "done"} or outcome in {
            "success",
            "succeeded",
            "completed",
            "done",
        } or (
            status is None and run.get("exit_code") == 0
        ):
            successful.append(run)
    if not successful:
        raise ResultContractError("missing_successful_run")
    return successful


def parse_worker_result(
    payload: object,
    *,
    tick_id: str,
    todo_id: str,
    step_key: str,
    acceptance_criteria: tuple[str, ...],
    allow_no_changes: bool = False,
) -> WorkerResult:
    """Parse ``metadata.tpo_result`` from the final successful Hermes run."""
    envelope = _mapping(payload, code="malformed_payload")
    run = _successful_runs(envelope)[-1]
    summary = run.get("summary")
    if summary is not None:
        _bounded_string(summary, maximum=MAX_SUMMARY_LENGTH, code="invalid_summary")
    metadata = _mapping(run.get("metadata"), code="missing_result")
    try:
        metadata_encoded = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ResultContractError("malformed_result") from exc
    if len(metadata_encoded) > MAX_METADATA_BYTES:
        raise ResultContractError("size_limit", "metadata")
    _reject_unsafe_strings(metadata)
    _exact_keys(metadata, {"tpo_result"}, code="malformed_result")
    raw = _mapping(metadata.get("tpo_result"), code="missing_result")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise ResultContractError("malformed_result") from exc
    if len(encoded) > MAX_METADATA_BYTES:
        raise ResultContractError("size_limit", "metadata")
    _reject_unsafe_strings(raw)
    if not _TOP_KEYS <= set(raw) or set(raw) - _TOP_KEYS - _OPTIONAL_TOP_KEYS:
        raise ResultContractError("malformed_result", "unexpected or missing fields")
    if raw["schema_version"] != SCHEMA_VERSION or raw["verdict"] != "success":
        raise ResultContractError("invalid_verdict")
    identities = (raw["tick_id"], raw["todo_id"], raw["step_key"])
    if identities != (tick_id, todo_id, step_key):
        raise ResultContractError("identity_mismatch")
    session = _bounded_string(raw["external_session_id"], maximum=256, code="invalid_session")

    git = _mapping(raw["git"], code="invalid_git")
    _exact_keys(
        git,
        {"expected_parent_sha", "resulting_head_sha", "task_commit_sha", "changed_files"},
        code="invalid_git",
    )
    shas = [git[key] for key in ("expected_parent_sha", "resulting_head_sha", "task_commit_sha")]
    if not all(isinstance(sha, str) and _SHA_RE.fullmatch(sha) for sha in shas):
        raise ResultContractError("invalid_git")
    if shas[1] != shas[2]:
        raise ResultContractError("invalid_git", "head and task commit differ")
    files = git["changed_files"]
    if (
        not isinstance(files, list)
        or (not files and not allow_no_changes)
        or len(files) != len(set(files))
    ):
        raise ResultContractError("invalid_git", "changed_files")
    for filename in files:
        if not isinstance(filename, str) or not filename or len(filename) > 500:
            raise ResultContractError("invalid_git", "changed_files")
        path = PurePosixPath(filename)
        if path.is_absolute() or ".." in path.parts or "\\" in filename:
            raise ResultContractError("invalid_git", "unsafe changed file")
    git_result = GitResult(shas[0], shas[1], shas[2], tuple(files))

    tdd = _mapping(raw["tdd"], code="invalid_tdd")
    _exact_keys(tdd, {"red", "green", "refactor"}, code="invalid_tdd")
    red = _command(tdd["red"], name="red")
    green = _command(tdd["green"], name="green")
    refactor = _command(tdd["refactor"], name="refactor")
    if red.exit_code == 0 or green.exit_code != 0 or refactor.exit_code != 0:
        raise ResultContractError("invalid_tdd", "red/green/refactor exit codes")

    acceptance = raw["acceptance"]
    if not isinstance(acceptance, list) or len(acceptance) != len(acceptance_criteria):
        raise ResultContractError("invalid_acceptance")
    observed: list[str] = []
    for item in acceptance:
        entry = _mapping(item, code="invalid_acceptance")
        _exact_keys(entry, {"criterion", "status"}, code="invalid_acceptance")
        if entry["status"] != "passed" or not isinstance(entry["criterion"], str):
            raise ResultContractError("invalid_acceptance")
        observed.append(entry["criterion"])
    if tuple(observed) != acceptance_criteria:
        raise ResultContractError("invalid_acceptance", "criteria mismatch")
    review_evidence = None
    if "review" in raw:
        _validate_review(raw["review"])
        review_raw = raw["review"]
        assert isinstance(review_raw, dict)
        findings = review_raw["findings"]
        assert isinstance(findings, list)
        review_evidence = ReviewEvidence(
            verdict=str(review_raw["verdict"]),
            findings=tuple(dict(item) for item in findings if isinstance(item, dict)),
        )
    delivery_evidence = None
    if "delivery" in raw:
        delivery_evidence = _validate_delivery(raw["delivery"])
    return WorkerResult(
        tick_id, todo_id, step_key, session, git_result, red, green, refactor,
        review_evidence, delivery_evidence,
    )


def verify_read_only_review(
    worktree: Path,
    result: WorkerResult,
    *,
    head_sha: str,
    require_current: bool = True,
) -> None:
    """Verify a review used an unchanged, clean pinned worktree."""
    if result.review is None:
        raise ResultContractError("invalid_review", "missing review evidence")
    if (
        result.git.expected_parent_sha != head_sha
        or result.git.resulting_head_sha != head_sha
        or result.git.task_commit_sha != head_sha
        or result.git.changed_files
    ):
        raise ResultContractError("review_changed_head")
    if not require_current:
        return
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout:
        raise ResultContractError("review_dirty_worktree")


def _validate_review(value: object) -> None:
    review = _mapping(value, code="invalid_review")
    _exact_keys(review, {"verdict", "findings"}, code="invalid_review")
    verdict = review["verdict"]
    findings = review["findings"]
    if verdict not in {"clean", "findings"} or not isinstance(findings, list):
        raise ResultContractError("invalid_review")
    if len(findings) > MAX_FINDINGS or (verdict == "clean") != (not findings):
        raise ResultContractError("invalid_review")
    for finding in findings:
        item = _mapping(finding, code="invalid_review")
        _exact_keys(
            item,
            {"priority", "location", "failure_scenario", "recommendation"},
            code="invalid_review",
        )
        if item["priority"] not in {"P0", "P1", "P2", "P3"}:
            raise ResultContractError("invalid_review")
        _bounded_string(item["location"], maximum=MAX_LOCATION_LENGTH, code="invalid_review")
        _bounded_string(
            item["failure_scenario"], maximum=MAX_FINDING_TEXT_LENGTH, code="invalid_review"
        )
        _bounded_string(
            item["recommendation"], maximum=MAX_FINDING_TEXT_LENGTH, code="invalid_review"
        )


def _validate_delivery(value: object) -> DeliveryEvidence:
    delivery = _mapping(value, code="invalid_delivery")
    _exact_keys(delivery, {"pr_url", "branch", "head_sha", "checks"}, code="invalid_delivery")
    pr_url = _bounded_string(delivery["pr_url"], maximum=1000, code="invalid_delivery")
    if not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+", pr_url
    ):
        raise ResultContractError("invalid_delivery")
    _bounded_string(delivery["branch"], maximum=256, code="invalid_delivery")
    if not isinstance(delivery["head_sha"], str) or not _SHA_RE.fullmatch(
        delivery["head_sha"]
    ):
        raise ResultContractError("invalid_delivery")
    checks = delivery["checks"]
    if not isinstance(checks, list) or not checks or len(checks) > 50:
        raise ResultContractError("invalid_delivery")
    parsed_checks = tuple(_command(check, name="delivery check") for check in checks)
    if any(check.exit_code != 0 for check in parsed_checks):
        raise ResultContractError("invalid_delivery", "failed check")
    return DeliveryEvidence(pr_url, str(delivery["branch"]), str(delivery["head_sha"]), parsed_checks)


_REGISTRATION_KEYS = {
    "schema_version",
    "tick_id",
    "todo_id",
    "repository",
    "base_sha",
    "issue_number",
    "issue_url",
    "issue_snapshot",
    "selected_entry_hash",
    "plan_path",
    "plan_hash",
    "branch",
    "worktree",
    "profile",
    "prompt_client",
    "assignee",
    "review_assignee",
    "step_keys",
}


def load_validated_registration(
    project_dir: Path, state_dir: Path, tick_id: str, *, repo: str | None = None
) -> ValidatedRegistration:
    """Load exact registration authority and verify it against its pinned issue snapshot.

    ``repo`` defaults to the live ``origin`` identity; tests inject it.
    """
    path = state_dir / "runs" / tick_id / "registration.json"
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResultContractError("registration_invalid") from exc
    registration = _mapping(raw, code="registration_invalid")
    if registration.get("schema_version") != REGISTRATION_SCHEMA_VERSION:
        raise ResultContractError("registration_invalid", "unsupported schema_version")
    _exact_keys(registration, _REGISTRATION_KEYS, code="registration_invalid")
    # The issue snapshot is hash-pinned authority content, not agent metadata:
    # bound its size instead of scanning it for secret-like text.
    _reject_unsafe_strings({key: value for key, value in registration.items() if key != "issue_snapshot"})
    if (
        not isinstance(registration["issue_snapshot"], str)
        or len(registration["issue_snapshot"]) > MAX_ISSUE_SNAPSHOT_CHARS
    ):
        raise ResultContractError("registration_invalid", "issue snapshot size")
    if registration["tick_id"] != tick_id:
        raise ResultContractError("registration_invalid")
    string_keys = (
        "todo_id",
        "repository",
        "base_sha",
        "issue_url",
        "issue_snapshot",
        "selected_entry_hash",
        "plan_path",
        "plan_hash",
        "branch",
        "worktree",
        "profile",
        "prompt_client",
        "assignee",
    )
    if not all(isinstance(registration[key], str) and registration[key] for key in string_keys):
        raise ResultContractError("registration_invalid")
    issue_number = registration["issue_number"]
    if type(issue_number) is not int or issue_number <= 0:
        raise ResultContractError("registration_invalid")
    if registration["review_assignee"] is not None and (
        not isinstance(registration["review_assignee"], str)
        or not registration["review_assignee"]
    ):
        raise ResultContractError("registration_invalid")
    if not _SHA_RE.fullmatch(registration["base_sha"]) or not re.fullmatch(
        r"[0-9a-f]{64}", registration["selected_entry_hash"]
    ) or not re.fullmatch(r"[0-9a-f]{64}", registration["plan_hash"]):
        raise ResultContractError("registration_invalid")
    steps = registration["step_keys"]
    if (
        not isinstance(steps, list)
        or not steps
        or len(steps) > 200
        or not all(isinstance(step, str) and 0 < len(step) <= 256 for step in steps)
        or len(steps) != len(set(steps))
    ):
        raise ResultContractError("registration_invalid")

    repository = Path(registration["repository"]).resolve()
    worktree = Path(registration["worktree"]).resolve()
    if repository != project_dir.resolve() or worktree.parent != repository / ".worktrees":
        raise ResultContractError("registration_invalid")
    common = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    branch = _git(worktree, "branch", "--show-current")
    if common != (repository / ".git").resolve() or branch != registration["branch"]:
        raise ResultContractError("registration_invalid")

    plan_path = registration["plan_path"]
    relative = PurePosixPath(plan_path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in plan_path:
        raise ResultContractError("registration_invalid")
    base_sha = registration["base_sha"]
    plan_bytes = _git_bytes(repository, "show", f"{base_sha}:{plan_path}")
    if hashlib.sha256(plan_bytes).hexdigest() != registration["plan_hash"]:
        raise ResultContractError("registration_invalid")
    try:
        manifest = parse_plan_manifest(
            plan_bytes.decode("utf-8"), expected_todo_id=registration["todo_id"]
        )
    except (UnicodeError, ValueError) as exc:
        raise ResultContractError("registration_invalid") from exc
    snapshot = registration["issue_snapshot"]
    if snapshot_hash(snapshot) != registration["selected_entry_hash"]:
        raise ResultContractError("registration_invalid", "issue snapshot hash")
    try:
        snapshot_repo, number, title, body = split_canonical_snapshot(snapshot)
    except SnapshotFormatError as exc:
        raise ResultContractError("registration_invalid", "issue snapshot") from exc
    if repo is None:
        try:
            repo = github_issues.repository_identity(project_dir)
        except GitHubIssuesError as exc:
            raise ResultContractError("git_verification_failed", "remote") from exc
    if (
        snapshot_repo.lower() != repo.lower()
        or number != issue_number
        or registration["todo_id"] != f"TODO-{number}"
        or registration["issue_url"].lower()
        != f"https://github.com/{snapshot_repo}/issues/{number}".lower()
    ):
        raise ResultContractError("registration_invalid", "issue identity")
    sections = parse_issue_body(body)
    title_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    expected_worktree = (
        repository / ".worktrees" / f"todo-{number}-{title_slug}"[:100].rstrip("-")
    ).resolve()
    if (
        worktree != expected_worktree
        or github_issues.first_lines(sections.get("Plan", ())) != (plan_path,)
        or github_issues.first_lines(sections.get("Branch", ())) != (registration["branch"],)
    ):
        raise ResultContractError("registration_invalid", "issue fields")
    if manifest is not None:
        required_steps = {
            key
            for task in manifest.tasks
            for key in (f"plan:{task.id}", f"validate:{task.id}")
        }
        if not required_steps <= set(steps):
            raise ResultContractError("registration_invalid")
    return ValidatedRegistration(
        registration["todo_id"],
        repository,
        base_sha,
        issue_number,
        registration["issue_url"],
        plan_path,
        registration["branch"],
        worktree,
        tuple(steps),
        manifest,
        registration["assignee"],
        registration["review_assignee"],
        registration["prompt_client"],
        registration["plan_hash"],
    )


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise ResultContractError("git_verification_failed") from exc
    if result.returncode != 0:
        raise ResultContractError("git_verification_failed", args[0])
    return result.stdout.strip()


def _git_bytes(cwd: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResultContractError("registration_invalid") from exc
    if result.returncode != 0:
        raise ResultContractError("registration_invalid")
    return result.stdout


def verify_worker_git_result(
    worktree: Path, git: GitResult, *, expected_parent_sha: str
) -> None:
    """Verify immutable commit topology and changed paths in the pinned worktree."""
    verify_worker_git_topology(worktree, git, expected_parent_sha=expected_parent_sha)
    status = _git_bytes(
        worktree, "status", "--porcelain=v1", "--untracked-files=all", "-z"
    )
    if status:
        raise ResultContractError("worktree_dirty")
    actual_head = _git(worktree, "rev-parse", "HEAD")
    if actual_head != git.resulting_head_sha:
        raise ResultContractError("head_mismatch")


def verify_worker_git_topology(
    worktree: Path, git: GitResult, *, expected_parent_sha: str
) -> None:
    """Verify immutable commit topology without requiring it to be current HEAD."""
    if git.expected_parent_sha != expected_parent_sha:
        raise ResultContractError("parent_mismatch")
    if git.resulting_head_sha != git.task_commit_sha:
        raise ResultContractError("head_mismatch")
    parent = _git(worktree, "rev-parse", f"{git.task_commit_sha}^")
    if parent != expected_parent_sha:
        raise ResultContractError("parent_mismatch")
    count = _git(worktree, "rev-list", "--count", f"{expected_parent_sha}..{git.resulting_head_sha}")
    if count != "1":
        raise ResultContractError("commit_count_mismatch")
    changed = tuple(
        sorted(
            line
            for line in _git(
                worktree, "diff", "--name-only", expected_parent_sha, git.resulting_head_sha
            ).splitlines()
            if line
        )
    )
    if changed != tuple(sorted(git.changed_files)):
        raise ResultContractError("changed_files_mismatch")
