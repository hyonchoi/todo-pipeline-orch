"""Tests for hermes_pipeline.todos_md.find_todo_fields."""
from __future__ import annotations
from pathlib import Path
from hermes_pipeline.todos_md import find_todo_fields


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
