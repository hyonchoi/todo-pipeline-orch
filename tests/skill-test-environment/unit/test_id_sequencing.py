"""Tests for ID sequencing logic — scanning, next-ID computation, counter cache."""

from pathlib import Path

import pytest

from tests.skill_test_environment.skill_logic import (
    compute_next_id,
    compute_scan_next_id,
    counter_matches_scan,
    read_counter_cache,
    reconcile_next_todo_id,
    scan_ids,
)


class TestScanIds:
    """Parse TODO-N IDs from markdown text."""

    def test_scans_all_ids_from_fixture(self, skill_demo_dir):
        todos = (skill_demo_dir / "TODOS.md").read_text()
        ids = scan_ids(todos)
        assert ids == {1, 2, 3, 4, 6, 7}

    def test_scans_archive_ids(self, skill_demo_dir):
        archive = (skill_demo_dir / "TODOS-archive.md").read_text()
        ids = scan_ids(archive)
        assert ids == {5}

    def test_empty_text_returns_empty_set(self):
        ids = scan_ids("")
        assert ids == set()

    def test_no_todo_entries_returns_empty(self):
        ids = scan_ids("# TODOS\n\nSome random text with no entries.\n")
        assert ids == set()

    def test_does_not_match_partial(self):
        """TODO-N in a sentence body should still match — regex finds all occurrences."""
        text = "- [ ] TODO-3: Depends on TODO-1 and TODO-4\n"
        ids = scan_ids(text)
        assert ids == {1, 3, 4}


class TestComputeNextId:
    """Next ID = max(all IDs from both files) + 1."""

    def test_next_id_from_fixture(self, skill_demo_dir):
        """Main has {1,2,3,4,6,7}, archive has {5} → next is 8."""
        next_id = compute_next_id(
            skill_demo_dir / "TODOS.md",
            skill_demo_dir / "TODOS-archive.md",
        )
        assert next_id == 8

    def test_next_id_empty_files(self, tmp_path):
        """Both files empty → next is 1."""
        todos = tmp_path / "TODOS.md"
        archive = tmp_path / "TODOS-archive.md"
        todos.write_text("# TODOS\n\n")
        archive.write_text("")
        next_id = compute_next_id(todos, archive)
        assert next_id == 1

    def test_next_id_archive_missing(self, skill_demo_dir):
        """Archive doesn't exist → compute from TODOS.md only → 8."""
        next_id = compute_next_id(
            skill_demo_dir / "TODOS.md",
            Path("/nonexistent/TODOS-archive.md"),
        )
        assert next_id == 8

    def test_gap_in_sequence(self, tmp_path):
        """IDs {1, 2, 5} → next is 6 (don't fill gaps)."""
        todos = tmp_path / "TODOS.md"
        archive = tmp_path / "TODOS-archive.md"
        todos.write_text("# TODOS\n\n- TODO-1: A\n- TODO-2: B\n- TODO-5: C\n")
        archive.write_text("")
        next_id = compute_next_id(todos, archive)
        assert next_id == 6


class TestTrackedNextTodoId:
    """Tracked metadata is reconciled against the scan-derived next ID."""

    def test_compute_scan_next_id_matches_existing_scan(self, tmp_path):
        todos = tmp_path / "TODOS.md"
        archive = tmp_path / "TODOS-archive.md"
        todos.write_text("# TODOS\n\n- [ ] TODO-4: Existing\n", encoding="utf-8")
        archive.write_text("- [x] TODO-7: Archived\n", encoding="utf-8")

        assert compute_scan_next_id(todos, archive) == 8

    def test_reconcile_repairs_stale_low_value_from_archive(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n> - NEXT_TODO_ID: 3\n\n- [ ] TODO-1: A\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text(
            "- [x] TODO-7: Archived\n", encoding="utf-8"
        )

        next_id, messages = reconcile_next_todo_id(tmp_path, mode="audit")

        assert next_id == 8
        assert "> - NEXT_TODO_ID: 8" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert any("corrected NEXT_TODO_ID from 3 to 8" in message for message in messages)

    def test_reconcile_missing_line_inserts_after_format_rules(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "> **Format rules (enforced by `todos-manager` skill):**\n"
            "> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`\n\n"
            "- [ ] TODO-4: Existing\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        next_id, messages = reconcile_next_todo_id(tmp_path, mode="audit")

        assert next_id == 5
        assert "> - NEXT_TODO_ID: 5" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert any("inserted NEXT_TODO_ID: 5" in message for message in messages)


class TestCounterCache:
    """Counter cache is performance-only; scan is authoritative."""

    @pytest.fixture(autouse=True)
    def _cleanup_counter(self, skill_demo_dir):
        """Remove counter file after each test to avoid cross-test pollution."""
        yield
        counter = skill_demo_dir / ".hermes" / "todo_id_counter"
        if counter.exists():
            counter.unlink()
            # Remove .hermes dir if empty
            parent = counter.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()

    def test_read_counter_cache(self, skill_demo_dir):
        counter = skill_demo_dir / ".hermes" / "todo_id_counter"
        counter.parent.mkdir(exist_ok=True)
        counter.write_text("7")
        assert read_counter_cache(skill_demo_dir) == 7

    def test_no_counter_returns_none(self, skill_demo_dir):
        assert read_counter_cache(skill_demo_dir) is None

    def test_counter_matches_scan(self, skill_demo_dir):
        counter = skill_demo_dir / ".hermes" / "todo_id_counter"
        counter.parent.mkdir(exist_ok=True)
        counter.write_text("7")
        # Max scanned ID is 7, so counter matches
        assert counter_matches_scan(skill_demo_dir) is True

    def test_counter_diverges_from_scan(self, skill_demo_dir):
        counter = skill_demo_dir / ".hermes" / "todo_id_counter"
        counter.parent.mkdir(exist_ok=True)
        counter.write_text("3")
        # Max scanned ID is 7, counter says 3 — diverges
        assert counter_matches_scan(skill_demo_dir) is False
