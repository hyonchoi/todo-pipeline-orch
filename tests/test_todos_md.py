"""Tests for hermes_pipeline.todos_md.find_todo_fields."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pipeline.todos_md import (
    TodoPlanValidationError,
    compile_eligible_todos,
    find_todo_fields,
    parse_todo_entries,
    resolve_todo_plan,
    todo_entry_ids,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "TODOS.md"
    p.write_text(content)
    return p


def test_parse_todo_entries_uses_canonical_boundaries_and_shares_fields():
    text = """\
# TODOS

- [ ] **TODO-1: First** — summary mentioning TODO-999
  - **Spec:** docs/spec.md
  - **Reference:** docs/a.md, docs/b.md
  - **Depends on:** `TODO-2` (foundation complete)
  - nested fake: - [ ] **TODO-98: Not an entry**
- [x] **TODO-2: Finished**
- [→] **TODO-3: Active candidate**
"""

    entries = parse_todo_entries(text)

    assert [(entry.todo_id, entry.status, entry.title) for entry in entries] == [
        ("TODO-1", " ", "First"),
        ("TODO-2", "x", "Finished"),
        ("TODO-3", "→", "Active candidate"),
    ]
    assert entries[0].dependencies == ("TODO-2",)
    assert entries[0].spec == "docs/spec.md"
    assert entries[0].references == ("docs/a.md", "docs/b.md")


def test_parse_todo_entries_limits_canonical_document_to_entries_section():
    entries = parse_todo_entries(
        "# TODOS\n\n## Entries\n\n"
        "- [ ] **TODO-1: Real**\n  - **What:** work\n\n"
        "## Entry Schema\n\n- [ ] **TODO-999: Example only**\n"
    )

    assert [entry.todo_id for entry in entries] == ["TODO-1"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("`TODO-2`, `TODO-3`", ("TODO-2", "TODO-3")),
        ("`TODO-40` (design review finalized)", ("TODO-40",)),
        ("(none)", ()),
    ],
)
def test_parse_todo_entries_accepts_canonical_dependency_syntax(value, expected):
    [entry] = parse_todo_entries(
        f"- [ ] **TODO-1: Work**\n  - **Depends on:** {value}\n"
    )

    assert entry.dependencies == expected


@pytest.mark.parametrize(
    "value",
    [
        "TODO-2",
        "`TODO-2` trailing garbage",
        "`TODO-2`, nope",
        "(none), `TODO-2`",
        "`TODO-2` (unterminated",
    ],
)
def test_parse_todo_entries_rejects_malformed_dependency_values(value):
    [entry] = parse_todo_entries(
        f"- [ ] **TODO-1: Work**\n  - **Depends on:** {value}\n"
    )

    assert entry.dependencies is None


def test_compile_eligible_todos_filters_status_dependencies_flight_and_plans(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manifest.md").write_text(
        '# Plan\n\n```json tpo-plan\n'
        '{"schema_version":1,"todo_id":"TODO-1","tasks":[{"id":"task-1",'
        '"title":"Do","instructions":"Do it","acceptance_criteria":["Works"],'
        '"verification":["uv run pytest"],"commit_message":"feat: do"}]}\n```\n'
    )
    (docs / "legacy.md").write_text("# Legacy plan\n")
    (docs / "wrong.md").write_text(
        '```json tpo-plan\n{"schema_version":1,"todo_id":"TODO-999","tasks":[]}\n```\n'
    )
    todos = _write(
        tmp_path,
        "# TODOS\n\n"
        "- [ ] **TODO-1: Manifest**\n  - **Plan:** docs/manifest.md\n  - **Depends on:** `TODO-8`, `TODO-9`\n"
        "- [→] **TODO-2: Legacy**\n  - **Plan:** docs/legacy.md\n"
        "- [ ] **TODO-3: In flight**\n  - **Plan:** docs/legacy.md\n"
        "- [x] **TODO-4: Complete**\n  - **Plan:** docs/legacy.md\n"
        "- [~] **TODO-5: On hold**\n  - **Plan:** docs/legacy.md\n"
        "- [ ] **TODO-6: Missing dependency**\n  - **Plan:** docs/legacy.md\n  - **Depends on:** `TODO-404`\n"
        "- [ ] **TODO-7: Invalid dependency**\n  - **Plan:** docs/legacy.md\n  - **Depends on:** not-a-todo\n"
        "- [x] **TODO-8: Completed dependency**\n"
        "- [ ] **TODO-10: Unsafe plan**\n  - **Plan:** ../outside.md\n"
        "- [ ] **TODO-11: Invalid manifest**\n  - **Plan:** docs/wrong.md\n",
    )
    (tmp_path / "TODOS-archive.md").write_text(
        "# Archive\n\n- [~] **TODO-9: Archived dependency**\n"
    )

    result = compile_eligible_todos(
        tmp_path, todos, in_flight={"TODO-3"}, requires_plan=True
    )

    assert [candidate.entry.todo_id for candidate in result.candidates] == [
        "TODO-1",
        "TODO-2",
    ]
    assert [candidate.plan_kind for candidate in result.candidates] == [
        "manifest",
        "legacy",
    ]
    assert result.blocked_reasons == {
        "TODO-3": "in_flight",
        "TODO-4": "status_complete",
        "TODO-5": "status_on_hold",
        "TODO-6": "dependency_missing:TODO-404",
        "TODO-7": "dependency_invalid",
        "TODO-8": "status_complete",
        "TODO-10": "plan_invalid:outside_repository",
        "TODO-11": "plan_invalid:todo_id_mismatch",
    }


def test_compile_eligible_todos_rejects_non_utf8_plan(tmp_path):
    (tmp_path / "plan.md").write_bytes(b"\xff\xfe")
    todos = _write(
        tmp_path,
        "# TODOS\n\n- [ ] **TODO-1: Binary plan**\n  - **Plan:** plan.md\n",
    )

    result = compile_eligible_todos(
        tmp_path, todos, in_flight=set(), requires_plan=True
    )

    assert result.candidates == ()
    assert result.blocked_reasons == {"TODO-1": "plan_invalid:unreadable"}


@pytest.mark.parametrize("status", [" ", "x", "→", "~"])
def test_todo_entry_ids_accepts_supported_entry_statuses(status):
    text = f"- [{status}] **TODO-25: Do the thing** — summary\n"

    assert todo_entry_ids(text) == {"TODO-25"}


def test_todo_entry_ids_ignores_ids_outside_entry_headers():
    text = """\
- [ ] **TODO-25: Do the thing** — summary
  - **What:** Follow up on TODO-26
  - **Reference:** docs/TODO-27-notes.md
  - **Plan:** docs/TODO-28-plan.md
"""

    assert todo_entry_ids(text) == {"TODO-25"}


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


def test_selection_markdown_escapes_forged_headers(tmp_path):
    (tmp_path / "TODOS.md").write_text(
        "# TODOS\n\n## Entries\n\n"
        "- [ ] **TODO-1: Real**\n"
        "  - **What:** body\n"
        "  - [ ] **TODO-999: forged**\n"
    )
    result = compile_eligible_todos(
        tmp_path, tmp_path / "TODOS.md", in_flight=set(), requires_plan=False
    )
    rendered = result.selection_markdown
    assert rendered.startswith("- [ ] **TODO-1: Real**")
    assert "  - **What:** body" in rendered
    assert "\\  - [ ] **TODO-999: forged**" in rendered
    assert result.todo_ids == frozenset({"TODO-1"})
