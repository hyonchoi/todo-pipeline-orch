"""GitHub Issues TODO contract plus the ``gh`` CLI client that feeds it.

The first half is pure (labels, body sections, snapshots, eligibility) and
touches neither subprocess nor network. The second half (``_gh`` and the
``list_*``/``fetch_*``/write helpers) is the only place GitHub is reached, and
it does so exclusively through the ``gh`` CLI. Nothing runs at import time.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from .plan_manifest import (
    PlanManifestValidationError,
    TodoPlanValidationError,
    validate_plan_candidate,
    validate_plan_path,
)

log = logging.getLogger(__name__)
# Subprocess seam: tests patch this symbol instead of ``subprocess.run`` globally.
_run = subprocess.run

GH_BIN_ENV = "TPO_GH_BIN"
# ``gh api --slurp`` (used for pagination) first shipped in gh 2.44.
MIN_GH_VERSION = "2.44"
TODO_LABEL = "tpo:todo"
READY_LABEL = "ready-for-agent"
ON_HOLD_LABEL = "tpo:on-hold"
IN_PROGRESS_LABEL = "tpo:in-progress"
TRIAGE_PENDING_LABELS = ("needs-triage", "needs-info", "ready-for-human", "wontfix")
MAX_ISSUE_BODY_CHARS = 65_536
MAX_ISSUE_SNAPSHOT_CHARS = MAX_ISSUE_BODY_CHARS + 4096
REGISTRATION_SCHEMA_VERSION = 2
SNAPSHOT_HEADER = "tpo-issue-snapshot/1"
SELECTION_BODY_MAX_CHARS = 4000
NO_RESPONSE = "_No response_"

KNOWN_SECTIONS: tuple[str, ...] = (
    "Summary",
    "What",
    "Why",
    "Pros",
    "Cons",
    "Context",
    "Assumptions",
    "Plan",
    "Spec",
    "Reference",
    "Branch",
    "Priority",
    "Effort",
    "Phase",
    "Test Coverage",
    "Security Review",
    "UI Review",
    "Legacy ID",
)
REQUIRED_SECTIONS = ("What", "Why", "Branch", "Priority", "Effort")
# Phase dropdown options of the issue form (single source; the form contract test binds
# .github/ISSUE_TEMPLATE/tpo-todo.yml to this tuple).
PHASE_OPTIONS: tuple[str, ...] = (
    "2 (Design)",
    "3 (Writing Plan)",
    "4 (Development)",
    "5 (Code Review)",
    "6.1 (CSO Security Review)",
    "6.2 (QA)",
    "7 (Document Release)",
    "8 (Finish Branch)",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def phase_label(phase_value: str) -> str:
    """Map a free-form Phase value (``"4 (Development)"``) to ``phase:4-development``."""
    slug = _SLUG_RE.sub("-", phase_value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("phase value has no label slug")
    return f"phase:{slug}"


LABEL_VOCABULARY: tuple[tuple[str, str, str], ...] = (
    (TODO_LABEL, "1d76db", "Managed TODO entry for todo-pipeline-orchestrator"),
    (ON_HOLD_LABEL, "e4e669", "TODO is paused and must not be selected"),
    (IN_PROGRESS_LABEL, "fbca04", "TODO is claimed by an active pipeline run"),
    ("needs-triage", "ededed", "Maintainer needs to evaluate this issue"),
    ("needs-info", "d4c5f9", "Waiting on reporter for more information"),
    (READY_LABEL, "0e8a16", "Fully specified, ready for an AFK agent"),
    ("ready-for-human", "5319e7", "Requires human implementation"),
    ("wontfix", "ffffff", "Will not be actioned"),
    ("priority:P0", "b60205", "Priority P0"),
    ("priority:P1", "d93f0b", "Priority P1"),
    ("priority:P2", "fbca04", "Priority P2"),
    ("priority:P3", "c2e0c6", "Priority P3"),
    ("effort:S", "c5def5", "Effort: small"),
    ("effort:M", "bfd4f2", "Effort: medium"),
    ("effort:L", "9ecbff", "Effort: large"),
    ("test-coverage:required", "0e8a16", "Test coverage required"),
    ("test-coverage:not-required", "e6e6e6", "Test coverage not required"),
    ("security-review:required", "b60205", "Security review required"),
    ("security-review:not-required", "e6e6e6", "Security review not required"),
    ("ui-review:required", "5319e7", "UI review required"),
    ("ui-review:not-required", "e6e6e6", "UI review not required"),
    *((phase_label(option), "c5def5", f"Phase {option}") for option in PHASE_OPTIONS),
)

_HEADING_RE = re.compile(r"^### (.+?)\s*$", re.MULTILINE)
# CommonMark fenced code block delimiter: up to 3 spaces of indent, then >=3 backticks or tildes.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_SNAPSHOT_NUMBER_RE = re.compile(r"0|[1-9][0-9]*")
_H3_LINE_RE = re.compile(r"^### ", re.MULTILINE)
_HOSTILE_SELECTION_LINE_RE = re.compile(
    r"^\s*(?:(?i:</?(?:candidate_todos|todos_md_content)\s*>)"
    r"|[-*+]\s*\[[ x→~]\]\s*\*\*TODO-\d+)"
)


class SnapshotFormatError(ValueError):
    """A canonical issue snapshot does not match the expected layout."""


class NotAnIssueError(ValueError):
    """The REST payload describes a pull request, not an issue."""


def legacy_id_label(todo_id: str) -> str:
    return f"legacy-id:{todo_id}"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_issue_body(body: str) -> dict[str, tuple[str, ...]]:
    """Split an issue body on ``### <Section>`` headings into known sections.

    Headings inside fenced code blocks (three or more backticks or tildes, indented up
    to three spaces) do not start a section; the fenced text stays part of the
    enclosing section. An unterminated fence runs to the end of the body.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    buffer: list[str] = []
    fence: tuple[str, int] | None = None

    def flush() -> None:
        if current is None:
            return
        value = "\n".join(buffer).strip()
        if value and value != NO_RESPONSE:
            sections.setdefault(current, []).append(value)

    for line in _normalize_newlines(body).split("\n"):
        fence_match = _FENCE_RE.match(line)
        if fence is None:
            if fence_match:
                fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            else:
                heading_match = _HEADING_RE.match(line)
                if heading_match:
                    flush()
                    heading = heading_match.group(1).strip()
                    current = heading if heading in KNOWN_SECTIONS else None
                    buffer = []
                    continue
        elif (
            fence_match
            and fence_match.group(1)[0] == fence[0]
            and len(fence_match.group(1)) >= fence[1]
            and not fence_match.group(2).strip()
        ):
            fence = None
        if current is not None:
            buffer.append(line)
    flush()
    return {key: tuple(values) for key, values in sections.items()}


def render_issue_body(fields: Mapping[str, str | None], *, include_empty: bool = True) -> str:
    """Render fields as issue-form style ``### <Section>`` blocks in canonical order."""
    unknown = sorted(set(fields) - set(KNOWN_SECTIONS))
    if unknown:
        raise ValueError(f"unknown issue sections: {', '.join(unknown)}")
    blocks: list[str] = []
    for section in KNOWN_SECTIONS:
        value = _normalize_newlines(fields.get(section) or "").strip()
        if _H3_LINE_RE.search(value):
            raise ValueError("section values must not contain H3 headings")
        if not value:
            if not include_empty:
                continue
            value = NO_RESPONSE
        blocks.append(f"### {section}\n\n{value}\n")
    return "\n".join(blocks)


def _normalize_body(body: str) -> str:
    lines = [line.rstrip() for line in _normalize_newlines(body).split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def canonical_issue_snapshot(repo: str, number: int, title: str, body: str) -> str:
    """Produce the exact byte layout hashed into ``entry_hash``."""
    if not isinstance(number, int) or isinstance(number, bool) or number < 0:
        raise SnapshotFormatError("issue number must be a non-negative integer")
    if any(char in value for value in (repo, title) for char in "\r\n"):
        raise SnapshotFormatError("repo and title must be single-line")
    return (
        f"{SNAPSHOT_HEADER}\nrepo: {repo}\nnumber: {number}\n"
        f"title: {title.strip()}\n\n{_normalize_body(body)}\n"
    )


def split_canonical_snapshot(snapshot: str) -> tuple[str, int, str, str]:
    """Exact inverse of :func:`canonical_issue_snapshot`."""
    if not snapshot.endswith("\n"):
        raise SnapshotFormatError("snapshot must end with a newline")
    parts = snapshot[:-1].split("\n", 5)
    if len(parts) < 5 or parts[0] != SNAPSHOT_HEADER or parts[4] != "":
        raise SnapshotFormatError("malformed snapshot header")
    fields: list[str] = []
    for line, prefix in zip(parts[1:4], ("repo: ", "number: ", "title: ")):
        if not line.startswith(prefix):
            raise SnapshotFormatError(f"expected {prefix.strip()} field")
        fields.append(line[len(prefix):])
    if _SNAPSHOT_NUMBER_RE.fullmatch(fields[1]) is None:
        raise SnapshotFormatError("issue number must be an integer")
    body = parts[5] if len(parts) == 6 else ""
    return fields[0], int(fields[1]), fields[2], body


def snapshot_hash(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssueTodo:
    """One GitHub issue viewed as a TODO entry plus its extracted fields."""

    number: int
    todo_id: str
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    url: str
    repo: str
    blocked_by_open: int | None
    spec: str | None
    references: tuple[str, ...]
    plan_values: tuple[str, ...]
    branch_values: tuple[str, ...]
    snapshot: str
    entry_hash: str
    state_reason: str | None = None


def first_lines(values: tuple[str, ...]) -> tuple[str, ...]:
    """First non-empty line of each section value; empty values are dropped."""
    result: list[str] = []
    for value in values:
        first = next((line.strip() for line in value.split("\n") if line.strip()), "")
        if first:
            result.append(first)
    return tuple(result)


def issue_from_api(payload: Mapping, *, repo: str) -> IssueTodo:
    """Map one REST issue JSON object onto :class:`IssueTodo`."""
    if "pull_request" in payload:
        raise NotAnIssueError(f"#{payload.get('number')} is a pull request")
    for field in ("number", "title"):
        if payload.get(field) is None:
            raise ValueError(f"issue payload missing {field}")
    if not isinstance(payload["number"], int) or isinstance(payload["number"], bool):
        raise ValueError("issue number must be an integer")
    if not isinstance(payload["title"], str):
        raise ValueError("issue title must be a string")
    labels = payload.get("labels")
    if labels is not None and not isinstance(labels, list):
        raise ValueError("issue labels must be a list")
    number = payload["number"]
    title = payload["title"].strip()
    body = payload.get("body") or ""
    summary = payload.get("issue_dependencies_summary")
    blocked_by_open: int | None = None
    if isinstance(summary, Mapping) and summary.get("blocked_by") is not None:
        blocked_by = summary["blocked_by"]
        if not isinstance(blocked_by, int) or isinstance(blocked_by, bool):
            raise ValueError("blocked_by must be an integer")
        blocked_by_open = blocked_by
    sections = parse_issue_body(body)
    spec_values = first_lines(sections.get("Spec", ()))
    references = tuple(
        item.strip()
        for value in sections.get("Reference", ())
        for item in value.split(",")
        if item.strip()
    )
    snapshot = canonical_issue_snapshot(repo, number, title, body)
    return IssueTodo(
        number=number,
        todo_id=f"TODO-{number}",
        title=title,
        body=body,
        state=str(payload.get("state") or ""),
        labels=tuple(
            label if isinstance(label, str) else label["name"] for label in labels or ()
        ),
        assignees=tuple(user["login"] for user in payload.get("assignees") or ()),
        url=str(payload.get("html_url") or ""),
        repo=repo,
        blocked_by_open=blocked_by_open,
        spec=spec_values[0] if spec_values else None,
        references=references,
        plan_values=first_lines(sections.get("Plan", ())),
        branch_values=first_lines(sections.get("Branch", ())),
        snapshot=snapshot,
        entry_hash=snapshot_hash(snapshot),
        state_reason=(
            str(payload["state_reason"]) if payload.get("state_reason") is not None else None
        ),
    )


@dataclass(frozen=True)
class EligibleTodo:
    entry: IssueTodo
    plan_path: str | None
    plan_kind: Literal["manifest", "legacy"] | None


@dataclass(frozen=True)
class EligibilityResult:
    candidates: tuple[EligibleTodo, ...]
    blocked_reasons: dict[str, str]

    @property
    def todo_ids(self) -> frozenset[str]:
        return frozenset(candidate.entry.todo_id for candidate in self.candidates)

    @property
    def selection_markdown(self) -> str:
        return render_selection_markdown(self.candidates)


def _status_reason(
    issue: IssueTodo,
    *,
    in_flight: Collection[str],
    active_registration_ids: Collection[int],
    kanban_available: bool,
) -> str | None:
    if issue.state != "open":
        return "status_closed"
    if ON_HOLD_LABEL in issue.labels:
        return "status_on_hold"
    triage = next((label for label in TRIAGE_PENDING_LABELS if label in issue.labels), None)
    if triage is not None:
        return f"triage_pending:{triage}"
    # An active registration is ownership proof, whatever the labels say.
    if issue.number in active_registration_ids:
        return "in_flight"
    if READY_LABEL not in issue.labels:
        return "not_ready"
    if issue.todo_id in in_flight:
        return "in_flight"
    if IN_PROGRESS_LABEL in issue.labels:
        if not kanban_available:
            return "in_progress_unverified"
        if issue.number in active_registration_ids:
            return "in_flight"
        return "in_progress_stale"
    if issue.blocked_by_open is None:
        return "dependency_unknown"
    if issue.blocked_by_open > 0:
        return f"dependency_incomplete:{issue.blocked_by_open}"
    return None


def _is_canonical_relative_path(value: str) -> bool:
    """Reject control characters, ``./``, ``//``, trailing ``/`` and ``.``/``..`` segments."""
    if any(ord(char) < 0x20 or char == "\x7f" for char in value):
        return False
    if value.startswith("./") or "//" in value or value.endswith("/"):
        return False
    return all(segment not in (".", "..") for segment in value.split("/"))


def compile_eligible_issues(
    project_dir: Path,
    issues: Iterable[IssueTodo],
    *,
    in_flight: Collection[str],
    active_registration_ids: Collection[int] = (),
    kanban_available: bool = True,
    requires_plan: bool,
) -> EligibilityResult:
    """Compile the deterministic candidate set from already-fetched issues."""
    if any(not isinstance(item, int) for item in active_registration_ids):
        raise TypeError("active_registration_ids must contain issue numbers (int)")
    candidates: list[EligibleTodo] = []
    blocked: dict[str, str] = {}
    for issue in sorted(issues, key=lambda item: item.number):
        reason = _status_reason(
            issue,
            in_flight=in_flight,
            active_registration_ids=active_registration_ids,
            kanban_available=kanban_available,
        )
        plan_path: str | None = None
        plan_kind: Literal["manifest", "legacy"] | None = None
        if reason is None and requires_plan:
            if len(issue.plan_values) != 1:
                reason = "plan_invalid:missing" if len(issue.plan_values) < 2 else "plan_invalid:duplicate"
            elif not _is_canonical_relative_path(issue.plan_values[0]):
                reason = "plan_invalid:non_canonical"
            else:
                try:
                    plan_path = validate_plan_path(project_dir, issue.plan_values[0])
                    manifest = validate_plan_candidate(
                        project_dir, plan_path, expected_todo_id=issue.todo_id
                    )
                    plan_kind = "manifest" if manifest is not None else "legacy"
                except (TodoPlanValidationError, PlanManifestValidationError) as exc:
                    plan_path = None
                    reason = f"plan_invalid:{exc.code}"
                except (OSError, UnicodeError, ValueError):
                    plan_path = None
                    reason = "plan_invalid:unreadable"
        if reason is None and len(issue.branch_values) != 1:
            reason = "branch_invalid"
        if reason is not None:
            blocked[issue.todo_id] = reason
        else:
            candidates.append(EligibleTodo(issue, plan_path, plan_kind))
    return EligibilityResult(tuple(candidates), blocked)


def escape_hostile_selection_lines(text: str) -> str:
    """Backslash-escape lines that could forge an entry header or a prompt fence.

    Shared by every selection-markdown renderer so untrusted body text can never
    widen the candidate set or break out of the `<candidate_todos>` fence.
    """
    return "\n".join(
        "\\" + line if _HOSTILE_SELECTION_LINE_RE.match(line) else line
        for line in text.split("\n")
    )


def render_selection_markdown(candidates: Iterable[IssueTodo | EligibleTodo]) -> str:
    """Render candidates as a deterministic markdown checklist for the selector."""
    entries: list[str] = []
    for candidate in candidates:
        issue = candidate.entry if isinstance(candidate, EligibleTodo) else candidate
        labels = ", ".join(issue.labels) if issue.labels else "(none)"
        blockers = "unknown" if issue.blocked_by_open is None else str(issue.blocked_by_open)
        lines = [
            f"- [ ] **{issue.todo_id}: {issue.title}** — #{issue.number} {issue.url}",
            f"  - labels: {labels}",
            f"  - open blockers: {blockers}",
        ]
        body = _normalize_body(issue.body)
        truncated = len(body) > SELECTION_BODY_MAX_CHARS
        if truncated:
            body = body[:SELECTION_BODY_MAX_CHARS].rstrip()
        for line in escape_hostile_selection_lines(body).split("\n") if body else ():
            lines.append(f"  {line}" if line else "")
        if truncated:
            lines.append("  … (truncated)")
        entries.append("\n".join(lines) + "\n")
    return "\n".join(entries)


# ---------------------------------------------------------------------------
# gh CLI client
# ---------------------------------------------------------------------------

_REPO_SEGMENT = r"[A-Za-z0-9_.-]+"
_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https?://(?:[^@/\s]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    rf"({_REPO_SEGMENT})/({_REPO_SEGMENT}?)(?:\.git)?/?$"
)
_REPO_RE = re.compile(rf"{_REPO_SEGMENT}/{_REPO_SEGMENT}")
_AUTH_RE = re.compile(r"(?i)gh auth login|HTTP 401|not logged in")
_RATE_LIMIT_RE = re.compile(r"(?i)rate limit exceeded|secondary rate limit|HTTP 429")
_NOT_FOUND_RE = re.compile(r"(?i)HTTP 404|Could not resolve")
_REJECTED_RE = re.compile(r"(?i)HTTP 4(?:00|09|22)|Validation Failed|could not (?:add|remove) label")
_ISSUE_URL_RE = re.compile(
    rf"https://github\.com/({_REPO_SEGMENT}/{_REPO_SEGMENT})/issues/(\d+)/?\s*$"
)
_API_ACCEPT = ("-H", "Accept: application/vnd.github+json")
_LABEL_LIST_LIMIT = 1000
_LIST_TIMEOUT = 180.0


class GitHubIssuesError(RuntimeError):
    """A ``gh`` invocation failed. The message never carries stderr or token text.

    ``partial_stdout`` holds whatever stdout was captured before a timeout;
    ``created`` is set by :func:`ensure_labels` to the names created before failing;
    ``detail`` is a fixed, code-path-chosen human hint (never stderr text) and is
    deliberately kept out of ``str()``; ``stderr_blank`` records that the failed
    process wrote nothing to stderr.
    """

    def __init__(self, code: str, verb: str) -> None:
        super().__init__(f"{code}: gh {verb}")
        self.code = code
        self.verb = verb
        self.partial_stdout: str = ""
        self.returncode: int | None = None
        self.created: tuple[str, ...] = ()
        self.detail: str | None = None
        self.stderr_blank: bool = False


def gh_bin() -> str:
    return os.environ.get(GH_BIN_ENV) or "gh"


def _verb(args: Sequence[str]) -> str:
    """Subcommand words used in error messages: ``api``, ``issue close``, ``label list``."""
    if args[0] in ("issue", "label", "auth") and len(args) > 1:
        return f"{args[0]} {args[1]}"
    return args[0]


def _classify_stderr(stderr: str) -> str:
    if _AUTH_RE.search(stderr):
        return "gh_auth"
    if _RATE_LIMIT_RE.search(stderr):
        return "gh_rate_limited"
    if _NOT_FOUND_RE.search(stderr):
        return "gh_not_found"
    if _REJECTED_RE.search(stderr):
        return "gh_rejected"
    return "gh_unavailable"


def _fail(
    code: str, verb: str, cause: BaseException | None = None, *, detail: str | None = None
) -> GitHubIssuesError:
    log.warning("gh call failed: %s (gh %s)", code, verb)
    error = GitHubIssuesError(code, verb)
    error.detail = detail
    if cause is not None:
        error.__cause__ = cause
    return error


def _issue_number(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("issue number must be a positive integer")
    return value


def _gh(project_dir: Path, args: Sequence[str], *, timeout: float = 60.0) -> str:
    """Run ``gh <args>`` in ``project_dir`` and return stdout, classifying failures."""
    verb = _verb(args)
    try:
        result = _run(
            [gh_bin(), *args],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GH_HOST": "github.com"},
        )
    except FileNotFoundError as exc:
        raise _fail("gh_missing", verb, exc) from exc
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or b""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        error = _fail("gh_unavailable", verb)
        error.partial_stdout = partial
        raise error from None
    except (OSError, UnicodeError) as exc:
        raise _fail("gh_unavailable", verb, exc) from exc
    if result.returncode != 0:
        stderr = result.stderr or ""
        error = _fail(_classify_stderr(stderr), verb)
        error.returncode = result.returncode
        error.stderr_blank = not stderr.strip()
        raise error
    return result.stdout or ""


def _gh_api(project_dir: Path, args: Sequence[str], *, timeout: float = 60.0) -> str:
    return _gh(project_dir, ["api", *_API_ACCEPT, *args], timeout=timeout)


def _decode_json(stdout: str, verb: str, *, empty: object = None) -> object:
    if not stdout.strip():
        if empty is not None:
            return empty
        raise _fail("gh_invalid", verb)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise _fail("gh_invalid", verb, exc) from exc


def _flatten_pages(data: object, verb: str) -> list:
    if not isinstance(data, list):
        raise _fail("gh_invalid", verb)
    items: list = []
    for page in data:
        if not isinstance(page, list):
            raise _fail("gh_invalid", verb)
        items.extend(page)
    return items


def _issue_from_payload(payload: object, *, repo: str, verb: str) -> IssueTodo:
    if not isinstance(payload, Mapping):
        raise _fail("gh_invalid", verb)
    try:
        return issue_from_api(payload, repo=repo)
    except NotAnIssueError as exc:
        raise _fail("not_an_issue", verb, exc) from exc
    except (ValueError, TypeError, KeyError, SnapshotFormatError) as exc:
        raise _fail("gh_invalid", verb, exc) from exc


def _valid_segments(*segments: str) -> bool:
    return all(
        segment not in (".", "..", ".git") and ".." not in segment for segment in segments
    )


def parse_github_remote(url: str) -> str | None:
    """Extract ``owner/repo`` from a github.com remote URL (https or ssh)."""
    match = _GITHUB_REMOTE_RE.match(url.strip())
    if match is None or not _valid_segments(*match.groups()):
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _validate_repo(repo: str) -> str:
    if _REPO_RE.fullmatch(repo) is None or not _valid_segments(*repo.split("/")):
        raise _fail("origin_identity_invalid", "git remote")
    return repo


def repository_identity(project_dir: Path) -> str:
    """Return ``owner/repo`` for the ``origin`` remote of ``project_dir``."""
    verb = "git remote"
    try:
        result = _run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _fail("origin_identity_invalid", verb, exc, detail="git not found") from exc
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise _fail(
            "origin_identity_invalid", verb, exc, detail="git remote get-url origin failed"
        ) from exc
    if result.returncode != 0:
        raise _fail(
            "origin_identity_invalid", verb, detail="no origin remote or not a git repository"
        )
    repo = parse_github_remote(result.stdout or "")
    if repo is None:
        raise _fail(
            "origin_identity_invalid", verb, detail="origin is not a github.com remote"
        )
    return repo


def _repo(project_dir: Path, repo: str | None) -> str:
    if repo is None:
        return repository_identity(project_dir)
    return _validate_repo(repo)


# -- read operations --------------------------------------------------------


def _list_issues(project_dir: Path, repo: str, query: str) -> tuple[IssueTodo, ...]:
    stdout = _gh_api(
        project_dir,
        ["--paginate", "--slurp", f"repos/{repo}/issues?{query}"],
        timeout=_LIST_TIMEOUT,
    )
    data = _decode_json(stdout, "api", empty=[])
    issues = [
        _issue_from_payload(payload, repo=repo, verb="api")
        for payload in _flatten_pages(data, "api")
        if not (isinstance(payload, Mapping) and "pull_request" in payload)
    ]
    return tuple(sorted(issues, key=lambda issue: issue.number))


def find_issues_by_label(
    project_dir: Path, label: str, *, state: str = "all", repo: str | None = None
) -> tuple[IssueTodo, ...]:
    """Issues (any state by default) carrying ``label``, sorted by number; PRs skipped."""
    if state not in ("open", "closed", "all"):
        raise ValueError("state must be one of: open, closed, all")
    repo = _repo(project_dir, repo)
    query = f"state={state}&labels={quote(label, safe='')}&per_page=100"
    return _list_issues(project_dir, repo, query)


def list_todo_issues(project_dir: Path, *, repo: str | None = None) -> tuple[IssueTodo, ...]:
    """All open issues labelled ``tpo:todo``, sorted by number; pull requests skipped."""
    return find_issues_by_label(project_dir, TODO_LABEL, state="open", repo=repo)


def fetch_issue(project_dir: Path, number: int, *, repo: str | None = None) -> IssueTodo:
    number = _issue_number(number)
    repo = _repo(project_dir, repo)
    stdout = _gh_api(project_dir, [f"repos/{repo}/issues/{number}"])
    return _issue_from_payload(_decode_json(stdout, "api"), repo=repo, verb="api")


def check_issue_drift(
    project_dir: Path,
    registration_payload: Mapping,
    *,
    repo: str | None = None,
    live: IssueTodo | None = None,
) -> str | None:
    """Compare a registration's pinned issue hash with the live issue.

    ``live`` is an already-fetched issue; when omitted the issue is fetched here.
    Returns ``None`` when unchanged, else ``"issue_unavailable:<code>"`` (``gh``
    failure or malformed registration), ``"issue_identity_mismatch"``,
    ``"issue_closed"``, ``"issue_on_hold"`` (``tpo:on-hold`` or ``wontfix``), or
    ``"issue_drift"``. Drift covers title and body only; other gating labels are
    evaluated by eligibility, not here.
    """
    number = registration_payload.get("issue_number")
    pinned_hash = registration_payload.get("selected_entry_hash")
    pinned_url = registration_payload.get("issue_url")
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number <= 0
        or not isinstance(pinned_hash, str)
        or not isinstance(pinned_url, str)
    ):
        return "issue_unavailable:registration_invalid"
    if live is None:
        try:
            live = fetch_issue(project_dir, number, repo=repo)
        except GitHubIssuesError as exc:
            return f"issue_unavailable:{exc.code}"
    if live.url.lower() != pinned_url.lower():
        return "issue_identity_mismatch"
    if live.state != "open":
        return "issue_closed"
    if ON_HOLD_LABEL in live.labels or "wontfix" in live.labels:
        return "issue_on_hold"
    if live.entry_hash != pinned_hash:
        return "issue_drift"
    return None


def list_comment_bodies(
    project_dir: Path, number: int, *, repo: str | None = None
) -> tuple[str, ...]:
    number = _issue_number(number)
    repo = _repo(project_dir, repo)
    stdout = _gh_api(
        project_dir,
        ["--paginate", "--slurp", f"repos/{repo}/issues/{number}/comments"],
        timeout=_LIST_TIMEOUT,
    )
    bodies: list[str] = []
    for comment in _flatten_pages(_decode_json(stdout, "api", empty=[]), "api"):
        if not isinstance(comment, Mapping):
            raise _fail("gh_invalid", "api")
        bodies.append(str(comment.get("body") or ""))
    return tuple(bodies)


def check_auth(project_dir: Path) -> None:
    """Raise ``gh_auth`` unless ``gh`` is authenticated against github.com.

    A failure is reported as ``gh_auth`` only when stderr matches the auth
    patterns or the process exited nonzero with empty stderr (``gh auth status``
    reports the missing login on stdout). Every other classification
    (``gh_missing``, ``gh_rate_limited``, and ``gh_unavailable`` for timeouts,
    network or 5xx text, or any other unrecognized stderr) passes through unchanged.
    """
    try:
        _gh(project_dir, ["auth", "status", "--hostname", "github.com"])
    except GitHubIssuesError as exc:
        if exc.code == "gh_unavailable" and exc.returncode is not None and exc.stderr_blank:
            raise GitHubIssuesError("gh_auth", "auth status") from exc
        raise


def list_labels(project_dir: Path, *, repo: str | None = None) -> frozenset[str]:
    repo = _repo(project_dir, repo)
    verb = "label list"
    args = ["label", "list", "--repo", repo, "--json", "name", "--limit", str(_LABEL_LIST_LIMIT)]
    data = _decode_json(_gh(project_dir, args), verb, empty=[])
    if not isinstance(data, list) or not all(
        isinstance(item, Mapping) and isinstance(item.get("name"), str) for item in data
    ):
        raise _fail("gh_invalid", verb)
    if len(data) >= _LABEL_LIST_LIMIT:
        raise _fail("gh_truncated", verb)  # listing capped at the limit
    return frozenset(item["name"] for item in data)


# -- write operations -------------------------------------------------------


def add_label(project_dir: Path, number: int, label: str, *, repo: str | None = None) -> None:
    number = _issue_number(number)
    repo = _repo(project_dir, repo)
    _gh(project_dir, ["issue", "edit", str(number), "--repo", repo, "--add-label", label])


def remove_label(project_dir: Path, number: int, label: str, *, repo: str | None = None) -> None:
    number = _issue_number(number)
    repo = _repo(project_dir, repo)
    _gh(project_dir, ["issue", "edit", str(number), "--repo", repo, "--remove-label", label])


def _check_body(body: str) -> str:
    if len(body) > MAX_ISSUE_BODY_CHARS:
        raise ValueError(f"issue body exceeds {MAX_ISSUE_BODY_CHARS} characters")
    return body


def _run_with_body_file(project_dir: Path, body: str, build_args) -> str:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", prefix="tpo-issue-", delete=True
    ) as handle:
        handle.write(body)
        handle.flush()
        return _gh(project_dir, build_args(handle.name))


def add_comment(project_dir: Path, number: int, body: str, *, repo: str | None = None) -> None:
    number = _issue_number(number)
    body = _check_body(body)
    repo = _repo(project_dir, repo)
    _run_with_body_file(
        project_dir,
        body,
        lambda path: ["issue", "comment", str(number), "--repo", repo, "--body-file", path],
    )


def close_issue(project_dir: Path, number: int, *, repo: str | None = None) -> None:
    """Close as completed. Idempotent because ``gh`` exits 0 (with a stderr
    warning) when the issue is already closed."""
    number = _issue_number(number)
    repo = _repo(project_dir, repo)
    _gh(project_dir, ["issue", "close", str(number), "--repo", repo, "--reason", "completed"])


def ensure_labels(
    project_dir: Path,
    *,
    repo: str | None = None,
    extra: Iterable[tuple[str, str, str]] = (),
) -> tuple[str, ...]:
    """Create any missing labels from ``LABEL_VOCABULARY`` + ``extra``; return names created.

    Existing names are compared case-insensitively (GitHub labels are). Any
    create failure (including ``gh_rejected``) re-raises with the names created so
    far attached as ``exc.created``.
    """
    repo = _repo(project_dir, repo)
    existing = {name.lower() for name in list_labels(project_dir, repo=repo)}
    created: list[str] = []
    for name, color, description in (*LABEL_VOCABULARY, *extra):
        if name.lower() in existing:
            continue
        try:
            # gh (cobra/pflag) treats everything after "--" as positional, so all
            # flags must precede it and "--" must sit immediately before the name.
            # This keeps a label such as "--repo" from being parsed as a flag.
            _gh(
                project_dir,
                ["label", "create", "--repo", repo, "--color", color,
                 "--description", description, "--force", "--", name],
            )
        except GitHubIssuesError as exc:
            exc.created = tuple(sorted(created))
            raise
        created.append(name)
        existing.add(name.lower())
    return tuple(sorted(created))


def _issue_number_from_url(stdout: str, repo: str) -> int | None:
    match = _ISSUE_URL_RE.search(stdout)
    if match is None or match.group(1).lower() != repo.lower():
        return None
    return int(match.group(2))


def create_issue(
    project_dir: Path,
    *,
    title: str,
    body: str,
    labels: Sequence[str],
    repo: str | None = None,
) -> int:
    """Create an issue and return its number parsed from the printed URL.

    If ``gh`` times out after printing the URL, the number is recovered from the
    partial stdout so the caller does not create a duplicate.
    """
    body = _check_body(body)
    repo = _repo(project_dir, repo)
    verb = "issue create"

    def build_args(path: str) -> list[str]:
        args = ["issue", "create", "--repo", repo, "--title", title, "--body-file", path]
        for label in labels:
            args.extend(["--label", label])
        return args

    try:
        stdout = _run_with_body_file(project_dir, body, build_args)
    except GitHubIssuesError as exc:
        if exc.code == "gh_unavailable" and exc.partial_stdout:
            recovered = _issue_number_from_url(exc.partial_stdout, repo)
            if recovered is not None:
                return recovered
        raise
    number = _issue_number_from_url(stdout, repo)
    if number is None:
        raise _fail("gh_invalid", verb)
    return number


def add_blocked_by(
    project_dir: Path, number: int, blocker_number: int, *, repo: str | None = None
) -> None:
    """Record that ``number`` is blocked by ``blocker_number`` via the dependencies API.

    A ``gh_rejected`` POST (edge already present) is treated as success.
    """
    number = _issue_number(number)
    blocker_number = _issue_number(blocker_number)
    repo = _repo(project_dir, repo)
    payload = _decode_json(_gh_api(project_dir, [f"repos/{repo}/issues/{blocker_number}"]), "api")
    if not isinstance(payload, Mapping):
        raise _fail("gh_invalid", "api")
    if "pull_request" in payload:
        raise _fail("not_an_issue", "api")
    blocker_id = payload.get("id")
    if not isinstance(blocker_id, int) or isinstance(blocker_id, bool):
        raise _fail("gh_invalid", "api")
    try:
        _gh_api(
            project_dir,
            ["--method", "POST", f"repos/{repo}/issues/{number}/dependencies/blocked_by",
             "-F", f"issue_id={blocker_id}"],
        )
    except GitHubIssuesError as exc:
        if exc.code != "gh_rejected":
            raise
