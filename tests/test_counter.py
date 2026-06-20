"""Tests for counter.py — recover_counter and auto-initialize logic."""

import pytest
from pathlib import Path
from hermes_pipeline.counter import recover_counter, COUNTER_FILE

class TestRecoverCounter:
    """Test recover_counter() function."""

    def test_happy_path(self, tmp_path):
        """TODO-1..5 in TODOS.md -> writes 5 to counter."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-1: Do something\n- TODO-2: Do another\n- TODO-5: Do fifth\n"
        )
        result = recover_counter(project_dir)
        assert result == 5
        counter_file = project_dir / COUNTER_FILE
        assert counter_file.exists()
        assert counter_file.read_text() == "5"

    def test_existing_counter_higher(self, tmp_path):
        """Counter at 8, max in TODOS.md is 4 -> keeps 8."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-1: Do something\n- TODO-4: Do fourth\n"
        )
        (project_dir / ".hermes").mkdir()
        (project_dir / COUNTER_FILE).write_text("8")
        result = recover_counter(project_dir)
        assert result == 8
        assert (project_dir / COUNTER_FILE).read_text() == "8"

    def test_scanned_higher_than_existing(self, tmp_path):
        """Counter at 3, max in TODOS.md is 7 -> writes 7."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-1: Do something\n- TODO-7: Do seventh\n"
        )
        (project_dir / ".hermes").mkdir()
        (project_dir / COUNTER_FILE).write_text("3")
        result = recover_counter(project_dir)
        assert result == 7
        assert (project_dir / COUNTER_FILE).read_text() == "7"

    def test_no_todos_file(self, tmp_path):
        """TODOS.md missing -> raises FileNotFoundError."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            recover_counter(project_dir)

    def test_no_todo_entries(self, tmp_path):
        """TODOS.md with no TODO-N entries -> writes 0."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\nNo todos yet.\n"
        )
        result = recover_counter(project_dir)
        assert result == 0
        counter_file = project_dir / COUNTER_FILE
        assert counter_file.exists()
        assert counter_file.read_text() == "0"

    def test_creates_hermes_dir(self, tmp_path):
        """.hermes/ doesn't exist -> creates it + writes counter."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-3: Do third\n"
        )
        # .hermes/ does NOT exist
        assert not (project_dir / ".hermes").exists()
        result = recover_counter(project_dir)
        assert result == 3
        assert (project_dir / ".hermes").is_dir()
        assert (project_dir / COUNTER_FILE).read_text() == "3"

    def test_corrupt_counter_file(self, tmp_path):
        """Counter file contains non-integer -> treats as 0, uses scanned max."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-4: Do fourth\n"
        )
        (project_dir / ".hermes").mkdir()
        (project_dir / COUNTER_FILE).write_text("not-a-number")
        result = recover_counter(project_dir)
        assert result == 4
        assert (project_dir / COUNTER_FILE).read_text() == "4"

    def test_empty_counter_file(self, tmp_path):
        """Counter file is empty -> treats as 0, uses scanned max."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-2: Do second\n"
        )
        (project_dir / ".hermes").mkdir()
        (project_dir / COUNTER_FILE).write_text("")
        result = recover_counter(project_dir)
        assert result == 2
        assert (project_dir / COUNTER_FILE).read_text() == "2"

    def test_matches_todo_references_in_body(self, tmp_path):
        """TODO-N in body text (not as entry) is still matched — this is correct behavior per design."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-1: Depends on TODO-6\n- TODO-3: Standalone\n"
        )
        result = recover_counter(project_dir)
        # TODO-6 appears in body text; it's matched by the regex. This is the
        # same regex the agent validation uses. If TODO-6 is referenced but
        # not a real entry, the counter is set higher — that's fine, it just
        # means the counter won't collide.
        assert result == 6

    def test_both_empty(self, tmp_path):
        """No counter file and no TODO entries -> writes 0."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text("# TODOS\n\n")
        result = recover_counter(project_dir)
        assert result == 0
        assert (project_dir / COUNTER_FILE).read_text() == "0"

    def test_atomic_write_failure_cleanup(self, tmp_path):
        """When os.replace fails during atomic write, temp file is cleaned up."""
        import os
        from unittest.mock import patch
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-4: Do fourth\n"
        )
        (project_dir / ".hermes").mkdir()

        # Mock os.replace to raise OSError, simulating a disk-full scenario
        original_replace = os.replace
        def fake_replace(src, dst):
            raise OSError("disk full")
        with patch("os.replace", fake_replace):
            with pytest.raises(OSError):
                recover_counter(project_dir)

        # Verify: the original counter file should be untouched (was not overwritten)
        # and no orphan temp files should exist in .hermes/
        entries = list((project_dir / ".hermes").iterdir())
        orphan_files = [f for f in entries if f.name.startswith(".todo_id_counter.") and f.is_file()]
        assert orphan_files == [], f"Orphan temp files found: {orphan_files}"
