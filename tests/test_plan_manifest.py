from __future__ import annotations

import json

import pytest

from hermes_pipeline.plan_manifest import (
    MAX_PLAN_TASKS,
    PlanManifestValidationError,
    parse_plan_manifest,
)


def _manifest(*, todo_id: str = "TODO-42", tasks: list[dict] | None = None, **extra):
    if tasks is None:
        tasks = [
            {
                "id": "task-1",
                "title": "Implement behavior",
                "instructions": "Bounded implementation instructions.",
                "acceptance_criteria": ["Observable criterion"],
                "verification": ["uv run pytest tests/test_example.py"],
                "commit_message": "feat(scope): implement behavior",
            }
        ]
    return {"schema_version": 1, "todo_id": todo_id, "tasks": tasks, **extra}


def _document(data: dict) -> str:
    return f"# Plan\n\n```json tpo-plan\n{json.dumps(data)}\n```\n"


def test_parse_valid_manifest_returns_immutable_types():
    manifest = parse_plan_manifest(_document(_manifest()), expected_todo_id="TODO-42")

    assert manifest is not None
    assert manifest.todo_id == "TODO-42"
    assert manifest.tasks[0].id == "task-1"
    with pytest.raises(AttributeError):
        manifest.todo_id = "TODO-43"


def test_absent_manifest_is_legacy():
    assert parse_plan_manifest("# Human-readable plan\n", expected_todo_id="TODO-42") is None


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (
            _document(_manifest()) + _document(_manifest()),
            "duplicate_manifest",
        ),
        (_document(_manifest(unexpected=True)), "unknown_keys"),
        (_document(_manifest(todo_id="TODO-43")), "todo_id_mismatch"),
    ],
)
def test_rejects_duplicate_unknown_and_mismatched_manifests(document, code):
    with pytest.raises(PlanManifestValidationError, match=code):
        parse_plan_manifest(document, expected_todo_id="TODO-42")


@pytest.mark.parametrize("task_id", ["../task", "task one", "-task", "task/one"])
def test_rejects_unsafe_task_ids(task_id):
    task = _manifest()["tasks"][0] | {"id": task_id}
    with pytest.raises(PlanManifestValidationError, match="unsafe_task_id"):
        parse_plan_manifest(_document(_manifest(tasks=[task])), expected_todo_id="TODO-42")


def test_rejects_duplicate_task_ids():
    task = _manifest()["tasks"][0]
    with pytest.raises(PlanManifestValidationError, match="duplicate_task_id"):
        parse_plan_manifest(_document(_manifest(tasks=[task, task])), expected_todo_id="TODO-42")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", " "),
        ("instructions", ""),
        ("acceptance_criteria", []),
        ("verification", []),
        ("commit_message", ""),
    ],
)
def test_rejects_empty_required_task_fields(field, value):
    task = _manifest()["tasks"][0] | {field: value}
    with pytest.raises(PlanManifestValidationError, match="invalid_task"):
        parse_plan_manifest(_document(_manifest(tasks=[task])), expected_todo_id="TODO-42")


def test_rejects_bounded_string_overflow():
    task = _manifest()["tasks"][0] | {"instructions": "x" * 10_001}
    with pytest.raises(PlanManifestValidationError, match="invalid_task"):
        parse_plan_manifest(_document(_manifest(tasks=[task])), expected_todo_id="TODO-42")


def test_accepts_exactly_fifty_tasks_and_rejects_fifty_one():
    template = _manifest()["tasks"][0]
    tasks = [template | {"id": f"task-{index}"} for index in range(1, MAX_PLAN_TASKS + 1)]
    manifest = parse_plan_manifest(_document(_manifest(tasks=tasks)), expected_todo_id="TODO-42")
    assert manifest is not None and len(manifest.tasks) == 50

    with pytest.raises(PlanManifestValidationError, match="task_count"):
        parse_plan_manifest(
            _document(_manifest(tasks=[*tasks, template | {"id": "task-51"}])),
            expected_todo_id="TODO-42",
        )


def test_rejects_malformed_json_and_non_exact_keys():
    with pytest.raises(PlanManifestValidationError, match="invalid_json"):
        parse_plan_manifest("```json tpo-plan\n{ nope }\n```", expected_todo_id="TODO-42")

    task = _manifest()["tasks"][0] | {"extra": "no"}
    with pytest.raises(PlanManifestValidationError, match="unknown_keys"):
        parse_plan_manifest(_document(_manifest(tasks=[task])), expected_todo_id="TODO-42")


def test_rejects_an_unclosed_manifest_fence_instead_of_treating_it_as_legacy():
    with pytest.raises(PlanManifestValidationError, match="malformed_fence"):
        parse_plan_manifest("```json tpo-plan\n{}\n", expected_todo_id="TODO-42")


def test_rejects_valid_manifest_followed_by_unclosed_second_opener_as_duplicate():
    document = _document(_manifest()) + "\n```json tpo-plan\n{}\n"

    with pytest.raises(PlanManifestValidationError, match="duplicate_manifest"):
        parse_plan_manifest(document, expected_todo_id="TODO-42")


@pytest.mark.parametrize("indent", ["    ", "\t"])
def test_rejects_non_commonmark_indented_manifest_openers(indent):
    document = f"{indent}```json tpo-plan\n{{}}\n{indent}```\n"

    with pytest.raises(PlanManifestValidationError, match="malformed_fence"):
        parse_plan_manifest(document, expected_todo_id="TODO-42")


@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
def test_accepts_commonmark_fence_indentation(indent):
    payload = json.dumps(_manifest())
    document = f"{indent}```json tpo-plan\n{payload}\n{indent}```\n"

    assert parse_plan_manifest(document, expected_todo_id="TODO-42") is not None
