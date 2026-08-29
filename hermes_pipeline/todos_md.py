"""Standalone TODOS.md attachment field extraction and Plan validation.

Not a full markdown parser, and not a reuse of the todos-manager skill's
prose-based logic (which isn't Python-importable, being an LLM-facing
skill). Scans only the sub-bullet block belonging to the requested
todo_id, anchored between its entry header and the next entry header
(or EOF), so a naive regex cannot bleed into a neighboring entry's
fields.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .plan_manifest import (
    PlanManifestValidationError,
    TodoPlanValidationError,
    validate_plan_candidate,
    validate_plan_path,
)

log = logging.getLogger(__name__)

_ENTRY_HEADER_RE = re.compile(
    r"^- \[(?P<status>[ x→~])\] \*\*(?P<todo_id>TODO-\d+):\s*"
    r"(?P<title>[^*\n]+?)\*\*(?:[^\n]*)$",
    re.MULTILINE,
)
_SPEC_RE = re.compile(r"^\s*-\s*\*\*Spec:\*\*[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_REFERENCE_RE = re.compile(r"^\s*-\s*\*\*Reference:\*\*[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_PLAN_RE = re.compile(r"^\s*-\s*\*\*Plan:\*\*[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_BRANCH_RE = re.compile(r"^\s*-\s*\*\*Branch:\*\*[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_DEPENDS_RE = re.compile(
    r"^\s*-\s*\*\*Depends on:\*\*[ \t]*(.*?)[ \t]*$", re.MULTILINE
)
_DEPENDENCY_LIST_RE = re.compile(
    r"`TODO-\d+`(?:[ \t]+\([^()\r\n]+\))?"
    r"(?:[ \t]*,[ \t]*`TODO-\d+`(?:[ \t]+\([^()\r\n]+\))?)*"
)
_BACKTICKED_TODO_ID_RE = re.compile(r"`(TODO-\d+)`")
_ENTRIES_HEADING_RE = re.compile(r"^## Entries[ \t]*$", re.MULTILINE)
_TOP_LEVEL_SECTION_RE = re.compile(r"^## [^\n]+$", re.MULTILINE)

_EMPTY_RESULT = {"spec": None, "references": []}


@dataclass(frozen=True)
class TodoEntry:
    """One canonical top-level TODOS.md entry and its deterministic fields."""

    todo_id: str
    status: str
    title: str
    raw: str
    spec: str | None
    references: tuple[str, ...]
    dependencies: tuple[str, ...] | None
    plan_values: tuple[str, ...]
    branch_values: tuple[str, ...]


@dataclass(frozen=True)
class EligibleTodo:
    entry: TodoEntry
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
        return "\n".join(candidate.entry.raw.rstrip() for candidate in self.candidates)


def parse_todo_entries(text: str) -> tuple[TodoEntry, ...]:
    """Parse canonical top-level entries without treating body mentions as entries."""
    section_match = _ENTRIES_HEADING_RE.search(text)
    if section_match:
        following_section = _TOP_LEVEL_SECTION_RE.search(text, section_match.end())
        scope_start = section_match.end()
        scope_end = following_section.start() if following_section else len(text)
        scope = text[scope_start:scope_end]
    else:
        scope = text
    lines = scope.splitlines(keepends=True)
    entries: list[TodoEntry] = []
    index = 0
    while index < len(lines):
        header = _ENTRY_HEADER_RE.fullmatch(lines[index].rstrip("\r\n"))
        if header is None:
            index += 1
            continue
        body_lines: list[str] = []
        index += 1
        while index < len(lines):
            line = lines[index]
            if _ENTRY_HEADER_RE.fullmatch(line.rstrip("\r\n")):
                break
            if line.startswith("  - ") or not line.strip():
                body_lines.append(line)
                index += 1
                continue
            break
        body = "".join(body_lines)
        raw = lines[index - len(body_lines) - 1] + body
        spec_match = _SPEC_RE.search(body)
        spec = spec_match.group(1).strip() or None if spec_match else None
        reference_match = _REFERENCE_RE.search(body)
        references = (
            tuple(
                value.strip()
                for value in reference_match.group(1).split(",")
                if value.strip()
            )
            if reference_match
            else ()
        )
        dependency_matches = _DEPENDS_RE.findall(body)
        dependencies: tuple[str, ...] | None = ()
        if len(dependency_matches) > 1:
            dependencies = None
        elif dependency_matches:
            value = dependency_matches[0].strip()
            if value == "(none)":
                dependencies = ()
            elif _DEPENDENCY_LIST_RE.fullmatch(value):
                dependencies = tuple(_BACKTICKED_TODO_ID_RE.findall(value))
            else:
                dependencies = None
        entries.append(
            TodoEntry(
                todo_id=header.group("todo_id"),
                status=header.group("status"),
                title=header.group("title").strip(),
                raw=raw,
                spec=spec,
                references=references,
                dependencies=dependencies,
                plan_values=tuple(value.strip() for value in _PLAN_RE.findall(body)),
                branch_values=tuple(
                    value.strip() for value in _BRANCH_RE.findall(body)
                ),
            )
        )
    return tuple(entries)


def todo_entry_ids(text: str) -> frozenset[str]:
    """Return only IDs from canonical entry headers."""
    return frozenset(entry.todo_id for entry in parse_todo_entries(text))


class TodoCompletionError(ValueError):
    """The requested deterministic TODO completion is not safe."""


def complete_todo_text(text: str, todo_id: str, *, pr_number: int, date: str) -> str:
    """Return canonical TODOS.md text with one entry atomically marked complete."""
    if not re.fullmatch(r"TODO-\d+", todo_id) or pr_number < 1:
        raise TodoCompletionError("invalid completion identity")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise TodoCompletionError("invalid completion date")
    try:
        dt.date.fromisoformat(date)
    except ValueError as exc:
        raise TodoCompletionError("invalid completion date") from exc
    entries = parse_todo_entries(text)
    matches = [entry for entry in entries if entry.todo_id == todo_id]
    if len(matches) != 1:
        raise TodoCompletionError("TODO entry not found or duplicated")
    entry = matches[0]
    expected = f"  - **Completed:** PR #{pr_number}, {date}\n"
    existing = re.findall(r"(?m)^  - \*\*Completed:\*\*.*(?:\n|$)", entry.raw)
    if entry.status == "x" and existing == [expected]:
        return text
    if entry.status not in {" ", "→"} or existing:
        raise TodoCompletionError("TODO completion state conflicts")
    completed = re.sub(r"^- \[[ →]\]", "- [x]", entry.raw, count=1)
    completed = completed.rstrip("\r\n") + "\n" + expected
    start = text.find(entry.raw)
    if start < 0:
        raise TodoCompletionError("TODO entry bytes not found")
    return text[:start] + completed + text[start + len(entry.raw):]


def complete_todo_file(path: Path, todo_id: str, *, pr_number: int, date: str) -> bool:
    """Replace TODOS.md durably; return False when already exactly complete."""
    lock_path = path.parent / ".hermes" / "todos.write.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        original = path.read_text(encoding="utf-8")
        updated = complete_todo_text(original, todo_id, pr_number=pr_number, date=date)
        if updated == original:
            return False
        mode = path.stat().st_mode & 0o777
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return True
    finally:
        os.close(lock_fd)


def compile_eligible_todos(
    project_dir: Path,
    todos_md_path: Path,
    *,
    in_flight: set[str] | frozenset[str],
    requires_plan: bool,
) -> EligibilityResult:
    """Compile the exact deterministic candidate set for one selection call."""
    text = todos_md_path.read_text(encoding="utf-8")
    entries = parse_todo_entries(text)
    known = {entry.todo_id: entry for entry in entries}
    archive_path = project_dir / "TODOS-archive.md"
    archived = (
        todo_entry_ids(archive_path.read_text(encoding="utf-8"))
        if archive_path.is_file()
        else frozenset()
    )
    satisfied = archived | frozenset(
        entry.todo_id for entry in entries if entry.status == "x"
    )
    candidates: list[EligibleTodo] = []
    blocked: dict[str, str] = {}
    for entry in entries:
        reason: str | None = None
        if entry.status == "x":
            reason = "status_complete"
        elif entry.status == "~":
            reason = "status_on_hold"
        elif entry.todo_id in in_flight:
            reason = "in_flight"
        elif entry.dependencies is None:
            reason = "dependency_invalid"
        else:
            missing = next(
                (dependency for dependency in entry.dependencies if dependency not in known and dependency not in archived),
                None,
            )
            blocked_dependency = next(
                (dependency for dependency in entry.dependencies if dependency not in satisfied),
                None,
            )
            if missing is not None:
                reason = f"dependency_missing:{missing}"
            elif blocked_dependency is not None:
                reason = f"dependency_incomplete:{blocked_dependency}"

        plan_path: str | None = None
        plan_kind: Literal["manifest", "legacy"] | None = None
        if reason is None and requires_plan:
            if len(entry.plan_values) != 1 or not entry.plan_values[0]:
                reason = "plan_invalid:missing" if len(entry.plan_values) < 2 else "plan_invalid:duplicate"
            else:
                plan_path = entry.plan_values[0]
                try:
                    manifest = validate_plan_candidate(
                        project_dir, plan_path, expected_todo_id=entry.todo_id
                    )
                    plan_kind = "manifest" if manifest is not None else "legacy"
                except (TodoPlanValidationError, PlanManifestValidationError) as exc:
                    reason = f"plan_invalid:{exc.code}"
                except (OSError, UnicodeError):
                    reason = "plan_invalid:unreadable"
        if reason is not None:
            blocked[entry.todo_id] = reason
        else:
            candidates.append(EligibleTodo(entry, plan_path, plan_kind))
    return EligibilityResult(tuple(candidates), blocked)
def find_todo_fields(todos_md_path: Path, todo_id: str) -> dict:
    """Locate the TODO-<n> entry in todos_md_path and extract Spec:/Reference:.

    Returns {"spec": str | None, "references": list[str]}.
    Never raises for parsing problems — missing file, missing todo_id, or
    a malformed entry all degrade to the empty/partial result.
    """
    try:
        text = todos_md_path.read_text()
    except (FileNotFoundError, OSError) as e:
        log.warning("todos_md: could not read %s: %s", todos_md_path, e)
        return dict(_EMPTY_RESULT)

    try:
        return _extract(text, todo_id)
    except Exception as e:  # pragma: no cover - defense in depth
        log.warning("todos_md: failed to parse entry for %s: %s", todo_id, e)
        return dict(_EMPTY_RESULT)


def _extract(text: str, todo_id: str) -> dict:
    entry = next((item for item in parse_todo_entries(text) if item.todo_id == todo_id), None)
    if entry is None:
        return dict(_EMPTY_RESULT)
    return {"spec": entry.spec, "references": list(entry.references)}


def _entry_block(text: str, todo_id: str) -> str | None:
    entry = next((item for item in parse_todo_entries(text) if item.todo_id == todo_id), None)
    if entry is None:
        return None
    return entry.raw.partition("\n")[2]


def resolve_todo_plan(project_dir: Path, todos_md_path: Path, todo_id: str) -> str:
    """Return one validated repository-relative Plan path for ``todo_id``."""
    try:
        text = todos_md_path.read_text()
    except (FileNotFoundError, OSError) as exc:
        raise TodoPlanValidationError("missing") from exc

    block = _entry_block(text, todo_id)
    if block is None:
        raise TodoPlanValidationError("missing")
    matches = [value.strip() for value in _PLAN_RE.findall(block)]
    if len(matches) > 1:
        raise TodoPlanValidationError("duplicate")
    if not matches or not matches[0]:
        raise TodoPlanValidationError("missing")

    return validate_plan_path(project_dir, matches[0])
