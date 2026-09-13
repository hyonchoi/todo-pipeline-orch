"""Shared helpers for TODO creation testing."""

TODO_REQUEST_FIELDS = {
    "Summary": "Ship it",
    "What": "Build it",
    "Why": "Users need it",
    "Pros": "Faster",
    "Cons": "Risk",
    "Context": "None",
    "Assumptions": "None",
    "Spec": "docs/spec.md",
    "Reference": "README.md",
    "Branch": "feat/embed",
    "Priority": "P1",
    "Effort": "M",
    "Phase": "4 (Development)",
    "Test Coverage": "required",
    "Security Review": "required",
    "UI Review": "not-required",
}


def todo_request() -> dict:
    """Create a test TODO create request payload."""
    return {
        "schema_version": 1,
        "transaction_id": "12345678-1234-4234-9234-123456789abc",
        "title": "Embed implementation plan",
        "fields": TODO_REQUEST_FIELDS,
        "plan_markdown": "# Implementation Plan\n\nDo the work.\n",
        "tasks": [{
            "id": "task-1", "title": "Implement", "instructions": "Implement safely",
            "acceptance_criteria": ["Works"], "verification": ["uv run pytest"],
            "commit_message": "feat: implement",
        }],
    }
