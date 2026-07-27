"""Tests for format validation — schema compliance checks."""

from tests.skill_test_environment.skill_logic import (
    REQUIRED_FIELDS,
    parse_todos_document_sections,
    read_next_todo_id,
    replace_next_todo_id_line,
    validate_all_entries,
    validate_dependency_refs,
    validate_entry,
)


class TestTrackedNextTodoIdFormat:
    def test_parse_sections_returns_three_ranges(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n"
            "> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        sections = parse_todos_document_sections(text)

        assert sections.metadata == "NEXT_TODO_ID: 8\n"
        assert "> **Format rules:**" in sections.schema
        assert "TODO-7: Existing" in sections.entries
        assert sections.diagnostics == []
        assert sections.has_canonical_layout is True

    def test_read_next_todo_id_from_metadata_section(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
        )

        value, issues = read_next_todo_id(text)

        assert value == 8
        assert issues == []

    def test_read_next_todo_id_rejects_metadata_under_entries(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "NEXT_TODO_ID: 8\n"
        )

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("outside ## Metadata" in issue for issue in issues)

    def test_read_next_todo_id_rejects_metadata_under_schema(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "\n"
            "## Entry Schema\n\n"
            "NEXT_TODO_ID: 8\n"
            "> **Format rules:**\n\n"
            "## Entries\n"
        )

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("outside ## Metadata" in issue for issue in issues)

    def test_read_next_todo_id_rejects_duplicate_across_sections(self):
        text = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "NEXT_TODO_ID: 99\n"
        )

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("duplicated" in issue.lower() for issue in issues)

    def test_replace_next_todo_id_line_migrates_legacy_layout(self):
        text = (
            "# TODOS\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "> **Format rules:**\n"
            "> - Entry header: example\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        updated = replace_next_todo_id_line(text, 9)

        assert updated.startswith("# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 9\n\n")
        assert "\n## Entry Schema\n\n> **Format rules:**" in updated
        assert "\n## Entries\n\n- [ ] **TODO-7: Existing**" in updated
        assert updated.count("NEXT_TODO_ID:") == 1

    def test_replace_next_todo_id_line_preserves_crlf(self):
        text = (
            "# TODOS\r\n\r\n"
            "## Metadata\r\n\r\n"
            "NEXT_TODO_ID: 8\r\n\r\n"
            "## Entry Schema\r\n\r\n"
            "> **Format rules:**\r\n\r\n"
            "## Entries\r\n"
        )

        updated = replace_next_todo_id_line(text, 9)

        assert "NEXT_TODO_ID: 9\r\n" in updated
        assert "\n" not in updated.replace("\r\n", "")

    def test_read_next_todo_id_rejects_global_metadata(self):
        text = "# TODOS\n\nNEXT_TODO_ID: 8\n\n> **Format rules:**\n\n- [ ] TODO-1: A\n"

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("outside ## Metadata" in issue for issue in issues)

    def test_read_next_todo_id_rejects_zero_negative_and_non_integer(self):
        for raw in ("0", "-1", "1.5", "abc"):
            text = f"# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: {raw}\n\n## Entry Schema\n\n## Entries\n"
            value, issues = read_next_todo_id(text)
            assert value is None
            assert any("NEXT_TODO_ID" in issue for issue in issues)

    def test_read_next_todo_id_rejects_empty_and_whitespace_values(self):
        for raw in ("", "   ", "\t"):
            text = f"# TODOS\n\n## Metadata\n\nNEXT_TODO_ID:{raw}\n\n## Entry Schema\n\n## Entries\n\n- [ ] TODO-1: Preserved\n"

            value, issues = read_next_todo_id(text)

            assert value is None
            assert any("positive base-10 integer" in issue for issue in issues)

    def test_replace_empty_metadata_value_does_not_consume_following_entry(self):
        text = "# TODOS\n\nNEXT_TODO_ID:\n- [ ] TODO-1: Preserved\n"

        updated = replace_next_todo_id_line(text, 2)

        assert "NEXT_TODO_ID: 2\n" in updated
        assert "- [ ] TODO-1: Preserved\n" in updated

    def test_read_next_todo_id_rejects_mixed_value(self):
        text = "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 8 trailing\n\n## Entry Schema\n\n## Entries\n"

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("positive base-10 integer" in issue for issue in issues)

    def test_read_next_todo_id_rejects_duplicate_lines(self):
        text = "# TODOS\n\nNEXT_TODO_ID: 8\nNEXT_TODO_ID: 9\n"

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("duplicated" in issue.lower() for issue in issues)

    def test_read_next_todo_id_rejects_valid_and_malformed_duplicates(self):
        text = "# TODOS\n\nNEXT_TODO_ID: 8\nNEXT_TODO_ID: invalid\n"

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("duplicated" in issue.lower() for issue in issues)

    def test_read_next_todo_id_rejects_stray_duplicate_metadata(self):
        text = (
            "# TODOS\n\nNEXT_TODO_ID: 8\n\n> **Format rules:**\n\n"
            "- [ ] TODO-1: A\n\nNEXT_TODO_ID: 99\n"
        )

        value, issues = read_next_todo_id(text)

        assert value is None
        assert any("duplicated" in issue.lower() for issue in issues)

    def test_replace_next_todo_id_line_preserves_preamble(self):
        text = (
            "# TODOS\n\nNEXT_TODO_ID: 8\n\n> **Format rules:**\n"
            "> - Completed entries: archived\n"
        )

        updated = replace_next_todo_id_line(text, 9)

        assert "NEXT_TODO_ID: 9" in updated
        assert updated.index("NEXT_TODO_ID: 9") < updated.index("> **Format rules:**")
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
