"""Strict parsing for the optional machine-readable block in a Plan document."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

MAX_PLAN_TASKS = 50
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TASK_ID_LENGTH = 64
MAX_TITLE_LENGTH = 256
MAX_INSTRUCTIONS_LENGTH = 10_000
MAX_CRITERION_LENGTH = 2_000
MAX_VERIFICATION_LENGTH = 500
MAX_COMMIT_MESSAGE_LENGTH = 256
MAX_LIST_ITEMS = 50

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
