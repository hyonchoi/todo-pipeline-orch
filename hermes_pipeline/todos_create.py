"""Validated, retry-safe creation of embedded-Plan TODO issues."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import github_issues
from .plan_manifest import (
    PlanManifestValidationError,
    parse_plan_manifest,
    render_embedded_plan,
)

REQUEST_KEYS = frozenset({"schema_version", "transaction_id", "title", "fields", "plan_markdown", "tasks"})
FIELD_NAMES = (
    "Summary", "What", "Why", "Pros", "Cons", "Context", "Assumptions", "Spec",
    "Reference", "Branch", "Priority", "Effort", "Phase", "Test Coverage",
    "Security Review", "UI Review",
)
FIELD_KEYS = frozenset(FIELD_NAMES)
REQUIRED_NONEMPTY = frozenset({"Summary", "What", "Why", "Branch", "Priority", "Effort", "Phase", "Test Coverage", "Security Review", "UI Review"})
FIELD_MAX_CHARS = 10_000
TITLE_MAX_CHARS = 256
PLAN_MAX_CHARS = 60_000
MAX_ISSUE_NUMBER = 9_999_999_999_999_999_999
MARKER_PREFIX = "<!-- tpo-create:"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,})"
)


class TodoCreateError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CreateRequest:
    transaction_id: str
    title: str
    fields: dict[str, str]
    plan_markdown: str
    tasks: tuple[dict[str, object], ...]
    canonical_json: str


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TodoCreateError("duplicate_json_key")
        result[key] = value
    return result


def _text(value: object, *, code: str, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or _CONTROL_RE.search(value):
        raise TodoCreateError(code)
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not empty and not normalized:
        raise TodoCreateError(code)
    return normalized


def load_create_request(path: Path) -> CreateRequest:
    descriptor = -1
    try:
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise TodoCreateError("invalid_request_file")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise TodoCreateError("request_mode")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            raw = handle.read()
        after = path.lstat()
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise TodoCreateError("invalid_request_file")
        if _SENSITIVE_RE.search(raw):
            raise TodoCreateError("secret_content")
        value = json.loads(raw, object_pairs_hook=_object)
    except TodoCreateError:
        raise
    except OSError as exc:
        raise TodoCreateError("invalid_request_file") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TodoCreateError("invalid_request") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict) or set(value) != REQUEST_KEYS:
        raise TodoCreateError("unknown_keys")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise TodoCreateError("schema_version")
    transaction = value["transaction_id"]
    try:
        parsed = uuid.UUID(transaction) if isinstance(transaction, str) else None
    except ValueError as exc:
        raise TodoCreateError("transaction_id") from exc
    if parsed is None or parsed.version != 4 or str(parsed) != transaction:
        raise TodoCreateError("transaction_id")
    fields = value["fields"]
    if not isinstance(fields, dict) or set(fields) != FIELD_KEYS:
        raise TodoCreateError("invalid_fields")
    clean_fields = {
        name: _text(fields[name], code="invalid_field", maximum=FIELD_MAX_CHARS, empty=name not in REQUIRED_NONEMPTY)
        for name in FIELD_NAMES
    }
    if any(
        value.startswith("### ")
        or "\n### " in value
        or MARKER_PREFIX in value
        or "```json tpo-plan" in value
        or "<details>" in value
        or "</details>" in value
        for value in clean_fields.values()
    ):
        raise TodoCreateError("invalid_field_structure")
    branch = clean_fields["Branch"]
    if branch.startswith(("-", "refs/")) or ".." in branch or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) is None:
        raise TodoCreateError("unsafe_branch")
    if clean_fields["Priority"] not in {"P0", "P1", "P2", "P3"} or clean_fields["Effort"] not in {"S", "M", "L"}:
        raise TodoCreateError("invalid_field")
    if clean_fields["Phase"] not in github_issues.PHASE_OPTIONS:
        raise TodoCreateError("invalid_field")
    for name in ("Test Coverage", "Security Review", "UI Review"):
        if clean_fields[name] not in {"required", "not-required"}:
            raise TodoCreateError("invalid_field")
    title = _text(value["title"], code="invalid_title", maximum=TITLE_MAX_CHARS)
    if "\n" in title or MARKER_PREFIX in title:
        raise TodoCreateError("invalid_title")
    plan = _text(value["plan_markdown"], code="invalid_plan", maximum=PLAN_MAX_CHARS)
    if (
        "```json tpo-plan" in plan
        or re.search(r"(?i)<!--\s*tpo-create:", plan)
        or re.search(r"(?i)</?details(?:\s|>)", plan)
        or re.search(
            r"(?i)<summary\s*>\s*Implementation Plan\s*</summary\s*>", plan
        )
        or re.search(r"(?i)</?proposed_plan(?:\s|>)", plan)
    ):
        raise TodoCreateError("forbidden_plan_structure")
    tasks = value["tasks"]
    manifest = {"schema_version": 1, "todo_id": "TODO-1", "tasks": tasks}
    document = plan + "\n\n```json tpo-plan\n" + json.dumps(manifest, indent=2) + "\n```\n"
    try:
        parsed_manifest = parse_plan_manifest(document, expected_todo_id="TODO-1")
    except PlanManifestValidationError as exc:
        raise TodoCreateError(exc.code) from exc
    assert parsed_manifest is not None
    task_keys = (
        "id", "title", "instructions", "acceptance_criteria", "verification", "commit_message"
    )
    normalized_tasks = tuple({key: task[key] for key in task_keys} for task in tasks)
    normalized = {
        "schema_version": 1,
        "transaction_id": str(parsed),
        "title": title,
        "fields": clean_fields,
        "plan_markdown": plan,
        "tasks": normalized_tasks,
    }
    canonical = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    return CreateRequest(str(parsed), title, clean_fields, plan, normalized_tasks, canonical)


def creation_marker(transaction_id: str) -> str:
    return f"{MARKER_PREFIX}{transaction_id} -->"


def _render_fields(request: CreateRequest) -> str:
    chunks = [creation_marker(request.transaction_id)]
    for name in FIELD_NAMES:
        chunks.append(f"### {name}\n\n{request.fields[name] or github_issues.NO_RESPONSE}")
    return "\n\n".join(chunks) + "\n"


def render_create_body(request: CreateRequest, *, issue_number: int) -> str:
    if not 1 <= issue_number <= MAX_ISSUE_NUMBER:
        raise TodoCreateError("issue_number")
    manifest = {"schema_version": 1, "todo_id": f"TODO-{issue_number}", "tasks": list(request.tasks)}
    document = request.plan_markdown.rstrip("\n") + "\n\n```json tpo-plan\n" + json.dumps(manifest, indent=2, ensure_ascii=False) + "\n```\n"
    body = _render_fields(request) + "\n" + render_embedded_plan(document, expected_todo_id=f"TODO-{issue_number}")
    if len(body) > github_issues.MAX_ISSUE_BODY_CHARS:
        raise TodoCreateError("body_too_large")
    return body


def validate_create_input_path(
    path: Path, state_dir: Path, *, transaction_id: str
) -> None:
    expected = state_dir / "todo-create-input" / f"{transaction_id}.json"
    try:
        input_dir = state_dir / "todo-create-input"
        state_info = state_dir.lstat()
        input_info = input_dir.lstat()
        if (
            stat.S_ISLNK(state_info.st_mode)
            or not stat.S_ISDIR(state_info.st_mode)
            or stat.S_ISLNK(input_info.st_mode)
            or not stat.S_ISDIR(input_info.st_mode)
            or stat.S_IMODE(input_info.st_mode) != 0o700
            or path.absolute().parent != input_dir.absolute()
            or path.resolve(strict=True) != expected.resolve(strict=True)
            or state_dir.resolve(strict=True).parent
            != state_dir.parent.resolve(strict=True)
        ):
            raise TodoCreateError("invalid_request_path")
    except OSError as exc:
        raise TodoCreateError("invalid_request_path") from exc


def render_create_preview(
    request: CreateRequest,
    *,
    project: str,
    repository: str,
    issue_number: int | None,
) -> str:
    """Render the normalized title/body preview, with an explicit unassigned-ID token."""
    if issue_number is not None:
        body = render_create_body(request, issue_number=issue_number)
    else:
        body = render_create_body(request, issue_number=MAX_ISSUE_NUMBER)
        body = body.replace(
            f'  "todo_id": "TODO-{MAX_ISSUE_NUMBER}",',
            '  "todo_id": "TODO-<assigned-by-github>",',
            1,
        )
    return (
        f"Project: {project}\nRepository: {repository}\n"
        f"Title:\n{request.title}\n\nBody:\n{body}"
    )


def persist_create_request(state_dir: Path, request: CreateRequest) -> Path:
    directory = state_dir / "todo-create"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request.transaction_id}.json"
    if path.exists():
        before = path.lstat()
        if not path.is_file() or path.is_symlink() or path.read_text(encoding="utf-8") != request.canonical_json:
            raise TodoCreateError("request_drift")
        after = path.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise TodoCreateError("request_drift")
        if before.st_mode & 0o777 != 0o600:
            raise TodoCreateError("request_mode")
        return path
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(request.canonical_json)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError as exc:
            raise TodoCreateError("request_drift") from exc
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


@contextmanager
def create_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "todo-create.lock"
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TodoCreateError("create_locked") from exc
        yield


def _matching_issues(project_dir: Path, repo: str, marker: str):
    matches = tuple(issue for issue in github_issues.list_all_issues(
        project_dir, repo=repo
    ) if marker in issue.body)
    if any(issue.body.count(marker) != 1 for issue in matches):
        raise TodoCreateError("duplicate_marker")
    return matches


def execute_create(
    project_dir: Path,
    state_dir: Path,
    request: CreateRequest,
    *,
    approved_repo: str,
    issue_number: int | None = None,
) -> int:
    if github_issues.repository_identity(project_dir) != approved_repo:
        raise TodoCreateError("repository_drift")
    repo = approved_repo
    marker = creation_marker(request.transaction_id)
    # The issue number changes only a few manifest characters; validate the complete
    # body limit before the first remote mutation as well as after the real ID exists.
    render_create_body(request, issue_number=MAX_ISSUE_NUMBER)
    with create_lock(state_dir):
        persisted = persist_create_request(state_dir, request)
        import subprocess

        try:
            branch_check = subprocess.run(
                ["git", "check-ref-format", "--branch", request.fields["Branch"]],
                cwd=project_dir, capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TodoCreateError("unsafe_branch") from exc
        if branch_check.returncode != 0:
            raise TodoCreateError("unsafe_branch")
        matches = _matching_issues(project_dir, repo, marker)
        if len(matches) > 1:
            raise TodoCreateError("duplicate_marker")
        if issue_number is not None:
            issue = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
            if matches and matches[0].number != issue_number:
                raise TodoCreateError("issue_mismatch")
            if marker not in issue.body:
                raise TodoCreateError("issue_marker_mismatch")
        elif matches:
            issue = matches[0]
            issue_number = issue.number
        else:
            try:
                issue_number = github_issues.create_issue(
                    project_dir, title=request.title, body=_render_fields(request),
                    labels=(github_issues.TODO_LABEL, "needs-triage"), repo=repo,
                )
            except github_issues.GitHubIssuesError:
                matches = _matching_issues(project_dir, repo, marker)
                if len(matches) != 1:
                    raise
                issue_number = matches[0].number
            issue = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
        assert issue_number is not None
        final_body = render_create_body(request, issue_number=issue_number)
        if issue.title != request.title or issue.state != "open":
            raise TodoCreateError("issue_drift")
        if issue.body != final_body:
            if issue.body != _render_fields(request):
                raise TodoCreateError("issue_drift")
            github_issues.update_issue_body(project_dir, issue_number, final_body, repo=repo)
            issue = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
            if issue.body != final_body:
                raise TodoCreateError("late_drift")
        # Every label mutation is conditional on a fresh authoritative snapshot.
        issue = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
        if issue.title != request.title or issue.state != "open" or issue.body != final_body:
            raise TodoCreateError("late_drift")
        from .cli import _audit_default_branch, _audit_issue, _audit_phase_options
        findings, add, remove = _audit_issue(
            project_dir, issue, phase_options=_audit_phase_options(project_dir),
            default_branch=_audit_default_branch(project_dir), branch_cache={}, require_todo_label=True,
        )
        actionable = [item for item in findings if not item.startswith("label:")]
        if actionable:
            raise TodoCreateError("audit_failed")
        def assert_fresh() -> None:
            fresh = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
            if fresh.title != request.title or fresh.state != "open" or fresh.body != final_body:
                raise TodoCreateError("late_drift")

        current_labels = {label.lower() for label in issue.labels}
        for label in add:
            if label.lower() not in current_labels:
                assert_fresh()
                github_issues.add_label(project_dir, issue_number, label, repo=repo)
                current_labels.add(label.lower())
        if github_issues.READY_LABEL not in current_labels:
            assert_fresh()
            github_issues.add_label(project_dir, issue_number, github_issues.READY_LABEL, repo=repo)
            current_labels.add(github_issues.READY_LABEL)
        for label in remove:
            if label.lower() in current_labels:
                assert_fresh()
                github_issues.remove_label(project_dir, issue_number, label, repo=repo)
                current_labels.discard(label.lower())
        if "needs-triage" in current_labels:
            assert_fresh()
            github_issues.remove_label(project_dir, issue_number, "needs-triage", repo=repo)
        complete = github_issues.fetch_issue(project_dir, issue_number, repo=repo)
        final_findings, _final_add, _final_remove = _audit_issue(
            project_dir, complete, phase_options=_audit_phase_options(project_dir),
            default_branch=_audit_default_branch(project_dir), branch_cache={}, require_todo_label=True,
        )
        expected = {
            label.lower() for label in (github_issues.TODO_LABEL, github_issues.READY_LABEL, *add)
        }
        triage = {label.lower() for label in github_issues.TRIAGE_PENDING_LABELS}
        complete_labels = {label.lower() for label in complete.labels}
        is_complete = (
            complete.title == request.title
            and complete.state == "open"
            and complete.body == final_body
            and expected.issubset(complete_labels)
            and not triage.intersection(complete_labels)
            and not final_findings
        )
        if is_complete:
            persisted.unlink()
            directory_fd = os.open(persisted.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        else:
            raise TodoCreateError("create_pending")
        return issue_number
