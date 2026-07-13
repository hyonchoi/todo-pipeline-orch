"""Tests for archive logic — finding completed entries and simulating archive movement."""

from tests.skill_test_environment.skill_logic import (
    find_completed_entries,
    extract_entry_blocks,
    simulate_archive,
    scan_ids,
)


class TestFindCompletedEntries:
    def test_finds_completed_in_fixture(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        completed = find_completed_entries(todos)
        assert len(completed) == 1
        assert completed[0]["id"] == 3

    def test_no_completed(self):
        text = "- [ ] **TODO-1: Pending** — Test\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n"
        completed = find_completed_entries(text)
        assert completed == []

    def test_multiple_completed(self):
        text = (
            "- [x] **TODO-1: Done A** — S\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n\n"
            "- [ ] **TODO-2: Pending** — S\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n\n"
            "- [x] **TODO-3: Done B** — S\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n"
        )
        completed = find_completed_entries(text)
        assert len(completed) == 2
        assert [e["id"] for e in completed] == [1, 3]


class TestExtractEntryBlocks:
    def test_extracts_blocks_from_fixture(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        blocks = extract_entry_blocks(todos)
        assert len(blocks) == 6
        assert "TODO-1" in blocks[0]
        assert "TODO-3" in blocks[2]

    def test_block_includes_sub_bullets(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        blocks = extract_entry_blocks(todos)
        entry_3_block = [b for b in blocks if "TODO-3" in b][0]
        assert "**What:**" in entry_3_block
        assert "**Completed:**" in entry_3_block


class TestSimulateArchive:
    def test_archive_removes_completed(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        archive = (skill_demo_dir / "TODOS-archive.md").read_text()
        new_todos, new_archive = simulate_archive(todos, archive)
        assert "TODO-3" not in new_todos
        assert "TODO-1" in new_todos
        assert "TODO-2" in new_todos

    def test_archive_appends_to_existing(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        archive = (skill_demo_dir / "TODOS-archive.md").read_text()
        new_todos, new_archive = simulate_archive(todos, archive)
        assert "TODO-5" in new_archive
        assert "TODO-3" in new_archive

    def test_archive_preserves_ids(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        archive = (skill_demo_dir / "TODOS-archive.md").read_text()
        new_todos, new_archive = simulate_archive(todos, archive)
        all_ids = scan_ids(new_todos) | scan_ids(new_archive)
        assert all_ids == {1, 2, 3, 4, 5, 6, 7}

    def test_no_completed_no_change(self, tmp_path):
        text = "- [ ] **TODO-1: Pending** — S\n  - **What:** W\n  - **Why:** Y\n  - **Decisions:** D\n"
        new_todos, new_archive = simulate_archive(text, "")
        assert new_todos == text
