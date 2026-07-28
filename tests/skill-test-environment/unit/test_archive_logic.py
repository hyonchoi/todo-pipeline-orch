"""Tests for archive logic — finding completed entries and simulating archive movement."""

import json
import multiprocessing as mp
from pathlib import Path

import pytest

from tests.skill_test_environment import skill_logic
from tests.skill_test_environment.skill_logic import (
    archive_completed_todos,
    assign_next_todo_id,
    extract_entry_blocks,
    find_completed_entries,
    scan_ids,
    simulate_archive,
)


def _write_archive_project(project_dir: Path, *, completed: bool = True) -> None:
    status = "[x]" if completed else "[ ]"
    project_dir.joinpath("TODOS.md").write_text(
        "# TODOS\n\n"
        "## Metadata\n\n"
        "NEXT_TODO_ID: 3\n\n"
        "## Entry Schema\n\n"
        "- [ ] **TODO-<n>: Example** — Summary\n\n"
        "## Entries\n\n"
        "- [ ] **TODO-1: Active** — Summary\n"
        "  - **What:** Keep\n"
        "  - **Why:** Still pending\n"
        "  - **Decisions:** Priority `P2`\n\n"
        f"- {status} **TODO-2: Done** — Summary\n"
        "  - **What:** Move\n"
        "  - **Why:** Finished\n"
        "  - **Decisions:** Priority `P1`\n",
        encoding="utf-8",
    )
    project_dir.joinpath("TODOS-archive.md").write_text(
        "# TODOS Archive\n\nCompleted TODOs, archived via `todos-manager --archive`.\n\n",
        encoding="utf-8",
    )


def _archive_in_process(project_dir: str, results) -> None:
    try:
        results.put(archive_completed_todos(Path(project_dir)))
    except BaseException as exc:
        results.put(repr(exc))


def _assign_in_process(project_dir: str, results) -> None:
    assigned, _ = assign_next_todo_id(
        Path(project_dir),
        entry_builder=lambda todo_id: f"- [ ] **TODO-{todo_id}: New** — Summary\n"
        "  - **What:** Add\n"
        "  - **Why:** Concurrent write\n"
        "  - **Decisions:** Priority `P3`",
    )
    results.put(assigned)


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

    def test_schema_completed_example_is_not_archived(self):
        todos = (
            "# TODOS\n\n"
            "## Metadata\n\n"
            "NEXT_TODO_ID: 4\n\n"
            "## Entry Schema\n\n"
            "- [x] **TODO-99: Done Example** — Documentation only\n\n"
            "## Entries\n\n"
            "- [x] **TODO-3: Done Real** — Summary\n"
            "  - **What:** Work\n"
            "  - **Why:** Reason\n"
            "  - **Decisions:** Priority `P1`\n"
        )

        completed = find_completed_entries(todos)
        blocks = extract_entry_blocks(todos)
        new_todos, new_archive = simulate_archive(todos, "")

        assert [entry["id"] for entry in completed] == [3]
        assert len(blocks) == 1
        assert "TODO-99" in new_todos
        assert "TODO-3" not in new_todos
        assert "TODO-3" in new_archive


class TestArchiveTransaction:
    def test_archive_recovers_after_archive_replace_before_active_replace(
        self, tmp_path, monkeypatch
    ):
        _write_archive_project(tmp_path)
        calls = 0

        def fail_after_archive(target: Path) -> None:
            nonlocal calls
            if target.name == "TODOS-archive.md" and calls == 0:
                calls += 1
                raise RuntimeError("simulated crash")

        monkeypatch.setattr(skill_logic, "_after_todo_transaction_replace", fail_after_archive)

        with pytest.raises(RuntimeError, match="simulated crash"):
            archive_completed_todos(tmp_path)

        todos_after_crash = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        archive_after_crash = (tmp_path / "TODOS-archive.md").read_text(encoding="utf-8")
        assert "TODO-2" in todos_after_crash
        assert "TODO-2" in archive_after_crash
        assert list((tmp_path / ".hermes").glob("todo-archive-*.json"))

        monkeypatch.setattr(skill_logic, "_after_todo_transaction_replace", lambda _target: None)
        assert archive_completed_todos(tmp_path) == 0

        recovered_todos = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        recovered_archive = (tmp_path / "TODOS-archive.md").read_text(encoding="utf-8")
        assert "TODO-2" not in recovered_todos
        assert recovered_archive.count("TODO-2") == 1
        assert not list((tmp_path / ".hermes").glob("todo-archive-*"))

    def test_archive_happy_path_cleans_transaction_files(self, tmp_path):
        _write_archive_project(tmp_path)

        assert archive_completed_todos(tmp_path) == 1

        assert "TODO-2" not in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        assert "TODO-2" in (tmp_path / "TODOS-archive.md").read_text(encoding="utf-8")
        assert not list((tmp_path / ".hermes").glob("todo-archive-*"))

    def test_archive_no_completed_entries_does_not_write_transaction(self, tmp_path):
        _write_archive_project(tmp_path, completed=False)
        original_todos = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        original_archive = (tmp_path / "TODOS-archive.md").read_text(encoding="utf-8")

        assert archive_completed_todos(tmp_path) == 0

        assert (tmp_path / "TODOS.md").read_text(encoding="utf-8") == original_todos
        assert (tmp_path / "TODOS-archive.md").read_text(encoding="utf-8") == original_archive
        assert not (tmp_path / ".hermes").exists()

    def test_recovery_rejects_journal_targets_outside_project(self, tmp_path):
        _write_archive_project(tmp_path)
        hermes_dir = tmp_path / ".hermes"
        hermes_dir.mkdir()
        outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
        outside.write_text("do not overwrite\n", encoding="utf-8")
        todos_payload = hermes_dir / "todo-archive-forged.todos.payload"
        archive_payload = hermes_dir / "todo-archive-forged.archive.payload"
        todos_payload.write_text("forged todos\n", encoding="utf-8")
        archive_payload.write_text("forged archive\n", encoding="utf-8")
        journal = hermes_dir / "todo-archive-forged.json"
        journal.write_text(
            json.dumps(
                {
                    "todos_target": str(outside),
                    "archive_target": str(tmp_path / "TODOS-archive.md"),
                    "todos_payload": str(todos_payload),
                    "archive_payload": str(archive_payload),
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="Malformed TODO archive transaction journal"):
            archive_completed_todos(tmp_path)

        assert outside.read_text(encoding="utf-8") == "do not overwrite\n"

    def test_archive_serializes_with_todo_add(self, tmp_path, monkeypatch):
        _write_archive_project(tmp_path)
        context = mp.get_context("fork")
        first_archive_replace = context.Event()
        add_attempted = context.Event()
        replacements = context.Value("i", 0)

        def after_replace(target: Path) -> None:
            if target.name != "TODOS-archive.md":
                return
            with replacements.get_lock():
                replacements.value += 1
                is_first = replacements.value == 1
            if is_first:
                first_archive_replace.set()
                assert add_attempted.wait(timeout=3)

        def before_lock() -> None:
            if first_archive_replace.is_set():
                add_attempted.set()

        monkeypatch.setattr(skill_logic, "_after_todo_transaction_replace", after_replace)
        monkeypatch.setattr(skill_logic, "_before_todo_lock", before_lock)
        results = context.Queue()
        archive_worker = context.Process(target=_archive_in_process, args=(str(tmp_path), results))
        add_worker = context.Process(target=_assign_in_process, args=(str(tmp_path), results))

        archive_worker.start()
        assert first_archive_replace.wait(timeout=3)
        add_worker.start()

        observed = [results.get(timeout=3) for _ in range(2)]
        for worker in (archive_worker, add_worker):
            worker.join(timeout=3)
            assert worker.exitcode == 0

        todos = (tmp_path / "TODOS.md").read_text(encoding="utf-8")
        archive = (tmp_path / "TODOS-archive.md").read_text(encoding="utf-8")
        assert sorted(observed) == [1, 3]
        assert "TODO-2" not in todos
        assert archive.count("TODO-2") == 1
        assert "TODO-3" in todos
