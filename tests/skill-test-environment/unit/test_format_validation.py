"""Tests for format validation — schema compliance checks."""

from tests.skill_test_environment.skill_logic import (
    REQUIRED_FIELDS,
    read_next_todo_id,
    replace_next_todo_id_line,
    validate_all_entries,
    validate_dependency_refs,
    validate_entry,
)


class TestTrackedNextTodoIdFormat:
    def test_read_next_todo_id_from_preamble(self):
        text = "# TODOS\n\n> **Format rules:**\n> - NEXT_TODO_ID: 8\n\n- [ ] TODO-1: A\n"

        value, issues = read_next_todo_id(text)

        assert value == 8
        assert issues == []

    def test_read_next_todo_id_rejects_zero_negative_and_non_integer(self):
        for raw in ("0", "-1", "1.5", "abc"):
            text = f"# TODOS\n\n> - NEXT_TODO_ID: {raw}\n"
            value, issues = read_next_todo_id(text)
            assert value is None
            assert any("NEXT_TODO_ID" in issue for issue in issues)

    def test_read_next_todo_id_rejects_duplicate_lines(self):
        text = "# TODOS\n\n> - NEXT_TODO_ID: 8\n> - NEXT_TODO_ID: 9\n"

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("duplicated" in issue.lower() for issue in issues)

    def test_replace_next_todo_id_line_preserves_preamble(self):
        text = (
            "# TODOS\n\n> **Format rules:**\n> - NEXT_TODO_ID: 8\n"
            "> - Completed entries: archived\n"
        )

        updated = replace_next_todo_id_line(text, 9)

        assert "> - NEXT_TODO_ID: 9" in updated
        assert "> - Completed entries: archived" in updated


class TestRequiredFields:
    def test_three_required(self):
        assert REQUIRED_FIELDS == {"What", "Why", "Decisions"}


class TestValidateEntry:
    """Per-entry schema validation."""

    def test_valid_entry_no_issues(self, skill_demo_dir):
        from tests.skill_test_environment.skill_logic import parse_entries
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_1 = entries[0]
        issues = validate_entry(entry_1)
        assert issues == []

    def test_missing_what_field(self):
        entry = {"id": 1, "status": "[ ]", "title": "Test", "summary": "Test", "fields": {"Why": "reason"}}
        issues = validate_entry(entry)
        assert any("What" in i for i in issues)

    def test_missing_why_field(self):
        entry = {"id": 1, "status": "[ ]", "title": "Test", "summary": "Test", "fields": {"What": "desc"}}
        issues = validate_entry(entry)
        assert any("Why" in i for i in issues)

    def test_missing_decisions_field(self):
        entry = {"id": 1, "status": "[ ]", "title": "Test", "summary": "Test", "fields": {"What": "desc", "Why": "reason"}}
        issues = validate_entry(entry)
        assert any("Decisions" in i for i in issues)

    def test_invalid_status_marker(self):
        entry = {"id": 1, "status": "[->]", "title": "Test", "summary": "Test", "fields": {"What": "d", "Why": "r", "Decisions": "x"}}
        issues = validate_entry(entry)
        assert any("status" in i.lower() or "marker" in i.lower() for i in issues)

    def test_all_required_present(self):
        entry = {"id": 1, "status": "[ ]", "title": "T", "summary": "S", "fields": {"What": "w", "Why": "y", "Decisions": "d"}}
        issues = validate_entry(entry)
        assert issues == []


class TestValidateAllEntries:
    def test_fixture_all_valid(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        results = validate_all_entries(todos)
        for result in results:
            assert result["issues"] == [], f"TODO-{result['id']} has issues: {result['issues']}"

    def test_mixed_valid_invalid(self, tmp_path):
        text = (
            "# TODOS\n\n"
            "- [ ] **TODO-1: Good Entry** — Summary\n"
            "  - **What:** Do something\n"
            "  - **Why:** Because\n"
            "  - **Decisions:** Priority `P1`\n\n"
            "- [ ] **TODO-2: Bad Entry** — Missing fields\n"
            "  - **What:** Do something\n"
        )
        results = validate_all_entries(text)
        assert len(results) == 2
        assert results[0]["id"] == 1
        assert results[0]["issues"] == []
        assert results[1]["id"] == 2
        assert len(results[1]["issues"]) > 0


class TestValidateDependencyRefs:
    def test_valid_deps(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        broken = validate_dependency_refs(todos)
        assert broken == []

    def test_broken_dep_reference(self, tmp_path):
        text = (
            "# TODOS\n\n"
            "- [ ] **TODO-1: Has dep** — Summary\n"
            "  - **What:** Test\n"
            "  - **Why:** Test\n"
            "  - **Depends on:** `TODO-99`\n"
            "  - **Decisions:** Priority `P1`\n"
        )
        broken = validate_dependency_refs(text)
        assert any("TODO-99" in b for b in broken)
