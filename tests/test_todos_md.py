"""Tests for hermes_pipeline.todos_md.find_todo_fields."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pipeline.todos_md import (
    TodoPlanValidationError,
    find_todo_fields,
    resolve_todo_plan,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "TODOS.md"
    p.write_text(content)
    return p


def test_entry_with_both_fields(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **What:** stuff
  - **Why:** reasons
  - **Spec:** docs/pipeline/TODO-25-spec.md
  - **Reference:** docs/notes/a.md, docs/notes/b.md
  - **Decisions:** Priority `P1`
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {
        "spec": "docs/pipeline/TODO-25-spec.md",
        "references": ["docs/notes/a.md", "docs/notes/b.md"],
    }


def test_entry_with_only_spec(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **What:** stuff
  - **Spec:** docs/pipeline/TODO-25-spec.md
  - **Decisions:** Priority `P1`
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": "docs/pipeline/TODO-25-spec.md", "references": []}


def test_entry_with_only_reference(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **What:** stuff
  - **Reference:** docs/notes/a.md
  - **Decisions:** Priority `P1`
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": ["docs/notes/a.md"]}


def test_entry_with_neither_field(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **What:** stuff
  - **Why:** reasons
  - **Decisions:** Priority `P1`
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": []}


def test_reference_whitespace_variance(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Reference:**   docs/notes/a.md ,docs/notes/b.md  ,  docs/notes/c.md
""")
    result = find_todo_fields(p, "TODO-25")
    assert result["references"] == [
        "docs/notes/a.md", "docs/notes/b.md", "docs/notes/c.md",
    ]


def test_malformed_todos_md_does_not_raise(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:**
  garbled nonsense with no closing structure
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": []}


def test_multiple_entries_anchors_to_todo_id(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-24: Other thing** — summary
  - **Spec:** docs/pipeline/TODO-24-spec.md
  - **Reference:** docs/notes/wrong.md
- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** docs/pipeline/TODO-25-spec.md
  - **Reference:** docs/notes/right.md
- [x] **TODO-26: Yet another** — summary
  - **Spec:** docs/pipeline/TODO-26-spec.md
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {
        "spec": "docs/pipeline/TODO-25-spec.md",
        "references": ["docs/notes/right.md"],
    }


def test_traversal_path_is_returned_verbatim_by_parser(tmp_path):
    """Containment rejection is phases.py's job (Task 3), not the parser's —
    the parser only extracts raw strings from TODOS.md text."""
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** ../../etc/passwd
""")
    result = find_todo_fields(p, "TODO-25")
    assert result["spec"] == "../../etc/passwd"


def test_todo_id_not_present(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-24: Other thing** — summary
  - **Spec:** docs/pipeline/TODO-24-spec.md
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": []}


def test_todos_md_missing_entirely(tmp_path):
    p = tmp_path / "TODOS.md"
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": []}


def _write_plan_todo(tmp_path: Path, plan_value: str) -> Path:
    todos = _write(
        tmp_path,
        "# TODOS\n\n"
        "- [ ] **TODO-25: Do the thing** — summary\n"
        f"  - **Plan:** {plan_value}\n",
    )
    return todos


def test_resolve_todo_plan_returns_validated_relative_path(tmp_path):
    plan = tmp_path / "docs" / "plan.md"
    plan.parent.mkdir()
    plan.write_text("# Plan\n")
    todos = _write_plan_todo(tmp_path, "docs/plan.md")

    assert resolve_todo_plan(tmp_path, todos, "TODO-25") == "docs/plan.md"


@pytest.mark.parametrize(
    ("plan_value", "code"),
    [
        ("", "missing"),
        ("/tmp/plan.md", "absolute"),
        ("../plan.md", "outside_repository"),
        ("docs/missing.md", "missing_file"),
    ],
)
def test_resolve_todo_plan_rejects_invalid_path(tmp_path, plan_value, code):
    todos = _write_plan_todo(tmp_path, plan_value)

    with pytest.raises(TodoPlanValidationError) as exc_info:
        resolve_todo_plan(tmp_path, todos, "TODO-25")

    assert exc_info.value.code == code


def test_resolve_todo_plan_rejects_duplicate_fields(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n")
    todos = _write(
        tmp_path,
        "# TODOS\n\n"
        "- [ ] **TODO-25: Do the thing** — summary\n"
        "  - **Plan:** plan.md\n"
        "  - **Plan:** plan.md\n",
    )

    with pytest.raises(TodoPlanValidationError) as exc_info:
        resolve_todo_plan(tmp_path, todos, "TODO-25")

    assert exc_info.value.code == "duplicate"


def test_resolve_todo_plan_rejects_directory(tmp_path):
    (tmp_path / "docs").mkdir()
    todos = _write_plan_todo(tmp_path, "docs")

    with pytest.raises(TodoPlanValidationError) as exc_info:
        resolve_todo_plan(tmp_path, todos, "TODO-25")

    assert exc_info.value.code == "not_regular_file"


def test_resolve_todo_plan_rejects_symlink_outside_repository(tmp_path):
    outside = tmp_path.parent / "outside-plan.md"
    outside.write_text("# Outside\n")
    link = tmp_path / "plan.md"
    link.symlink_to(outside)
    todos = _write_plan_todo(tmp_path, "plan.md")

    with pytest.raises(TodoPlanValidationError) as exc_info:
        resolve_todo_plan(tmp_path, todos, "TODO-25")

    assert exc_info.value.code == "outside_repository"


def test_resolve_todo_plan_rejects_unreadable_file(tmp_path, mocker):
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n")
    todos = _write_plan_todo(tmp_path, "plan.md")
    mocker.patch("builtins.open", side_effect=PermissionError)

    with pytest.raises(TodoPlanValidationError) as exc_info:
        resolve_todo_plan(tmp_path, todos, "TODO-25")

    assert exc_info.value.code == "unreadable"
