"""Tests for state.py — State class, locks, checkpoints."""

import json
import pytest
from pathlib import Path
from hermes_pipeline.state import State


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
