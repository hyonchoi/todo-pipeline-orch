"""Tests for merge.py — Phase 9 merge orchestration."""

import subprocess
import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch, call
from hermes_pipeline.state import State, ReadyForReview
from hermes_pipeline.merge import (
    run_phase9,
    abandon,
    MergeError,
    default_confirm_fn,
    default_bump_fn,
)


class TestDefaultConfirmFn:
    """Test default e2e confirmation function."""

    def test_default_confirm_fn_accepts_correct_input(self, monkeypatch):
        """default_confirm_fn should return True for correct input."""
        monkeypatch.setattr("builtins.input", lambda _: "TODO-42")
        assert default_confirm_fn(42) is True

    def test_default_confirm_fn_rejects_incorrect_input(self, monkeypatch):
        """default_confirm_fn should return False for incorrect input."""
        monkeypatch.setattr("builtins.input", lambda _: "wrong")
        assert default_confirm_fn(42) is False

    def test_default_confirm_fn_strips_whitespace(self, monkeypatch):
        """default_confirm_fn should strip whitespace from input."""
        monkeypatch.setattr("builtins.input", lambda _: "  TODO-42  ")
        assert default_confirm_fn(42) is True


class TestDefaultBumpFn:
    """Test default semver bump function."""

    def test_default_bump_fn_creates_version_if_missing(self, tmp_path, monkeypatch):
        """default_bump_fn should return 0.1.1 if VERSION doesn't exist."""
        monkeypatch.chdir(tmp_path)
        rec = ReadyForReview(
            project="test",
            todo_id=1,
            branch="feat/test",
            pr_url="https://example.com",
            phase_summaries={},
            kanban_task_id=None,
        )
        version, label = default_bump_fn(rec)
        assert version == "0.1.1"
        assert "0.1.1" in label

    def test_default_bump_fn_increments_patch(self, tmp_path, monkeypatch):
        """default_bump_fn should increment patch version."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "VERSION").write_text("1.2.3\n")
        rec = ReadyForReview(
            project="test",
            todo_id=1,
            branch="feat/test",
            pr_url="https://example.com",
            phase_summaries={},
            kanban_task_id=None,
        )
        version, label = default_bump_fn(rec)
        assert version == "1.2.4"


class TestRunPhase9:
    """Test run_phase9 main workflow."""

    def test_phase9_raises_if_no_ready_for_review_record(self, tmp_path):
        """run_phase9 should raise MergeError if record doesn't exist."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        kanban = Mock()

        with pytest.raises(MergeError, match="No ready_for_review record"):
            run_phase9(state, project_dir, 42, kanban)

    def test_phase9_raises_if_merge_status_not_pending_or_failed(self, tmp_path):
        """run_phase9 should raise MergeError if merge_status is not pending/failed."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Create record with merged status
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.set_merge_status(42, "merged")

        kanban = Mock()
        with pytest.raises(MergeError, match="merge_status=merged"):
            run_phase9(state, project_dir, 42, kanban)

    def test_phase9_rejects_merge_if_not_confirmed(self, tmp_path):
        """If user doesn't confirm, run_phase9 should set status=rejected and unlock."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", "TASK-1")
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=False)

        run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        # Check status is rejected
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "rejected"

        # Check kanban was cleared
        kanban.clear_active_task.assert_called_once_with("TASK-1")

        # Check lock was released
        assert not state.is_locked()

    def test_phase9_successful_merge(self, tmp_path, monkeypatch):
        """Successful merge should update VERSION, CHANGELOG, git merge, and unlock."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", "TASK-1")
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)
        bump_fn = Mock(return_value=("1.0.1", "patch bump"))

        # Mock git merge to succeed
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = None
            run_phase9(
                state,
                project_dir,
                42,
                kanban,
                confirm_fn=confirm_fn,
                bump_fn=bump_fn,
            )

        # Check VERSION was written
        assert (project_dir / "VERSION").read_text().strip() == "1.0.1"

        # Check CHANGELOG was updated
        changelog = (project_dir / "CHANGELOG.md").read_text()
        assert "1.0.1" in changelog
        assert "feat/test" in changelog

        # Check git merge was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["git", "merge", "--no-ff", "feat/test"]

        # Check status is merged
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "merged"

        # Check kanban was cleared
        kanban.clear_active_task.assert_called_once_with("TASK-1")

        # Check lock was released
        assert not state.is_locked()

    def test_phase9_git_merge_failure_keeps_lock(self, tmp_path, monkeypatch):
        """If git merge fails, status should be failed and lock should be held."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)
        bump_fn = Mock(return_value=("1.0.1", "patch bump"))

        # Mock git merge to fail
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git merge", stderr="Merge conflict in main.py"
            )
            run_phase9(
                state,
                project_dir,
                42,
                kanban,
                confirm_fn=confirm_fn,
                bump_fn=bump_fn,
            )

        # Check status is failed with error message
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "failed"
        assert "Merge conflict" in rec.error

        # Check lock is still held
        assert state.is_locked()

    def test_phase9_uses_default_confirm_fn(self, tmp_path, monkeypatch):
        """run_phase9 should use default_confirm_fn if not provided."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        bump_fn = Mock(return_value=("1.0.1", "bump"))

        # Mock input to reject
        monkeypatch.setattr("builtins.input", lambda _: "wrong")

        with patch("subprocess.run"):
            run_phase9(state, project_dir, 42, kanban, bump_fn=bump_fn)

        # Status should be rejected (default_confirm_fn was used and returned False)
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "rejected"

    def test_phase9_uses_default_bump_fn(self, tmp_path, monkeypatch):
        """run_phase9 should use default_bump_fn if not provided."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)

        with patch("subprocess.run"):
            run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        # Check VERSION was bumped using default_bump_fn (patch increment)
        assert (project_dir / "VERSION").read_text().strip() == "1.0.1"

    def test_phase9_handles_kanban_error_gracefully(self, tmp_path):
        """run_phase9 should not fail if kanban.clear_active_task raises."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", "TASK-1")
        state.lock()

        kanban = Mock()
        kanban.clear_active_task.side_effect = Exception("Kanban unreachable")
        confirm_fn = Mock(return_value=True)
        bump_fn = Mock(return_value=("1.0.1", "bump"))

        with patch("subprocess.run"):
            # Should not raise even though kanban.clear_active_task failed
            run_phase9(
                state,
                project_dir,
                42,
                kanban,
                confirm_fn=confirm_fn,
                bump_fn=bump_fn,
            )

        # Status should still be merged
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "merged"

    def test_phase9_retryable_after_failure(self, tmp_path):
        """After git merge failure, running Phase 9 again should be retryable."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)
        bump_fn = Mock(return_value=("1.0.1", "bump"))

        # First attempt: merge fails
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git merge", stderr="conflict"
            )
            run_phase9(
                state,
                project_dir,
                42,
                kanban,
                confirm_fn=confirm_fn,
                bump_fn=bump_fn,
            )

        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "failed"

        # Lock should still be held (user fixes conflict manually)
        assert state.is_locked()

        # Second attempt: merge succeeds (user fixed conflict)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = None
            run_phase9(
                state,
                project_dir,
                42,
                kanban,
                confirm_fn=confirm_fn,
                bump_fn=bump_fn,
            )

        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "merged"
        assert not state.is_locked()


class TestAbandon:
    """Test abandon function."""

    def test_abandon_sets_status_abandoned(self, tmp_path):
        """abandon() should set status to abandoned."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        abandon(state, 42, kanban)

        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "abandoned"

    def test_abandon_clears_kanban(self, tmp_path):
        """abandon() should clear kanban task."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", "TASK-1")
        state.lock()

        kanban = Mock()
        abandon(state, 42, kanban)

        kanban.clear_active_task.assert_called_once_with("TASK-1")

    def test_abandon_unlocks(self, tmp_path):
        """abandon() should release the lock."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        abandon(state, 42, kanban)

        assert not state.is_locked()

    def test_abandon_handles_missing_record(self, tmp_path):
        """abandon() should not fail if record doesn't exist."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        state.lock()

        kanban = Mock()
        # Should not raise
        abandon(state, 999, kanban)

        assert not state.is_locked()
