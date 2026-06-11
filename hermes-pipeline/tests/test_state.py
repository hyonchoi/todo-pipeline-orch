"""Tests for state.py — State class, locks, checkpoints, ready-for-review records."""

import json
import pytest
from pathlib import Path
from hermes_pipeline.state import State, ReadyForReview


class TestStateLocks:
    """Test lock acquisition and release."""

    def test_is_locked_false_initially(self, tmp_path):
        """Lock should not exist initially."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        assert not state.is_locked()

    def test_lock_creates_lock_file(self, tmp_path):
        """lock() should create an exclusive lock file."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.lock()
        assert state.is_locked()
        assert (tmp_path / "demo.lock").exists()

    def test_lock_fails_if_already_locked(self, tmp_path):
        """lock() should raise FileExistsError if already locked."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.lock()
        with pytest.raises(FileExistsError):
            state.lock()

    def test_unlock_removes_lock_file(self, tmp_path):
        """unlock() should remove the lock file."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.lock()
        assert state.is_locked()
        state.unlock()
        assert not state.is_locked()

    def test_unlock_idempotent(self, tmp_path):
        """unlock() should not fail if lock doesn't exist."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.unlock()  # Should not raise


class TestStateHash:
    """Test TODOS.md hash save/restore."""

    def test_get_saved_hash_returns_none_initially(self, tmp_path):
        """get_saved_hash() should return None if not saved."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        assert state.get_saved_hash() is None

    def test_save_and_get_hash(self, tmp_path):
        """save_hash() should persist and get_saved_hash() should retrieve."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        test_hash = "abc123def456"
        state.save_hash(test_hash)
        assert state.get_saved_hash() == test_hash

    def test_hash_file_is_created(self, tmp_path):
        """save_hash() should create .todos_hash file."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.save_hash("abc123")
        hash_file = tmp_path / "demo.todos_hash"
        assert hash_file.exists()
        assert hash_file.read_text().strip() == "abc123"


class TestStateCheckpoints:
    """Test checkpoint management."""

    def test_last_completed_phase_index_returns_minus_one_if_no_checkpoint(self, tmp_path):
        """last_completed_phase_index() should return -1 if no checkpoint."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        assert state.last_completed_phase_index(42) == -1

    def test_mark_phase_done_creates_checkpoint(self, tmp_path):
        """mark_phase_done() should create a checkpoint JSON."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.mark_phase_done(42, "P1_research", 0)
        assert state.last_completed_phase_index(42) == 0

    def test_mark_phase_done_updates_existing_checkpoint(self, tmp_path):
        """mark_phase_done() should update an existing checkpoint."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.mark_phase_done(42, "P1_research", 0)
        state.mark_phase_done(42, "P2_design", 1)
        assert state.last_completed_phase_index(42) == 1

    def test_checkpoint_contains_phase_key(self, tmp_path):
        """Checkpoint JSON should track completed phase keys."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.mark_phase_done(42, "P1_research", 0)
        state.mark_phase_done(42, "P2_design", 1)

        checkpoint_path = tmp_path / "todo-42.json"
        data = json.loads(checkpoint_path.read_text())
        assert data["completed_phases"]["P1_research"] is True
        assert data["completed_phases"]["P2_design"] is True

    def test_reset_clears_checkpoint(self, tmp_path):
        """reset() should delete the checkpoint file."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.mark_phase_done(42, "P1_research", 0)
        assert state.last_completed_phase_index(42) == 0

        state.reset(42)
        assert state.last_completed_phase_index(42) == -1
        assert not (tmp_path / "todo-42.json").exists()


class TestReadyForReviewRecord:
    """Test ReadyForReview dataclass."""

    def test_to_json_serialization(self):
        """ReadyForReview.to_json() should produce valid JSON."""
        rec = ReadyForReview(
            project="demo",
            todo_id=42,
            branch="feat/new-feature",
            pr_url="https://github.com/org/repo/pull/123",
            phase_summaries={"P1": "Researched", "P2": "Designed"},
            kanban_task_id="TASK-123",
        )
        json_str = rec.to_json()
        parsed = json.loads(json_str)
        assert parsed["todo_id"] == 42
        assert parsed["branch"] == "feat/new-feature"

    def test_from_json_deserialization(self):
        """ReadyForReview.from_json() should restore all fields."""
        json_str = json.dumps({
            "project": "demo",
            "todo_id": 42,
            "branch": "feat/new-feature",
            "pr_url": "https://github.com/org/repo/pull/123",
            "phase_summaries": {"P1": "Researched"},
            "kanban_task_id": "TASK-123",
            "merge_status": "pending",
            "error": None,
            "created_at": "2026-06-11T12:00:00Z",
        })
        rec = ReadyForReview.from_json(json_str)
        assert rec.project == "demo"
        assert rec.todo_id == 42
        assert rec.merge_status == "pending"

    def test_roundtrip_serialization(self):
        """Serialization and deserialization should be idempotent."""
        rec1 = ReadyForReview(
            project="demo",
            todo_id=42,
            branch="feat/test",
            pr_url="https://example.com/pr/1",
            phase_summaries={"P1": "Done"},
            kanban_task_id="TSK-1",
            merge_status="pending",
        )
        json_str = rec1.to_json()
        rec2 = ReadyForReview.from_json(json_str)
        assert rec1 == rec2


class TestStateReadyForReview:
    """Test state.py ready-for-review record operations."""

    def test_write_and_read_ready_for_review(self, tmp_path):
        """write_ready_for_review() and read_ready_for_review() should round-trip."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        rec = ReadyForReview(
            project="demo",
            todo_id=42,
            branch="feat/test",
            pr_url="https://example.com/pr/1",
            phase_summaries={"P1": "Done"},
            kanban_task_id="TSK-1",
        )
        state.write_ready_for_review(rec)

        read_rec = state.read_ready_for_review(42)
        assert read_rec is not None
        assert read_rec.todo_id == 42
        assert read_rec.branch == "feat/test"

    def test_read_ready_for_review_returns_none_if_missing(self, tmp_path):
        """read_ready_for_review() should return None if record doesn't exist."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        assert state.read_ready_for_review(999) is None

    def test_write_ready_for_review_min(self, tmp_path):
        """write_ready_for_review_min() should create a minimal record."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", "TSK-1")

        rec = state.read_ready_for_review(42)
        assert rec is not None
        assert rec.todo_id == 42
        assert rec.branch == "feat/test"
        assert rec.pr_url == "https://pr.url"
        assert rec.kanban_task_id == "TSK-1"
        assert rec.phase_summaries == {}
        assert rec.merge_status == "pending"

    def test_set_merge_status(self, tmp_path):
        """set_merge_status() should update merge_status and error."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)

        state.set_merge_status(42, "merged")
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "merged"

    def test_set_merge_status_with_error(self, tmp_path):
        """set_merge_status() should record error if provided."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)

        state.set_merge_status(42, "failed", "Merge conflict in main.py")
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "failed"
        assert rec.error == "Merge conflict in main.py"

    def test_set_merge_status_fails_if_no_record(self, tmp_path):
        """set_merge_status() should raise ValueError if record doesn't exist."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        with pytest.raises(ValueError):
            state.set_merge_status(999, "merged")

    def test_list_ready_for_review_pending(self, tmp_path):
        """list_ready_for_review_pending() should return pending and failed records."""
        state = State("demo", tmp_path, tmp_path, tmp_path)

        # Create pending record
        state.write_ready_for_review_min(1, "feat/a", "https://pr.a", None)

        # Create failed record
        state.write_ready_for_review_min(2, "feat/b", "https://pr.b", None)
        state.set_merge_status(2, "failed", "error")

        # Create merged record (should not appear)
        state.write_ready_for_review_min(3, "feat/c", "https://pr.c", None)
        state.set_merge_status(3, "merged")

        # Create rejected record (should not appear)
        state.write_ready_for_review_min(4, "feat/d", "https://pr.d", None)
        state.set_merge_status(4, "rejected")

        pending = state.list_ready_for_review_pending()
        assert len(pending) == 2
        assert {rec.todo_id for rec in pending} == {1, 2}
        assert any(rec.merge_status == "pending" for rec in pending)
        assert any(rec.merge_status == "failed" for rec in pending)

    def test_list_ready_for_review_pending_empty(self, tmp_path):
        """list_ready_for_review_pending() should return [] if no pending records."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        pending = state.list_ready_for_review_pending()
        assert pending == []

    def test_list_ready_for_review_pending_ignores_other_statuses(self, tmp_path):
        """list_ready_for_review_pending() should exclude merged/rejected/abandoned."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        for i, status in enumerate(["merged", "rejected", "abandoned"], start=1):
            state.write_ready_for_review_min(i, f"feat/{status}", f"https://pr.{i}", None)
            state.set_merge_status(i, status)

        pending = state.list_ready_for_review_pending()
        assert pending == []
