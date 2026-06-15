"""Regression tests for Phase 9 typed-confirm merge — pinned BEFORE extraction.

These tests capture the current behavior of merge.py run_phase9 so any drift
during the phases.py extraction surfaces immediately.

Pinned scenarios:
1. Exact-match typed confirm merges successfully.
2. Mismatched typed confirm aborts (sets status to rejected).
3. Missing RFR record raises MergeError.
"""

import pytest
from unittest.mock import Mock, patch

from hermes_pipeline.state import State
from hermes_pipeline.merge import run_phase9, MergeError


class TestTypedConfirmMergeRegression:
    """Regression pins for the typed-confirm merge gate."""

    def test_typed_confirm_exact_match_merges(self, tmp_path):
        """Typing TODO-7 when prompted merges and sets status=merged."""
        state = State("testproj", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Seed RFR record for TODO-7
        state.write_ready_for_review_min(
            7, "feat/todo-7", "https://pr.url/7", "TASK-7"
        )
        state.lock()

        kanban = Mock()
        bump_fn = Mock(return_value=("1.0.1", "bump"))

        # Mock user typing the correct confirmation
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = None
            # Should not raise — exact match merges
            run_phase9(
                state,
                project_dir,
                7,
                kanban,
                bump_fn=bump_fn,
                confirm_fn=lambda tid: True,
            )

        # Merge succeeded
        rec = state.read_ready_for_review(7)
        assert rec.merge_status == "merged"

        # Lock released, kanban cleared
        assert not state.is_locked()
        kanban.clear_active_task.assert_called_once_with("TASK-7")

        # Filesystem side effects
        assert (project_dir / "VERSION").read_text().strip() == "1.0.1"
        assert "1.0.1" in (project_dir / "CHANGELOG.md").read_text()

    def test_typed_confirm_mismatch_aborts(self, tmp_path):
        """Typing the wrong TODO-ID aborts and sets status=rejected."""
        state = State("testproj", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Seed RFR record for TODO-7
        state.write_ready_for_review_min(
            7, "feat/todo-7", "https://pr.url/7", "TASK-7"
        )
        state.lock()

        kanban = Mock()

        # Mock user typing a different ID — mismatch aborts
        run_phase9(
            state,
            project_dir,
            7,
            kanban,
            bump_fn=Mock(return_value=("1.0.1", "bump")),
            confirm_fn=lambda tid: False,
        )

        # Merge was rejected — never advanced
        rec = state.read_ready_for_review(7)
        assert rec.merge_status == "rejected"

        # Lock released, kanban cleared on reject
        assert not state.is_locked()
        kanban.clear_active_task.assert_called_once_with("TASK-7")

    def test_no_ready_for_review_record_is_error(self, tmp_path):
        """Missing RFR record raises MergeError."""
        state = State("testproj", tmp_path, tmp_path, tmp_path)
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # No RFR record written for TODO-7

        kanban = Mock()

        with pytest.raises(MergeError, match="No ready_for_review record"):
            run_phase9(
                state,
                project_dir,
                7,
                kanban,
            )
