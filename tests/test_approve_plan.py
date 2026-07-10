"""Tests for approve-plan CLI subcommand and domain logic (Task 7)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_pipeline import approve_plan as ap
from hermes_pipeline.decision.schema import (
    DecisionQuestion,
    DecisionSheet,
    _Option,
)
from hermes_pipeline.gates import (
    PLAN_GATE_PHASE_KEY,
    read_decision_sheet,
    read_rejection_sidecar,
    write_decision_sheet,
)
from hermes_pipeline.kanban_tasks import KanbanTaskInfo
from hermes_pipeline.outcomes import CURRENT_TICK_ID_FILE
from hermes_pipeline.ship import ApproveRefused, approve_lock


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_sheet(*, todo_id: int = 5, tick_id: str = "TICK1") -> DecisionSheet:
    return DecisionSheet(
        schema_version="1.0",
        todo_id=todo_id,
        tick_id=tick_id,
        questions=[
            DecisionQuestion(
                question_id="q1",
                classification="taste",
                prompt="Which approach?",
                options=[_Option("A", "approach A"), _Option("B", "approach B")],
                recommendation="A",
                rationale="A is simpler",
            ),
            DecisionQuestion(
                question_id="q2",
                classification="mechanical",
                prompt="Which lib?",
                options=[_Option("X", "lib X"), _Option("Y", "lib Y")],
                recommendation="Y",
                rationale="Y is maintained",
            ),
        ],
    )


def _gate(status: str = "blocked", task_id: str = "t_gate") -> KanbanTaskInfo:
    return KanbanTaskInfo(
        task_id=task_id,
        phase_key=PLAN_GATE_PHASE_KEY,
        status=status,
        todo_id="TODO-5",
    )


def _mock_gate(mocker, gate: KanbanTaskInfo | None):
    tasks = {} if gate is None else {PLAN_GATE_PHASE_KEY: gate}
    return mocker.patch(
        "hermes_pipeline.approve_plan.get_todo_kanban_tasks",
        return_value=tasks,
    )


# ---------------------------------------------------------------------------
# Override parsing
# ---------------------------------------------------------------------------


class TestParseOverrides:
    def test_none_returns_empty(self):
        assert ap._parse_overrides(None) == {}

    def test_valid(self):
        assert ap._parse_overrides(["q1=A", "q2=B"]) == {"q1": "A", "q2": "B"}

    def test_strips_whitespace(self):
        assert ap._parse_overrides([" q1 = B "]) == {"q1": "B"}

    def test_missing_equals_raises(self):
        with pytest.raises(ApproveRefused, match="expected q_id=LABEL"):
            ap._parse_overrides(["q1"])

    def test_duplicate_raises(self):
        with pytest.raises(ApproveRefused, match="duplicate override"):
            ap._parse_overrides(["q1=A", "q1=B"])


class TestValidateOverrides:
    def test_empty_noop(self):
        ap._validate_overrides(sheet=_make_sheet(), overrides={})

    def test_valid(self):
        ap._validate_overrides(sheet=_make_sheet(), overrides={"q1": "B"})

    def test_unknown_qid_raises(self):
        with pytest.raises(ApproveRefused, match="unknown question 'q9'"):
            ap._validate_overrides(sheet=_make_sheet(), overrides={"q9": "A"})

    def test_bad_label_raises(self):
        with pytest.raises(ApproveRefused, match="must be one of"):
            ap._validate_overrides(sheet=_make_sheet(), overrides={"q1": "Z"})

    def test_injection_value_rejected(self, mocker):
        """Sanitization is wired: even if a label somehow passed the option
        gate, _sanitize_override would reject format-brace patterns."""
        # Label validation rejects `{__class__}` before sanitization, but
        # patch _sanitize_override to confirm it is called for valid labels.
        with mocker.patch.object(
            ap, "_sanitize_override", side_effect=ap.PlanGateError("injection")
        ):
            with pytest.raises(ApproveRefused, match="injection"):
                ap._validate_overrides(sheet=_make_sheet(), overrides={"q1": "A"})


# ---------------------------------------------------------------------------
# Tick resolution (DX-C2 / CP16)
# ---------------------------------------------------------------------------


class TestResolveTick:
    def test_from_current_tick_file(self, tmp_path):
        write_decision_sheet(_make_sheet(tick_id="TICKA"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("TICKA\n")
        tick = ap._resolve_tick_for_todo(
            state_dir=tmp_path, todo_id=5, project_slug="proj",
        )
        assert tick == "TICKA"

    def test_by_scanning_decisions(self, tmp_path):
        # current tick points elsewhere; scan finds the TODO-5 sheet
        write_decision_sheet(_make_sheet(todo_id=5, tick_id="TICKB"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("SOME_OTHER_TICK\n")
        tick = ap._resolve_tick_for_todo(
            state_dir=tmp_path, todo_id=5, project_slug="proj",
        )
        assert tick == "TICKB"

    def test_not_found_raises(self, tmp_path, mocker):
        mocker.patch(
            "hermes_pipeline.approve_plan._find_blocked_plan_gate_tick",
            return_value=None,
        )
        with pytest.raises(ApproveRefused, match="no plan-gate decision sheet"):
            ap._resolve_tick_for_todo(
                state_dir=tmp_path, todo_id=99, project_slug="proj",
            )


# ---------------------------------------------------------------------------
# Approve path (CP1)
# ---------------------------------------------------------------------------


class TestApprove:
    def test_fills_recommendations_and_completes_gate(self, tmp_path, mocker):
        write_decision_sheet(_make_sheet(tick_id="T1"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("T1")
        _mock_gate(mocker, _gate())
        complete = mocker.patch("hermes_pipeline.approve_plan.complete_gate_task")

        summary = ap.approve_plan(
            project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
        )

        assert "Approved plan for TODO-5" in summary
        complete.assert_called_once_with("t_gate")
        # Answers filled with recommendations
        sheet = read_decision_sheet(state_dir=tmp_path, tick_id="T1")
        answers = {q.question_id: q.answer for q in sheet.questions}
        assert answers == {"q1": "A", "q2": "Y"}

    def test_override_uses_override_value(self, tmp_path, mocker):
        write_decision_sheet(_make_sheet(tick_id="T1"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("T1")
        _mock_gate(mocker, _gate())
        mocker.patch("hermes_pipeline.approve_plan.complete_gate_task")

        ap.approve_plan(
            project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
            overrides={"q1": "B"},
        )
        sheet = read_decision_sheet(state_dir=tmp_path, tick_id="T1")
        answers = {q.question_id: q.answer for q in sheet.questions}
        assert answers == {"q1": "B", "q2": "Y"}  # q1 overridden, q2 recommended

    def test_missing_sheet_raises(self, tmp_path, mocker):
        # tick resolves via kanban but no sheet on disk
        mocker.patch(
            "hermes_pipeline.approve_plan._resolve_tick_for_todo",
            return_value="GHOST",
        )
        with pytest.raises(ApproveRefused, match="no decision sheet found"):
            ap.approve_plan(
                project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
            )

    def test_gate_not_blocked_raises(self, tmp_path, mocker):
        write_decision_sheet(_make_sheet(tick_id="T1"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("T1")
        _mock_gate(mocker, _gate(status="done"))
        with pytest.raises(ApproveRefused, match="not 'blocked'"):
            ap.approve_plan(
                project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
            )

    def test_gate_missing_raises(self, tmp_path, mocker):
        write_decision_sheet(_make_sheet(tick_id="T1"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("T1")
        _mock_gate(mocker, None)
        with pytest.raises(ApproveRefused, match="no plan-gate task found"):
            ap.approve_plan(
                project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# Reject path (CP9)
# ---------------------------------------------------------------------------


class TestReject:
    def test_writes_sidecar_and_archives(self, tmp_path, mocker):
        write_decision_sheet(_make_sheet(tick_id="T1"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("T1")
        _mock_gate(mocker, _gate())
        run = mocker.patch(
            "hermes_pipeline.approve_plan.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        )

        summary = ap.approve_plan(
            project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
            reject_reason="dependency risk not addressed",
        )

        assert "Rejected plan for TODO-5" in summary
        sidecar = read_rejection_sidecar(state_dir=tmp_path, tick_id="T1")
        assert sidecar["reason"] == "dependency risk not addressed"
        assert sidecar["rejection_count"] == 1
        # archive called with the gate task id
        archive_cmd = run.call_args[0][0]
        assert archive_cmd[:3] == ["hermes", "kanban", "archive"]
        assert archive_cmd[3] == "t_gate"

    def test_increments_rejection_count(self, tmp_path, mocker):
        write_decision_sheet(_make_sheet(tick_id="T1"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("T1")
        _mock_gate(mocker, _gate())
        mocker.patch(
            "hermes_pipeline.approve_plan.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        )
        # First reject
        ap.approve_plan(
            project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
            reject_reason="first",
        )
        # Gate is still blocked (mocked), second reject bumps count
        summary = ap.approve_plan(
            project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
            reject_reason="second",
        )
        assert "rejection #2" in summary
        sidecar = read_rejection_sidecar(state_dir=tmp_path, tick_id="T1")
        assert sidecar["rejection_count"] == 2

    def test_archive_failure_raises(self, tmp_path, mocker):
        write_decision_sheet(_make_sheet(tick_id="T1"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("T1")
        _mock_gate(mocker, _gate())
        mocker.patch(
            "hermes_pipeline.approve_plan.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", "boom"),
        )
        with pytest.raises(ApproveRefused, match="failed to archive"):
            ap.approve_plan(
                project_dir=tmp_path, project_slug="proj", todo_id=5, state_dir=tmp_path,
                reject_reason="bad",
            )


# ---------------------------------------------------------------------------
# Lock contention (CP5)
# ---------------------------------------------------------------------------


class TestLockContention:
    def test_second_caller_refused(self, tmp_path, mocker):
        write_decision_sheet(_make_sheet(tick_id="T1"), state_dir=tmp_path)
        (tmp_path / CURRENT_TICK_ID_FILE).write_text("T1")
        _mock_gate(mocker, _gate())
        mocker.patch("hermes_pipeline.approve_plan.complete_gate_task")

        with approve_lock(tmp_path):
            with pytest.raises(ApproveRefused, match="another approve is already"):
                ap.approve_plan(
                    project_dir=tmp_path, project_slug="proj", todo_id=5,
                    state_dir=tmp_path,
                )
