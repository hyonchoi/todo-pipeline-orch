"""Tests for ID sequencing logic — scanning, next-ID computation, counter cache."""

import multiprocessing as mp
from pathlib import Path

import pytest

from tests.skill_test_environment.skill_logic import (
    assign_next_todo_id,
    compute_next_id,
    compute_scan_next_id,
    counter_matches_scan,
    read_counter_cache,
    reconcile_next_todo_id,
    scan_ids,
)


def _assign_todo_id_in_process(project_dir: str, results) -> None:
    """Append an entry in the worker's atomic TODO update."""
    assigned, _ = assign_next_todo_id(
        Path(project_dir),
        entry_builder=lambda todo_id: f"- [ ] TODO-{todo_id}: Reserved",
    )
    results.put(assigned)


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

    def test_assign_next_todo_id_appends_under_entries_section(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 8\n\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n",
            encoding="utf-8",
        )

        assigned, messages = assign_next_todo_id(
            tmp_path,
            lambda todo_id: (
                f"- [ ] **TODO-{todo_id}: Added** — Summary\n"
                "  - **What:** Work\n"
                "  - **Why:** Reason\n"
                "  - **Decisions:** Priority `P1`"
            ),
        )

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert assigned == 8
        assert messages == []
        assert updated.index("TODO-8: Added") > updated.index("## Entries")
        assert updated.index("TODO-8: Added") > updated.index("TODO-7: Existing")
        assert updated.index("NEXT_TODO_ID: 9") < updated.index("## Entry Schema")

    def test_assign_next_todo_id_preserves_crlf_for_canonical_document(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\r\n\r\n"
            "## Metadata\r\n\r\n"
            "NEXT_TODO_ID: 8\r\n\r\n"
            "## Entry Schema\r\n\r\n"
            "> **Format rules:**\r\n\r\n"
            "## Entries\r\n\r\n"
            "- [ ] **TODO-7: Existing** — Summary\r\n"
            "  - **What:** Work\r\n"
            "  - **Why:** Reason\r\n"
            "  - **Decisions:** Priority `P1`\r\n",
            encoding="utf-8",
        )

        assigned, messages = assign_next_todo_id(
            tmp_path,
            lambda todo_id: (
                f"- [ ] **TODO-{todo_id}: Added** — Summary\n"
                "  - **What:** Work\n"
                "  - **Why:** Reason\n"
                "  - **Decisions:** Priority `P1`"
            ),
        )

        with (tmp_path / "TODOS.md").open(encoding="utf-8", newline="") as todos_file:
            updated = todos_file.read()
        assert assigned == 8
        assert messages == []
        assert "**TODO-8: Added** — Summary\r\n" in updated
        assert "\n" not in updated.replace("\r\n", "")

    def test_reconcile_migrates_legacy_layout_to_sections(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "NEXT_TODO_ID: 2\n\n"
            "> **Format rules:**\n"
            "> - Entry header: example\n\n"
            "- [ ] **TODO-1: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n",
            encoding="utf-8",
        )

        reconciled, messages = reconcile_next_todo_id(tmp_path, "audit")

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert reconciled == 2
        assert "## Metadata\n\nNEXT_TODO_ID: 2" in updated
        assert "## Entry Schema\n\n> **Format rules:**" in updated
        assert "## Entries\n\n- [ ] **TODO-1: Existing**" in updated
        assert any(
            "inserted NEXT_TODO_ID" in message
            or "corrected NEXT_TODO_ID" in message
            for message in messages
        )

    def test_reconcile_repairs_misplaced_metadata_under_entries(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "## Metadata\n\n"
            "\n"
            "## Entry Schema\n\n"
            "> **Format rules:**\n\n"
            "## Entries\n\n"
            "- [ ] **TODO-7: Existing** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n\n"
            "NEXT_TODO_ID: 99\n",
            encoding="utf-8",
        )

        reconciled, messages = reconcile_next_todo_id(tmp_path, "audit")

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert reconciled == 8
        assert updated.count("NEXT_TODO_ID:") == 1
        assert "## Metadata\n\nNEXT_TODO_ID: 8" in updated
        assert "NEXT_TODO_ID: 99" not in updated
        assert any("outside ## Metadata" in message for message in messages)

    def test_atomic_update_todos_rolls_back_when_replace_fails(self, tmp_path, monkeypatch):
        from tests.skill_test_environment import skill_logic

        todos = tmp_path / "TODOS.md"
        original = "# TODOS\n\nNEXT_TODO_ID: 8\n\n- [ ] TODO-7: Existing\n"
        todos.write_text(original, encoding="utf-8")

        def fail_replace(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(skill_logic.os, "replace", fail_replace)

        with pytest.raises(OSError):
            skill_logic.atomic_update_todos(todos, lambda text: text.replace("8", "9", 1))

        assert todos.read_text(encoding="utf-8") == original

    def test_assign_next_todo_id_repairs_conflict_before_returning(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\nNEXT_TODO_ID: 7\n\n- [ ] TODO-7: Existing\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        assigned, messages = assign_next_todo_id(
            tmp_path, lambda todo_id: f"- [ ] TODO-{todo_id}: Added"
        )

        assert assigned == 8
        assert "NEXT_TODO_ID: 9" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert "TODO-8: Added" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert any("corrected NEXT_TODO_ID" in message for message in messages)

    def test_assign_next_todo_id_repairs_stale_high_tracked_value(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 50\n\n"
            "## Entry Schema\n\n> **Format rules:**\n\n"
            "## Entries\n\n- [ ] TODO-7: Existing\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        assigned, messages = assign_next_todo_id(
            tmp_path, lambda todo_id: f"- [ ] TODO-{todo_id}: Added"
        )

        assert assigned == 8
        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert "NEXT_TODO_ID: 9" in updated
        assert "TODO-8: Added" in updated
        assert any("corrected NEXT_TODO_ID from 50 to 8" in message for message in messages)

    def test_assign_next_todo_id_uses_consistent_tracked_value(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\nNEXT_TODO_ID: 8\n\n- [ ] TODO-7: Existing\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        assigned, _ = assign_next_todo_id(
            tmp_path, lambda todo_id: f"- [ ] TODO-{todo_id}: Added"
        )

        assert assigned == 8
        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert "NEXT_TODO_ID: 9" in updated
        assert "TODO-8: Added" in updated

    def test_assign_next_todo_id_requires_entry_builder(self, tmp_path):
        todos = tmp_path / "TODOS.md"
        original = "# TODOS\n\nNEXT_TODO_ID: 1\n"
        todos.write_text(original, encoding="utf-8")

        with pytest.raises(TypeError):
            assign_next_todo_id(tmp_path)  # type: ignore[call-arg]

        assert todos.read_text(encoding="utf-8") == original

    def test_assign_next_todo_id_serializes_a_forced_stale_writer(self, tmp_path, monkeypatch):
        from tests.skill_test_environment import skill_logic

        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\nNEXT_TODO_ID: 1\n", encoding="utf-8"
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")
        context = mp.get_context("fork")
        lock_attempts = context.Value("i", 0)
        replacements = context.Value("i", 0)
        flock_calls = context.Value("i", 0)
        second_attempted = context.Event()
        first_ready_to_replace = context.Event()
        original_flock = skill_logic.fcntl.flock

        def before_lock():
            with lock_attempts.get_lock():
                lock_attempts.value += 1
                if lock_attempts.value == 2:
                    second_attempted.set()

        def before_replace():
            with replacements.get_lock():
                replacements.value += 1
                is_first_replacement = replacements.value == 1
            if is_first_replacement:
                first_ready_to_replace.set()
                assert second_attempted.wait(timeout=3)

        def trace_flock(fd, operation):
            with flock_calls.get_lock():
                flock_calls.value += 1
            return original_flock(fd, operation)

        monkeypatch.setattr(skill_logic, "_before_todo_lock", before_lock)
        monkeypatch.setattr(skill_logic, "_before_todos_replace", before_replace)
        monkeypatch.setattr(skill_logic.fcntl, "flock", trace_flock)
        results = context.Queue()
        first_worker = context.Process(
            target=_assign_todo_id_in_process,
            args=(str(tmp_path), results),
        )
        second_worker = context.Process(
            target=_assign_todo_id_in_process,
            args=(str(tmp_path), results),
        )
        first_worker.start()
        assert first_ready_to_replace.wait(timeout=3)
        second_worker.start()

        assigned = sorted(results.get(timeout=3) for _ in range(2))
        for worker in (first_worker, second_worker):
            worker.join(timeout=3)
            assert worker.exitcode == 0

        assert lock_attempts.value == 2
        assert replacements.value == 2
        assert flock_calls.value == 4
        assert assigned == [1, 2]
        assert "NEXT_TODO_ID: 3" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")

    def test_assign_next_todo_id_removes_temporary_file_when_replace_fails(
        self, tmp_path, monkeypatch
    ):
        from tests.skill_test_environment import skill_logic

        todos = tmp_path / "TODOS.md"
        original = "# TODOS\n\nNEXT_TODO_ID: 1\n"
        todos.write_text(original, encoding="utf-8")
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        def fail_replace(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(skill_logic.os, "replace", fail_replace)

        with pytest.raises(OSError):
            skill_logic.assign_next_todo_id(
                tmp_path, lambda todo_id: f"- [ ] TODO-{todo_id}: Added"
            )

        assert todos.read_text(encoding="utf-8") == original
        assert not list(tmp_path.glob(".TODOS.*"))

    def test_reconcile_next_todo_id_rolls_back_when_replace_fails(self, tmp_path, monkeypatch):
        from tests.skill_test_environment import skill_logic

        todos = tmp_path / "TODOS.md"
        original = "# TODOS\n\nNEXT_TODO_ID: 1\n\n- [ ] TODO-4: Existing\n"
        todos.write_text(original, encoding="utf-8")
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        def fail_replace(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(skill_logic.os, "replace", fail_replace)

        with pytest.raises(OSError):
            skill_logic.reconcile_next_todo_id(tmp_path, mode="audit")

        assert todos.read_text(encoding="utf-8") == original

    def test_compute_scan_next_id_matches_existing_scan(self, tmp_path):
        todos = tmp_path / "TODOS.md"
        archive = tmp_path / "TODOS-archive.md"
        todos.write_text("# TODOS\n\n- [ ] TODO-4: Existing\n", encoding="utf-8")
        archive.write_text("- [x] TODO-7: Archived\n", encoding="utf-8")

        assert compute_scan_next_id(todos, archive) == 8

    def test_reconcile_repairs_stale_low_value_from_archive(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 3\n\n"
            "## Entry Schema\n\n> **Format rules:**\n\n"
            "## Entries\n\n- [ ] TODO-1: A\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text(
            "- [x] TODO-7: Archived\n", encoding="utf-8"
        )

        next_id, messages = reconcile_next_todo_id(tmp_path, mode="audit")

        assert next_id == 8
        assert "NEXT_TODO_ID: 8" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert any("corrected NEXT_TODO_ID from 3 to 8" in message for message in messages)

    def test_reconcile_repairs_stale_high_value(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: 50\n\n"
            "## Entry Schema\n\n> **Format rules:**\n\n"
            "## Entries\n\n- [ ] TODO-7: Existing\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        next_id, messages = reconcile_next_todo_id(tmp_path, mode="audit")

        assert next_id == 8
        assert "NEXT_TODO_ID: 8" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert any("corrected NEXT_TODO_ID from 50 to 8" in message for message in messages)

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
        assert "NEXT_TODO_ID: 5" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert any("inserted NEXT_TODO_ID: 5" in message for message in messages)

    def test_reconcile_repairs_metadata_embedded_in_format_heading(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "> **Format rules (enforced by `todos-manager` skill): NEXT_TODO_ID: 3**\n"
            "> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`\n\n"
            "- [ ] TODO-4: Existing\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        next_id, messages = reconcile_next_todo_id(tmp_path, mode="audit")

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert next_id == 5
        assert "> **Format rules (enforced by `todos-manager` skill):**\n" in updated
        assert "NEXT_TODO_ID: 5\n" in updated
        assert "NEXT_TODO_ID: 3**" not in updated
        assert any("inserted NEXT_TODO_ID: 5" in message for message in messages)

    def test_reconcile_removes_duplicate_metadata_lines(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n"
            "NEXT_TODO_ID: 2\n"
            "NEXT_TODO_ID: 9\n\n"
            "- [ ] TODO-1: Existing\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        next_id, _ = reconcile_next_todo_id(tmp_path, mode="audit")

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert next_id == 2
        assert updated.count("NEXT_TODO_ID:") == 1
        assert "NEXT_TODO_ID: 2" in updated

    def test_reconcile_repairs_malformed_present_value(self, tmp_path):
        (tmp_path / "TODOS.md").write_text(
            "# TODOS\n\n## Metadata\n\nNEXT_TODO_ID: invalid\n\n"
            "## Entry Schema\n\n> **Format rules:**\n\n"
            "## Entries\n\n- [ ] TODO-4: Existing\n",
            encoding="utf-8",
        )
        (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

        next_id, messages = reconcile_next_todo_id(tmp_path, mode="audit")

        updated = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert next_id == 5
        assert updated.count("NEXT_TODO_ID:") == 1
        assert "NEXT_TODO_ID: 5" in updated
        assert any("positive base-10 integer" in message for message in messages)


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
