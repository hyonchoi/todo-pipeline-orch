"""Tests for entry parsing — extracting TODO entries from markdown text."""

from tests.skill_test_environment.skill_logic import parse_entries, VALID_STATUSES


class TestValidStatuses:
    def test_four_markers(self):
        assert VALID_STATUSES == {"[ ]", "[→]", "[x]", "[~]"}


class TestParseEntries:
    """Parse all entries from TODOS.md text."""

    def test_parses_all_entries_from_fixture(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        assert len(entries) == 6
        ids = [e["id"] for e in entries]
        assert ids == [1, 2, 3, 4, 6, 7]

    def test_entry_statuses(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        status_map = {e["id"]: e["status"] for e in entries}
        assert status_map[1] == "[ ]"
        assert status_map[2] == "[→]"
        assert status_map[3] == "[x]"
        assert status_map[4] == "[~]"

    def test_entry_fields(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_1 = entries[0]
        assert entry_1["id"] == 1
        assert "What" in entry_1["fields"]
        assert "Why" in entry_1["fields"]
        assert "Decisions" in entry_1["fields"]

    def test_missing_optional_fields(self, skill_demo_dir):
        """TODO-6 has no optional fields — should still parse."""
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_6 = [e for e in entries if e["id"] == 6][0]
        assert entry_6["fields"].get("Pros") is None
        assert entry_6["fields"].get("Cons") is None
        assert entry_6["fields"].get("Context") is None
        assert "What" in entry_6["fields"]

    def test_dependency_references(self, skill_demo_dir):
        """TODO-7 has Depends on field."""
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_7 = [e for e in entries if e["id"] == 7][0]
        assert "Depends on" in entry_7["fields"]
        assert "TODO-1" in entry_7["fields"]["Depends on"]
        assert "TODO-4" in entry_7["fields"]["Depends on"]

    def test_completed_field(self, skill_demo_dir):
        """TODO-3 has Completed field."""
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_3 = [e for e in entries if e["id"] == 3][0]
        assert "Completed" in entry_3["fields"]

    def test_empty_text(self):
        entries = parse_entries("")
        assert entries == []

    def test_preamble_skipped(self):
        """Preamble blockquote is not an entry."""
        text = "# TODOS\n\n> **Format rules...**\n> - Entry header...\n\n"
        entries = parse_entries(text)
        assert entries == []

    def test_title_and_summary(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        entries = parse_entries(todos)
        entry_1 = entries[0]
        assert "CLI" in entry_1["title"]
        assert entry_1["summary"] != ""
