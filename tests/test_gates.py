"""Tests for plan gate I/O helpers."""
import pytest
from pathlib import Path
import shutil

from hermes_pipeline.gates import (
    write_decision_sheet,
    read_decision_sheet,
    write_rejection_sidecar,
    read_rejection_sidecar,
    _sanitize_override,
)
from hermes_pipeline.decision.schema import (
    DecisionSheet,
    PlanGateError,
    _Option,
    DecisionQuestion,
)



class TestDecisionSheetIO:
    def test_write_and_read_roundtrip(self, tmp_path):
        state = tmp_path / ".hermes"
        sheet = DecisionSheet(
            schema_version="1.0",
            todo_id=5,
            tick_id="T1",
            questions=[
                DecisionQuestion(
                    question_id="q1",
                    classification="taste",
                    prompt="Q?",
                    options=[_Option("A", "A desc"), _Option("B", "B desc")],
                    recommendation="A",
                    rationale="r",
                )
            ],
        )
        write_decision_sheet(sheet, state_dir=state)
        loaded = read_decision_sheet(state_dir=state, tick_id="T1")
        assert loaded is not None
        assert loaded.todo_id == 5
        assert loaded.tick_id == "T1"
        assert len(loaded.questions) == 1

    def test_read_missing_returns_none(self, tmp_path):
        state = tmp_path / ".hermes"
        state.mkdir()
        result = read_decision_sheet(state_dir=state, tick_id="nonexistent")
        assert result is None

    def test_read_corrupt_returns_none(self, tmp_path):
        state = tmp_path / ".hermes"
        d = state / "decisions"
        d.mkdir(parents=True)
        (d / "nonexistent-plan.json").write_text("not json")
        result = read_decision_sheet(state_dir=state, tick_id="nonexistent")
        assert result is None

    def test_write_creates_decisions_directory(self, tmp_path):
        state = tmp_path / ".hermes"
        sheet = DecisionSheet(
            schema_version="1.0",
            todo_id=1,
            tick_id="T-new",
            questions=[
                DecisionQuestion(
                    question_id="q1",
                    classification="taste",
                    prompt="X",
                    options=[_Option("A", "a"), _Option("B", "b")],
                    recommendation="A",
                    rationale="r",
                )
            ],
        )
        path = write_decision_sheet(sheet, state_dir=state)
        assert path.exists()
        assert path.parent.name == "decisions"

    def test_write_is_atomic_no_tmp_leftover(self, tmp_path):
        """Atomic write: no .tmp files remain after write."""
        state = tmp_path / ".hermes"
        sheet = DecisionSheet(
            schema_version="1.0",
            todo_id=1,
            tick_id="T-atomic",
            questions=[
                DecisionQuestion(
                    question_id="q1",
                    classification="taste",
                    prompt="X",
                    options=[_Option("A", "a"), _Option("B", "b")],
                    recommendation="A",
                    rationale="r",
                )
            ],
        )
        write_decision_sheet(sheet, state_dir=state)
        tmp_files = list(state.rglob("*.tmp"))
        assert len(tmp_files) == 0


class TestRejectionSidecar:
    def test_write_and_read_roundtrip(self, tmp_path):
        state = tmp_path / ".hermes"
        state.mkdir()
        write_rejection_sidecar(
            state_dir=state, tick_id="T1", reason="bad scope", rejection_count=1
        )
        result = read_rejection_sidecar(state_dir=state, tick_id="T1")
        assert result is not None
        assert result["reason"] == "bad scope"
        assert result["rejection_count"] == 1

    def test_read_missing_returns_none(self, tmp_path):
        state = tmp_path / ".hermes"
        state.mkdir()
        result = read_rejection_sidecar(state_dir=state, tick_id="T1")
        assert result is None

    def test_read_corrupt_returns_none(self, tmp_path):
        state = tmp_path / ".hermes"
        d = state / "decisions"
        d.mkdir(parents=True)
        (d / "T1-rejected.json").write_text("{corrupt")
        result = read_rejection_sidecar(state_dir=state, tick_id="T1")
        assert result is None

    def test_preserves_all_fields(self, tmp_path):
        state = tmp_path / ".hermes"
        state.mkdir()
        write_rejection_sidecar(
            state_dir=state,
            tick_id="T-reject",
            reason="plan rejected",
            rejection_count=3,
        )
        result = read_rejection_sidecar(state_dir=state, tick_id="T-reject")
        assert result["tick_id"] == "T-reject"
        assert "timestamp" in result

    def test_uses_shutil_move_not_os_rename(self, tmp_path, mocker):
        """Write paths use shutil.move, not os.rename (cross-device safety)."""
        state = tmp_path / ".hermes"
        state.mkdir()
        mock_shutil_move = mocker.patch.object(shutil, "move", return_value=True)
        write_rejection_sidecar(
            state_dir=state, tick_id="T-shutil", reason="test", rejection_count=1
        )
        mock_shutil_move.assert_called_once()

    def test_sanitizes_control_characters_in_reason(self, tmp_path):
        """Control characters are stripped from the reason field."""
        state = tmp_path / ".hermes"
        state.mkdir()
        reason_with_controls = "bad\x00scope\x1f\x7f extra"
        write_rejection_sidecar(
            state_dir=state, tick_id="T-sanitize", reason=reason_with_controls, rejection_count=1
        )
        result = read_rejection_sidecar(state_dir=state, tick_id="T-sanitize")
        assert result is not None
        # Control chars \x00, \x1f, \x7f should be stripped by the regex
        assert "\x00" not in result["reason"]
        assert "\x1f" not in result["reason"]
        assert "\x7f" not in result["reason"]
        assert result["reason"] == "badscope extra"

    def test_caps_reason_length_at_500(self, tmp_path):
        """Reason longer than 500 chars is truncated."""
        state = tmp_path / ".hermes"
        state.mkdir()
        long_reason = "x" * 600
        write_rejection_sidecar(
            state_dir=state, tick_id="T-cap", reason=long_reason, rejection_count=1
        )
        result = read_rejection_sidecar(state_dir=state, tick_id="T-cap")
        assert result is not None
        assert len(result["reason"]) == 500



class TestSanitizeOverride:
    def test_passes_normal_text(self):
        assert _sanitize_override("Approach A is better") == "Approach A is better"

    def test_strips_control_chars(self):
        result = _sanitize_override("hello\x00world\x1f!")
        assert "\x00" not in result
        assert "\x1f" not in result

    def test_rejects_python_expressions(self):
        with pytest.raises(PlanGateError):
            _sanitize_override("{__class__}")

    def test_enforces_length_cap(self):
        long_val = "A" * 600
        result = _sanitize_override(long_val)
        assert len(result) <= 500

    def test_rejects_eval_pattern(self):
        with pytest.raises(PlanGateError):
            _sanitize_override("eval(import os)")

    def test_rejects_brace_patterns(self):
        with pytest.raises(PlanGateError):
            _sanitize_override("{0} {1}")


