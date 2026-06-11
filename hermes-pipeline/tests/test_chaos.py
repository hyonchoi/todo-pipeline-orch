"""Chaos tests for merge.py — process kill and recovery scenarios."""

import subprocess
import pytest
from unittest.mock import Mock, patch
from pathlib import Path
from hermes_pipeline.state import State
from hermes_pipeline.merge import run_phase9, MergeError


class TestChaosKilledBetweenVersionAndMerge:
    """Test recovery when process is killed between VERSION write and git merge."""

    def test_killed_before_merge_recovers_on_retry(self, tmp_path):
        """
        Simulate process kill after VERSION write but before git merge.

        Scenario:
        1. First attempt: VERSION is written, but git merge raises RuntimeError (simulating kill)
           → merge_status stays "pending" (before set_merge_status call)
           → lock is still held
        2. Second attempt: merge_status is still "pending", so run_phase9 can be retried
           → subprocess.run succeeds this time
           → merge_status becomes "merged"
        """
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)

        # First attempt: process is killed (or RuntimeError equivalent)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = RuntimeError("Process killed (simulated)")
            try:
                run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)
            except RuntimeError:
                pass  # Expected: process was killed

        # Check VERSION was written (before kill)
        assert (project_dir / "VERSION").read_text().strip() == "1.0.1"

        # Check merge_status is still pending (set_merge_status wasn't called before kill)
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "pending"

        # Check lock is still held (unlock wasn't called before kill)
        assert state.is_locked()

        # Second attempt: retry succeeds (mock_run.side_effect = None means no error)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = None  # No error this time
            run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        # Check merge_status is now merged
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "merged"

        # Check lock was released
        assert not state.is_locked()

    def test_version_file_double_bumped_on_kill_and_retry(self, tmp_path):
        """
        VERSION gets double-bumped if process is killed before merge and retried.
        This is expected behavior: default_bump_fn reads current VERSION and increments it.
        Solution: implement idempotent version handling in a real system (e.g., check merge_status before bumping).
        """
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)

        # First attempt: process killed
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = RuntimeError("Process killed")
            try:
                run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)
            except RuntimeError:
                pass

        version_after_first = (project_dir / "VERSION").read_text().strip()
        assert version_after_first == "1.0.1"

        # Second attempt: succeeds (no error this time)
        # NOTE: merge_status is still "pending", so the bump_fn runs again
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = None
            run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        version_after_second = (project_dir / "VERSION").read_text().strip()
        # VERSION gets bumped again because merge_status was still "pending"
        assert version_after_second == "1.0.2"

    def test_changelog_updated_again_on_kill_and_retry(self, tmp_path):
        """
        CHANGELOG.md gets updated again if process is killed and retried.
        This is expected behavior: the second attempt reads VERSION (which was bumped)
        and prepends a new entry for version 1.0.2.
        """
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)

        # First attempt: process killed
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = RuntimeError("Process killed")
            try:
                run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)
            except RuntimeError:
                pass

        changelog_after_first = (project_dir / "CHANGELOG.md").read_text()
        assert "[1.0.1]" in changelog_after_first

        # Second attempt: succeeds (no error)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = None
            run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        changelog_after_second = (project_dir / "CHANGELOG.md").read_text()
        # Second attempt bumped to 1.0.2 and prepended that entry
        assert "[1.0.1]" in changelog_after_second
        assert "[1.0.2]" in changelog_after_second

    def test_git_merge_idempotency_not_guaranteed_but_recoverable(self, tmp_path):
        """
        git merge --no-ff is not idempotent (second merge may fail with "already up to date").
        But the important thing is:
        - After first attempt fails or is killed, merge_status is still "pending"
        - After second attempt, merge_status becomes "merged" even if git merge output is different
        """
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)

        # First attempt: killed
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = RuntimeError("Process killed")
            try:
                run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)
            except RuntimeError:
                pass

        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "pending"

        # Second attempt: succeeds (no error)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = None
            run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "merged"


class TestChaosLockHeldOnMergeFailure:
    """Test that lock is held when git merge fails (not during kill)."""

    def test_lock_held_when_git_merge_fails(self, tmp_path):
        """Lock should be held if git merge fails (merge conflict, etc.)."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git merge", stderr="Merge conflict in main.py"
            )
            run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        # Lock should still be held
        assert state.is_locked()

        # merge_status should be "failed"
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "failed"
        assert "conflict" in rec.error.lower()

    def test_failed_merge_can_be_retried_after_manual_fix(self, tmp_path):
        """User can manually fix conflict and retry."""
        state = State("demo", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "VERSION").write_text("1.0.0\n")

        state.write_ready_for_review_min(42, "feat/test", "https://pr.url", None)
        state.lock()

        kanban = Mock()
        confirm_fn = Mock(return_value=True)

        # First attempt: merge fails
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "git merge", stderr="Merge conflict"
            )
            run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        assert state.is_locked()
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "failed"

        # User manually fixes the conflict in git

        # Second attempt: retry (no error this time)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = None
            run_phase9(state, project_dir, 42, kanban, confirm_fn=confirm_fn)

        assert not state.is_locked()
        rec = state.read_ready_for_review(42)
        assert rec.merge_status == "merged"
