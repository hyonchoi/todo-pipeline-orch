"""Tests for runner gate handling (Task 8).

Covers:
- gate_status() pure read (CP2)
- all_phases_complete() rejection sidecar exception to sentinel check
- _invoke_hermes() gate phase skip/halt
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_pipeline.gate_state import GateStatus, gate_status
from hermes_pipeline.gates import (
    PLAN_GATE_PHASE_KEY,
    read_rejection_sidecar,
    write_decision_sheet,
    write_rejection_sidecar,
)
from hermes_pipeline.kanban_tasks import (
    BLOCKED,
    KanbanTaskInfo,
    all_phases_complete,
)
from hermes_pipeline.phases import Phase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_expected_phases(state_dir: Path | str, keys: list[str]) -> None:
    """Write the expected-phases sentinel."""
    sd = Path(state_dir)
    (sd / "outcomes").mkdir(parents=True, exist_ok=True)
    (sd / "outcomes" / "expected-phases.json").write_text(json.dumps(keys))


def _make_kanban_header(tick_id: str, phase_key: str, todo_id: str) -> str:
    return json.dumps({
        "tick_id": tick_id,
        "phase_key": phase_key,
        "todo_id": todo_id,
    })


# ---------------------------------------------------------------------------
# gate_status (CP2)
# ---------------------------------------------------------------------------


class TestGateStatus:
    def test_blocked(self, mocker):
        """Gate task exists in kanban with blocked status."""
        mocker.patch(
            "hermes_pipeline.gate_state.get_todo_kanban_tasks",
            return_value={
                PLAN_GATE_PHASE_KEY: KanbanTaskInfo(
                    task_id="t1", phase_key=PLAN_GATE_PHASE_KEY,
                    status=BLOCKED, todo_id="TODO-5",
                )
            },
        )
        # No rejection sidecar
        mocker.patch(
            "hermes_pipeline.gate_state.read_rejection_sidecar", return_value=None,
        )
        assert gate_status(
            state_dir=Path("/tmp"), project_slug="proj", tick_id="T1",
        ) == GateStatus.BLOCKED

    def test_ready(self, mocker):
        mocker.patch(
            "hermes_pipeline.gate_state.get_todo_kanban_tasks",
            return_value={
                PLAN_GATE_PHASE_KEY: KanbanTaskInfo(
                    task_id="t1", phase_key=PLAN_GATE_PHASE_KEY,
                    status="ready", todo_id="TODO-5",
                )
            },
        )
        mocker.patch(
            "hermes_pipeline.gate_state.read_rejection_sidecar", return_value=None,
        )
        assert gate_status(
            state_dir=Path("/tmp"), project_slug="proj", tick_id="T1",
        ) == GateStatus.READY

    def test_running(self, mocker):
        mocker.patch(
            "hermes_pipeline.gate_state.get_todo_kanban_tasks",
            return_value={
                PLAN_GATE_PHASE_KEY: KanbanTaskInfo(
                    task_id="t1", phase_key=PLAN_GATE_PHASE_KEY,
                    status="running", todo_id="TODO-5",
                )
            },
        )
        mocker.patch(
            "hermes_pipeline.gate_state.read_rejection_sidecar", return_value=None,
        )
        assert gate_status(
            state_dir=Path("/tmp"), project_slug="proj", tick_id="T1",
        ) == GateStatus.RUNNING

    def test_failed_from_rejection_sidecar(self, mocker):
        """Rejection sidecar takes priority over kanban status."""
        mocker.patch(
            "hermes_pipeline.gate_state.read_rejection_sidecar",
            return_value={"tick_id": "T1", "rejection_count": 1, "reason": "bad"},
        )
        # Kanban still shows blocked (sidecar written but task not yet archived)
        mocker.patch(
            "hermes_pipeline.gate_state.get_todo_kanban_tasks",
            return_value={
                PLAN_GATE_PHASE_KEY: KanbanTaskInfo(
                    task_id="t1", phase_key=PLAN_GATE_PHASE_KEY,
                    status=BLOCKED, todo_id="TODO-5",
                )
            },
        )
        assert gate_status(
            state_dir=Path("/tmp"), project_slug="proj", tick_id="T1",
        ) == GateStatus.FAILED

    def test_unknown_when_no_kanban_task(self, mocker):
        mocker.patch(
            "hermes_pipeline.gate_state.get_todo_kanban_tasks",
            return_value={},  # No tasks found
        )
        mocker.patch(
            "hermes_pipeline.gate_state.read_rejection_sidecar", return_value=None,
        )
        assert gate_status(
            state_dir=Path("/tmp"), project_slug="proj", tick_id="T1",
        ) == GateStatus.UNKNOWN


# ---------------------------------------------------------------------------
# all_phases_complete rejection exception (CP1 - tick stall fix)
# ---------------------------------------------------------------------------


class TestAllPhasesCompleteRejection:
    def test_rejection_sidecar_prevents_stall(self, tmp_path, mocker):
        """When plan-gate is rejected (archived), all_phases_complete should
        return True if non-gate phases are done — the rejection sidecar
        signals the missing plan-gate phase is intentionally absent."""
        # Status map: non-gate phases are "done", plan-gate is missing (archived)
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={
                "phase_2_autoplan": "done",
                "phase_3_writing_plan": "done",
                # phase_2b_plan_gate is missing (archived)
            },
        )

        # Write expected-phases sentinel that includes plan-gate
        _write_expected_phases(tmp_path, [
            "phase_2_autoplan",
            "phase_2b_plan_gate",
            "phase_3_writing_plan",
        ])

        # Write rejection sidecar
        write_rejection_sidecar(
            state_dir=tmp_path, tick_id="T1", reason="bad plan",
            rejection_count=1,
        )

        assert all_phases_complete("proj", "T1", state_dir=tmp_path) is True

    def test_no_rejection_still_stalls_on_missing_gate(self, tmp_path, mocker):
        """Without a rejection sidecar, a missing plan-gate phase triggers
        the partial-registration stall (return False)."""
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={
                "phase_2_autoplan": "done",
                "phase_3_writing_plan": "done",
                # plan-gate missing, no rejection sidecar
            },
        )
        _write_expected_phases(tmp_path, [
            "phase_2_autoplan",
            "phase_2b_plan_gate",
            "phase_3_writing_plan",
        ])
        # No rejection sidecar

        assert all_phases_complete("proj", "T1", state_dir=tmp_path) is False

    def test_rejection_routed_through_gate_state(self, tmp_path, mocker):
        """all_phases_complete's plan-gate special case calls
        gate_state.gate_status rather than re-implementing the sidecar
        read inline."""
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={
                "phase_2_autoplan": "done",
                "phase_3_writing_plan": "done",
                # phase_2b_plan_gate is missing (archived)
            },
        )
        _write_expected_phases(tmp_path, [
            "phase_2_autoplan",
            "phase_2b_plan_gate",
            "phase_3_writing_plan",
        ])

        mocked_gate_status = mocker.patch(
            "hermes_pipeline.gate_state.gate_status",
            return_value=GateStatus.FAILED,
        )

        assert all_phases_complete("proj", "T1", state_dir=tmp_path) is True
        mocked_gate_status.assert_called_once_with(
            state_dir=tmp_path, project_slug="proj", tick_id="T1",
            gate_key="phase_2b_plan_gate",
        )


# ---------------------------------------------------------------------------
# _invoke_hermes gate phase handling
# ---------------------------------------------------------------------------


class TestInvokeHermesGate:
    def test_gate_approved_skips_hermes(self, mocker, tmp_path):
        """When a gate phase is approved (RUNNING), _invoke_hermes returns
        success without calling hermes subprocess."""
        mocker.patch(
            "hermes_pipeline.gate_state.gate_status",
            return_value=GateStatus.RUNNING,
        )
        run_hermes = mocker.patch("hermes_pipeline.phases._run_hermes_subprocess")

        result = __import__(
            "hermes_pipeline.phases", fromlist=["_invoke_hermes"]
        )._invoke_hermes(
            todo_id="TODO-5",
            phase_key="phase_2b_plan_gate",
            tick_id="T1",
            state_dir=tmp_path,
            project_slug="proj",
        )

        assert result["status"] == "success"
        run_hermes.assert_not_called()

    def test_gate_blocked_raises(self, mocker, tmp_path):
        """When a gate phase is blocked, _invoke_hermes raises RuntimeError
        (the tick holds)."""
        mocker.patch(
            "hermes_pipeline.gate_state.gate_status",
            return_value=GateStatus.BLOCKED,
        )

        with pytest.raises(RuntimeError, match="is blocked"):
            __import__(
                "hermes_pipeline.phases", fromlist=["_invoke_hermes"]
            )._invoke_hermes(
                todo_id="TODO-5",
                phase_key="phase_2b_plan_gate",
                tick_id="T1",
                state_dir=tmp_path,
                project_slug="proj",
            )

    def test_gate_rejected_raises(self, mocker, tmp_path):
        """When a gate phase is rejected (FAILED), _invoke_hermes raises
        RuntimeError so the runner records the failure."""
        mocker.patch(
            "hermes_pipeline.gate_state.gate_status",
            return_value=GateStatus.FAILED,
        )

        with pytest.raises(RuntimeError, match="is failed"):
            __import__(
                "hermes_pipeline.phases", fromlist=["_invoke_hermes"]
            )._invoke_hermes(
                todo_id="TODO-5",
                phase_key="phase_2b_plan_gate",
                tick_id="T1",
                state_dir=tmp_path,
                project_slug="proj",
            )
