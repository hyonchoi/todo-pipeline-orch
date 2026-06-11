"""Tests for runner.py: branch naming and phase loop orchestration."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_pipeline.phases import Phase
from hermes_pipeline.runner import PipelineRunner, decide_branch_name
from hermes_pipeline.state import State


def _git_branch_list(out: str):
    """Mock subprocess.run return for git branch --list."""
    return MagicMock(returncode=0, stdout=out, stderr="")


# ============================================================================
# TE.1: decide_branch_name tests
# ============================================================================


def test_branch_first_attempt(tmp_path):
    """First attempt (not a new attempt): return feat/{base}-{slug}."""
    with patch("subprocess.run", return_value=_git_branch_list("")):
        name = decide_branch_name(
            project_dir=tmp_path,
            base_version="0.1.0",
            slug="cool",
            is_new_attempt=False,
            prior_attempt_branch_existed=False,
        )
    assert name == "feat/0.1.0-cool"


def test_branch_checkpoint_resume_reuses_name(tmp_path):
    """Checkpoint resume (is_new_attempt=False, prior existed): reuse same name."""
    name = decide_branch_name(
        project_dir=tmp_path,
        base_version="0.1.0",
        slug="cool",
        is_new_attempt=False,
        prior_attempt_branch_existed=True,
    )
    assert name == "feat/0.1.0-cool"


def test_branch_new_attempt_increments_with_no_existing(tmp_path):
    """New attempt with no existing -attemptN branches: return -attempt2."""
    with patch("subprocess.run", return_value=_git_branch_list("")):
        name = decide_branch_name(
            project_dir=tmp_path,
            base_version="0.1.0",
            slug="cool",
            is_new_attempt=True,
            prior_attempt_branch_existed=True,
        )
    assert name == "feat/0.1.0-cool-attempt2"


def test_branch_scan_max_plus_one(tmp_path):
    """New attempt: scan existing -attemptN branches and return max+1."""
    # Simulate git branch output with attempt2 and attempt3
    listing = "  feat/0.1.0-cool-attempt2\n* feat/0.1.0-cool-attempt3\n"
    with patch("subprocess.run", return_value=_git_branch_list(listing)):
        name = decide_branch_name(
            project_dir=tmp_path,
            base_version="0.1.0",
            slug="cool",
            is_new_attempt=True,
            prior_attempt_branch_existed=True,
        )
    assert name == "feat/0.1.0-cool-attempt4"


# ============================================================================
# TE.2: PipelineRunner tests
# ============================================================================


def test_runner_set_active_task_called(tmp_path):
    """Runner calls set_active_task at the start."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(
            phase_key="phase_2",
            name="Phase 2: Autoplan",
            prompt="autoplan",
            tools="Read,Write",
            turns=10,
        )
    ]

    def run_phase_fn(phase: Phase) -> int:
        return 0  # success

    runner = PipelineRunner(
        project="test-project",
        project_dir=tmp_path,
        branch="feat/0.1.0-test",
        todo_id=1,
        title="Test TODO",
        phases=phases,
        state=state,
        kanban=kanban,
        run_phase_fn=run_phase_fn,
    )

    result = runner.run()

    assert result is True
    kanban.set_active_task.assert_called_once_with(
        project="test-project",
        todo_id=1,
        title="Test TODO",
        phase="Phase 2: Autoplan",
    )


def test_runner_update_phase_on_each_phase(tmp_path):
    """Runner calls update_phase for each phase."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(
            phase_key="phase_2",
            name="Phase 2: Autoplan",
            prompt="autoplan",
            tools="Read,Write",
            turns=10,
        ),
        Phase(
            phase_key="phase_3",
            name="Phase 3: Writing Plan",
            prompt="write plan",
            tools="Read,Write",
            turns=10,
        ),
    ]

    def run_phase_fn(phase: Phase) -> int:
        return 0  # success

    runner = PipelineRunner(
        project="test-project",
        project_dir=tmp_path,
        branch="feat/0.1.0-test",
        todo_id=1,
        title="Test TODO",
        phases=phases,
        state=state,
        kanban=kanban,
        run_phase_fn=run_phase_fn,
    )

    result = runner.run()

    assert result is True
    # update_phase called once per phase (all succeed)
    assert kanban.update_phase.call_count >= 2


def test_runner_returns_false_on_phase_failure(tmp_path):
    """Runner returns False if a phase fails (rc != 0)."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(
            phase_key="phase_2",
            name="Phase 2: Autoplan",
            prompt="autoplan",
            tools="Read,Write",
            turns=10,
        ),
        Phase(
            phase_key="phase_3",
            name="Phase 3: Writing Plan",
            prompt="write plan",
            tools="Read,Write",
            turns=10,
        ),
    ]

    call_count = 0

    def run_phase_fn(phase: Phase) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 0  # first phase succeeds
        return 1  # second phase fails

    runner = PipelineRunner(
        project="test-project",
        project_dir=tmp_path,
        branch="feat/0.1.0-test",
        todo_id=1,
        title="Test TODO",
        phases=phases,
        state=state,
        kanban=kanban,
        run_phase_fn=run_phase_fn,
    )

    result = runner.run()

    assert result is False
    # Should call update_phase with status "failed" when phase fails
    failed_call = [c for c in kanban.update_phase.call_args_list if "failed" in str(c)]
    assert len(failed_call) > 0


def test_runner_ready_for_review_on_success(tmp_path):
    """Runner calls write_ready_for_review_min after all phases succeed."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(
            phase_key="phase_2",
            name="Phase 2: Autoplan",
            prompt="autoplan",
            tools="Read,Write",
            turns=10,
        ),
    ]

    def run_phase_fn(phase: Phase) -> int:
        return 0  # success

    def pr_resolver():
        return "https://github.com/example/pull/1"

    runner = PipelineRunner(
        project="test-project",
        project_dir=tmp_path,
        branch="feat/0.1.0-test",
        todo_id=1,
        title="Test TODO",
        phases=phases,
        state=state,
        kanban=kanban,
        run_phase_fn=run_phase_fn,
        pr_url_resolver=pr_resolver,
    )

    result = runner.run()

    assert result is True
    # Should call write_ready_for_review_min after all phases succeed
    state.write_ready_for_review_min.assert_called_once()
    call_kwargs = state.write_ready_for_review_min.call_args[1]
    assert call_kwargs["todo_id"] == 1
    assert call_kwargs["branch"] == "feat/0.1.0-test"
    assert "github.com" in call_kwargs["pr_url"]


def test_runner_kanban_ready_for_review_status(tmp_path):
    """Runner updates kanban to 'ready_for_review' after all phases succeed."""
    kanban = MagicMock()
    kanban.set_active_task.return_value = None
    kanban.update_phase.return_value = None
    state = MagicMock(spec=State)

    phases = [
        Phase(
            phase_key="phase_2",
            name="Phase 2: Autoplan",
            prompt="autoplan",
            tools="Read,Write",
            turns=10,
        ),
    ]

    def run_phase_fn(phase: Phase) -> int:
        return 0

    runner = PipelineRunner(
        project="test-project",
        project_dir=tmp_path,
        branch="feat/0.1.0-test",
        todo_id=1,
        title="Test TODO",
        phases=phases,
        state=state,
        kanban=kanban,
        run_phase_fn=run_phase_fn,
    )

    result = runner.run()

    assert result is True
    # Should update kanban phase to "Phase 8: Finish Branch" with "ready_for_review" status
    ready_for_review_calls = [
        c
        for c in kanban.update_phase.call_args_list
        if "ready_for_review" in str(c)
    ]
    assert len(ready_for_review_calls) > 0
