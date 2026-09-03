"""Strict parsing for the optional machine-readable block in a Plan document."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_PLAN_TASKS = 50
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TASK_ID_LENGTH = 64
MAX_TITLE_LENGTH = 256
MAX_INSTRUCTIONS_LENGTH = 10_000
MAX_CRITERION_LENGTH = 2_000
MAX_VERIFICATION_LENGTH = 500
MAX_COMMIT_MESSAGE_LENGTH = 256
MAX_LIST_ITEMS = 50
MAX_EMBEDDED_PLAN_CHARS = 65_536

EMBEDDED_PLAN_OPEN = "<details>\n<summary>Implementation Plan</summary>\n---\n"
EMBEDDED_PLAN_CLOSE = "---\n</details>\n"
_EMBEDDED_SUMMARY = "<summary>Implementation Plan</summary>"

_MANIFEST_RE = re.compile(
    r"^ {0,3}```json tpo-plan[ \t]*\r?\n(.*?)^ {0,3}```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_MANIFEST_START_RE = re.compile(r"^ {0,3}```json tpo-plan[ \t]*$", re.MULTILINE)
_PSEUDO_MANIFEST_START_RE = re.compile(
    r"^[ \t]*```json tpo-plan[ \t]*$", re.MULTILINE
)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_MANIFEST_KEYS = frozenset({"schema_version", "todo_id", "tasks"})
_TASK_KEYS = frozenset(
    {
        "id",
        "title",
        "instructions",
        "acceptance_criteria",
        "verification",
        "commit_message",
    }
)


class TodoPlanValidationError(ValueError):
    """A selected TODO does not have one safe, readable Plan attachment."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PlanManifestValidationError(ValueError):
    """A Plan's embedded manifest violates the public schema contract."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PlanTask:
    id: str
    title: str
    instructions: str
    acceptance_criteria: tuple[str, ...]
    verification: tuple[str, ...]
    commit_message: str


@dataclass(frozen=True)
class PlanManifest:
    schema_version: int
    todo_id: str
    tasks: tuple[PlanTask, ...]


@dataclass(frozen=True)
class PlanSource:
    """Resolved execution authority from an issue snapshot or legacy path."""

    kind: Literal["embedded", "legacy_path"]
    document: str
    plan_hash: str
    manifest: PlanManifest | None
    plan_path: str | None = None


@dataclass(frozen=True)
class PlanReference:
    """A runtime reference bound to one validated Plan source and digest."""

    value: str
    source: PlanSource


def validate_plan_reference(reference: PlanReference, *, expected_todo_id: str) -> None:
    source = reference.source
    if source.kind == "embedded":
        reference_path = Path(reference.value)
        if (
            source.plan_path is not None
            or not reference_path.is_absolute()
            or reference_path.name != "plan.md"
            or reference_path != reference_path.resolve()
        ):
            raise TodoPlanValidationError("invalid_plan_reference")
        from .run_registration import RunRegistrationError, _read_verified_artifact

        try:
            document = _read_verified_artifact(reference_path, source.plan_hash).decode()
        except (RunRegistrationError, UnicodeError) as exc:
            raise TodoPlanValidationError("invalid_plan_reference") from exc
    elif source.kind == "legacy_path":
        if not source.plan_path or reference.value != source.plan_path:
            raise TodoPlanValidationError("invalid_plan_reference")
        document = source.document
    else:
        raise TodoPlanValidationError("invalid_plan_reference")
    if hashlib.sha256(document.encode()).hexdigest() != source.plan_hash:
        raise TodoPlanValidationError("invalid_plan_reference")
    manifest = parse_plan_manifest(document, expected_todo_id=expected_todo_id)
    if manifest != source.manifest:
        raise TodoPlanValidationError("invalid_plan_reference")


def _normalized_document(document: str) -> str:
    return document.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def render_embedded_plan(document: str, *, expected_todo_id: str) -> str:
    """Render a complete, manifest-bearing Plan as the canonical folded block."""
    normalized = _normalized_document(document)
    if not normalized.strip():
        raise TodoPlanValidationError("empty_embedded_plan")
    if (
        re.search(r"(?i)</?details(?:\s|>)", normalized)
        or re.search(
            r"(?i)<summary\s*>\s*Implementation Plan\s*</summary\s*>", normalized
        )
        or re.search(r"(?i)<!--\s*tpo-create:", normalized)
        or re.search(r"(?i)</?proposed_plan(?:\s|>)", normalized)
    ):
        raise TodoPlanValidationError("forbidden_plan_structure")
    try:
        manifest = parse_plan_manifest(normalized, expected_todo_id=expected_todo_id)
    except PlanManifestValidationError as exc:
        raise TodoPlanValidationError(exc.code) from exc
    if manifest is None:
        raise TodoPlanValidationError("manifest_required")
    return EMBEDDED_PLAN_OPEN + normalized + EMBEDDED_PLAN_CLOSE


def _embedded_structure(body: str) -> tuple[list[int], list[int]]:
    """Return top-level candidate starts and matching-summary offsets."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    starts: list[int] = []
    summaries: list[int] = []
    offset = 0
    fence: tuple[str, int] | None = None
    in_comment = False
    lines = normalized.splitlines(keepends=True)
    for index, line in enumerate(lines):
        plain = line.removesuffix("\n")
        was_comment = in_comment
        if "<!--" in plain and "-->" not in plain.split("<!--", 1)[1]:
            in_comment = True
        if not was_comment and not in_comment:
            fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", plain)
            if fence is None and fence_match:
                fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            elif (
                fence is not None
                and fence_match
                and fence_match.group(1)[0] == fence[0]
                and len(fence_match.group(1)) >= fence[1]
                and not fence_match.group(2).strip()
            ):
                fence = None
            elif fence is None:
                if plain == _EMBEDDED_SUMMARY:
                    summaries.append(offset)
                if (
                    plain == "<details>"
                    and index + 1 < len(lines)
                    and lines[index + 1].removesuffix("\n") == _EMBEDDED_SUMMARY
                ):
                    starts.append(offset)
        if "-->" in plain:
            in_comment = False
        offset += len(line)
    return starts, summaries


def embedded_plan_candidate_start(body: str) -> int | None:
    """Locate the first top-level structural candidate, including malformed ones."""
    starts, _summaries = _embedded_structure(body)
    return starts[0] if starts else None


def extract_embedded_plan(body: str) -> str | None:
    """Extract the sole canonical final embedded Plan block from an issue body."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    starts, summaries = _embedded_structure(normalized)
    if not summaries:
        return None
    if len(summaries) > 1 or len(starts) > 1:
        raise TodoPlanValidationError("duplicate_embedded_plan")
    if len(starts) > 1:
        raise TodoPlanValidationError("duplicate_embedded_plan")
    if not starts:
        raise TodoPlanValidationError("malformed_embedded_plan")
    start = starts[0]
    if not normalized[start:].startswith(EMBEDDED_PLAN_OPEN):
        raise TodoPlanValidationError("malformed_embedded_plan")
    suffix = normalized[start:]
    if not suffix.endswith(EMBEDDED_PLAN_CLOSE):
        if EMBEDDED_PLAN_CLOSE in suffix:
            raise TodoPlanValidationError("misplaced_embedded_plan")
        raise TodoPlanValidationError("malformed_embedded_plan")
    document = suffix[len(EMBEDDED_PLAN_OPEN) : -len(EMBEDDED_PLAN_CLOSE)]
    if not document.strip():
        raise TodoPlanValidationError("empty_embedded_plan")
    if len(document) > MAX_EMBEDDED_PLAN_CHARS:
        raise TodoPlanValidationError("embedded_plan_too_large")
    normalized_document = _normalized_document(document)
    if not _PSEUDO_MANIFEST_START_RE.search(normalized_document):
        raise TodoPlanValidationError("manifest_required")
    return normalized_document


def embedded_plan_source(body: str, *, expected_todo_id: str) -> PlanSource | None:
    document = extract_embedded_plan(body)
    if document is None:
        return None
    try:
        manifest = parse_plan_manifest(document, expected_todo_id=expected_todo_id)
    except PlanManifestValidationError as exc:
        raise TodoPlanValidationError(exc.code) from exc
    if manifest is None:
        raise TodoPlanValidationError("manifest_required")
    return PlanSource(
        kind="embedded",
        document=document,
        plan_hash=hashlib.sha256(document.encode("utf-8")).hexdigest(),
        manifest=manifest,
    )


def _bounded_string(value: object, *, maximum: int, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PlanManifestValidationError(code)
    return value.strip()


def _bounded_string_list(value: object, *, maximum: int, code: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_LIST_ITEMS:
        raise PlanManifestValidationError(code)
    return tuple(_bounded_string(item, maximum=maximum, code=code) for item in value)


def _exact_keys(value: dict[str, object], expected: frozenset[str]) -> None:
    if set(value) != expected:
        raise PlanManifestValidationError("unknown_keys")


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PlanManifestValidationError("duplicate_json_key")
        result[key] = value
    return result


def _parse_task(value: object) -> PlanTask:
    if not isinstance(value, dict):
        raise PlanManifestValidationError("invalid_task")
    _exact_keys(value, _TASK_KEYS)
    task_id = _bounded_string(value["id"], maximum=MAX_TASK_ID_LENGTH, code="invalid_task")
    if _TASK_ID_RE.fullmatch(task_id) is None:
        raise PlanManifestValidationError("unsafe_task_id")
    return PlanTask(
        id=task_id,
        title=_bounded_string(value["title"], maximum=MAX_TITLE_LENGTH, code="invalid_task"),
        instructions=_bounded_string(
            value["instructions"], maximum=MAX_INSTRUCTIONS_LENGTH, code="invalid_task"
        ),
        acceptance_criteria=_bounded_string_list(
            value["acceptance_criteria"], maximum=MAX_CRITERION_LENGTH, code="invalid_task"
        ),
        verification=_bounded_string_list(
            value["verification"], maximum=MAX_VERIFICATION_LENGTH, code="invalid_task"
        ),
        commit_message=_bounded_string(
            value["commit_message"], maximum=MAX_COMMIT_MESSAGE_LENGTH, code="invalid_task"
        ),
    )


def parse_plan_manifest(document: str, *, expected_todo_id: str) -> PlanManifest | None:
    """Parse and validate the sole ``json tpo-plan`` fenced block, if present."""
    opening_markers = _PSEUDO_MANIFEST_START_RE.findall(document)
    if len(opening_markers) > 1:
        raise PlanManifestValidationError("duplicate_manifest")
    if opening_markers and not _MANIFEST_START_RE.search(document):
        raise PlanManifestValidationError("malformed_fence")
    blocks = _MANIFEST_RE.findall(document)
    if not blocks:
        if opening_markers:
            raise PlanManifestValidationError("malformed_fence")
        return None
    encoded = blocks[0].encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise PlanManifestValidationError("manifest_too_large")
    try:
        value = json.loads(blocks[0], object_pairs_hook=_json_object)
    except PlanManifestValidationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise PlanManifestValidationError("invalid_json") from exc
    if not isinstance(value, dict):
        raise PlanManifestValidationError("invalid_manifest")
    _exact_keys(value, _MANIFEST_KEYS)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PlanManifestValidationError("schema_version")
    todo_id = _bounded_string(value["todo_id"], maximum=64, code="invalid_todo_id")
    if todo_id != expected_todo_id:
        raise PlanManifestValidationError("todo_id_mismatch")
    raw_tasks = value["tasks"]
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= MAX_PLAN_TASKS:
        raise PlanManifestValidationError("task_count")
    tasks = tuple(_parse_task(task) for task in raw_tasks)
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise PlanManifestValidationError("duplicate_task_id")
    return PlanManifest(schema_version=1, todo_id=todo_id, tasks=tasks)


def validate_plan_path(project_dir: Path, plan_path: str) -> str:
    """Validate one candidate Plan path without requiring a persisted TODO."""
    raw_path = Path(plan_path)
    if raw_path.is_absolute():
        raise TodoPlanValidationError("absolute")

    root = project_dir.resolve()
    candidate = project_dir / raw_path
    unresolved = candidate.resolve(strict=False)
    try:
        unresolved.relative_to(root)
    except ValueError as exc:
        raise TodoPlanValidationError("outside_repository") from exc
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise TodoPlanValidationError("missing_file") from exc
    except OSError as exc:
        raise TodoPlanValidationError("unreadable") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TodoPlanValidationError("outside_repository") from exc
    if not resolved.is_file():
        raise TodoPlanValidationError("not_regular_file")
    try:
        with open(resolved, "rb"):
            pass
    except OSError as exc:
        raise TodoPlanValidationError("unreadable") from exc

    return raw_path.as_posix()


def validate_plan_candidate(
    project_dir: Path,
    plan_path: str,
    *,
    expected_todo_id: str,
) -> PlanManifest | None:
    """Validate a repository-contained Plan and its optional manifest."""
    relative_plan = validate_plan_path(project_dir, plan_path)
    document = (project_dir / relative_plan).read_text(encoding="utf-8")
    return parse_plan_manifest(document, expected_todo_id=expected_todo_id)


def legacy_plan_source(project_dir: Path, plan_path: str, *, expected_todo_id: str) -> PlanSource:
    relative_plan = validate_plan_path(project_dir, plan_path)
    document = (project_dir / relative_plan).read_text(encoding="utf-8")
    manifest = parse_plan_manifest(document, expected_todo_id=expected_todo_id)
    return PlanSource(
        kind="legacy_path",
        document=document,
        plan_hash=hashlib.sha256(document.encode("utf-8")).hexdigest(),
        manifest=manifest,
        plan_path=relative_plan,
    )
