"""Pure GitHub Issues TODO contract: labels, body sections, snapshots, eligibility.

No subprocess or network access lives here. Callers fetch REST payloads and
feed them through :func:`issue_from_api`; everything else is deterministic.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .plan_manifest import (
    PlanManifestValidationError,
    TodoPlanValidationError,
    validate_plan_candidate,
)

TODO_LABEL = "tpo:todo"
READY_LABEL = "ready-for-agent"
ON_HOLD_LABEL = "tpo:on-hold"
IN_PROGRESS_LABEL = "tpo:in-progress"
TRIAGE_PENDING_LABELS = ("needs-triage", "needs-info", "ready-for-human", "wontfix")
MAX_ISSUE_BODY_CHARS = 65_536
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

LABEL_VOCABULARY: tuple[tuple[str, str, str], ...] = (
    (TODO_LABEL, "0e8a16", "Managed TODO entry for todo-pipeline-orchestrator"),
    (ON_HOLD_LABEL, "fbca04", "TODO is paused and must not be selected"),
    (IN_PROGRESS_LABEL, "1d76db", "TODO is claimed by an active pipeline run"),
    ("needs-triage", "ededed", "Awaiting triage"),
    ("needs-info", "d4c5f9", "Blocked on missing information"),
    (READY_LABEL, "0052cc", "Ready for an agent to pick up"),
    ("ready-for-human", "5319e7", "Needs a human to pick up"),
    ("wontfix", "ffffff", "Will not be worked on"),
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
)

_HEADING_RE = re.compile(r"^### (.+?)\s*$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
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


def phase_label(phase_value: str) -> str:
    """Map a free-form Phase value (``"4 (Development)"``) to ``phase:4-development``."""
    slug = _SLUG_RE.sub("-", phase_value.strip().lower()).strip("-")
    return f"phase:{slug}"


def legacy_id_label(todo_id: str) -> str:
    return f"legacy-id:{todo_id}"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_issue_body(body: str) -> dict[str, tuple[str, ...]]:
    """Split an issue body on ``### <Section>`` headings into known sections."""
    text = _normalize_newlines(body)
    matches = list(_HEADING_RE.finditer(text))
    sections: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if heading not in KNOWN_SECTIONS:
            continue
        value = text[match.end():end].strip()
        if not value or value == NO_RESPONSE:
            continue
        sections.setdefault(heading, []).append(value)
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


def _first_lines(values: tuple[str, ...]) -> tuple[str, ...]:
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
    number = int(payload["number"])
    title = str(payload["title"]).strip()
    body = payload.get("body") or ""
    summary = payload.get("issue_dependencies_summary")
    blocked_by_open: int | None = None
    if isinstance(summary, Mapping) and summary.get("blocked_by") is not None:
        blocked_by_open = int(summary["blocked_by"])
    sections = parse_issue_body(body)
    spec_values = _first_lines(sections.get("Spec", ()))
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
            label if isinstance(label, str) else label["name"]
            for label in payload.get("labels") or ()
        ),
        assignees=tuple(user["login"] for user in payload.get("assignees") or ()),
        url=str(payload.get("html_url") or ""),
        repo=repo,
        blocked_by_open=blocked_by_open,
        spec=spec_values[0] if spec_values else None,
        references=references,
        plan_values=_first_lines(sections.get("Plan", ())),
        branch_values=_first_lines(sections.get("Branch", ())),
        snapshot=snapshot,
        entry_hash=snapshot_hash(snapshot),
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
            else:
                plan_path = issue.plan_values[0]
                try:
                    manifest = validate_plan_candidate(
                        project_dir, plan_path, expected_todo_id=issue.todo_id
                    )
                    plan_kind = "manifest" if manifest is not None else "legacy"
                except (TodoPlanValidationError, PlanManifestValidationError) as exc:
                    reason = f"plan_invalid:{exc.code}"
                except (OSError, UnicodeError):
                    reason = "plan_invalid:unreadable"
        if reason is None and len(issue.branch_values) != 1:
            reason = "branch_invalid"
        if reason is not None:
            blocked[issue.todo_id] = reason
        else:
            candidates.append(EligibleTodo(issue, plan_path, plan_kind))
    return EligibilityResult(tuple(candidates), blocked)


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
        for line in body.split("\n") if body else ():
            if _HOSTILE_SELECTION_LINE_RE.match(line):
                line = "\\" + line
            lines.append(f"  {line}" if line else "")
        if truncated:
            lines.append("  … (truncated)")
        entries.append("\n".join(lines) + "\n")
    return "\n".join(entries)
