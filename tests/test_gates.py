"""Tests for plan gate I/O helpers and stub decision sheet generator."""
import pytest
from pathlib import Path
import shutil

from hermes_pipeline.gates import (
    stub_generate_decision_sheet,
    write_decision_sheet,
    read_decision_sheet,
    write_rejection_sidecar,
    read_rejection_sidecar,
    is_high_risk,
    _sanitize_override,
    PLAN_GATE_PHASE_KEY,
)
from hermes_pipeline.decision.schema import (
    DecisionSheet,
    PlanGateError,
    _Option,
    DecisionQuestion,
)


class TestStubGenerator:
    def test_parses_decisions_section(self, tmp_path):
        plan = tmp_path / "todo-5-plan.md"
        plan.write_text(
            """# Plan for TODO-5

## Decisions

### Q1: Which database to use?
**Classification:** taste
**Options:** A) SQLite — simple, zero-config | B) Postgres — scalable
**Recommendation:** A
**Rationale:** A is simpler and sufficient for MVP

### Q2: Auth approach?
**Classification:** premise
**Options:** A) Session-based | B) JWT
**Recommendation:** B
**Rationale:** Stateless is better for distributed
"""
        )
        sheet = stub_generate_decision_sheet(
            plan_md_path=plan,
            todo_id=5,
            tick_id="TICK1",
            state_dir=tmp_path / ".hermes",
        )
        assert isinstance(sheet, DecisionSheet)
        assert len(sheet.questions) == 2
        assert sheet.questions[0].question_id == "q1"
        assert sheet.questions[0].recommendation == "A"

    def test_rejects_empty_decisions(self, tmp_path):
        plan = tmp_path / "todo-5-plan.md"
        plan.write_text("# Plan\n\nNo decisions section.\n")
        with pytest.raises(PlanGateError, match="no '## Decisions'"):
            stub_generate_decision_sheet(
                plan_md_path=plan,
                todo_id=5,
                tick_id="TICK1",
                state_dir=tmp_path / ".hermes",
            )

    def test_persisted_to_state_dir(self, tmp_path):
        """Decision sheet is written to .hermes/decisions/ on generation."""
        plan = tmp_path / "todo-5-plan.md"
        plan.write_text(
            """# Plan

## Decisions

### Q1: Store config where?
**Classification:** taste
**Options:** A) YAML file | B) TOML file
**Recommendation:** B
**Rationale:** TOML is unambiguous
"""
        )
        state = tmp_path / ".hermes"
        sheet = stub_generate_decision_sheet(
            plan_md_path=plan,
            todo_id=3,
            tick_id="TICK2",
            state_dir=state,
        )
        assert (state / "decisions" / "TICK2-plan.json").exists()

    def test_parses_question_fields(self, tmp_path):
        """Verify classification, prompt, options, rationale are parsed."""
        plan = tmp_path / "plan.md"
        plan.write_text(
            """## Decisions

### Q1: Title vs heading?
**Classification:** user-challenge
**Options:** A) Title case | B) Sentence case | C) Lowercase
**Recommendation:** B
**Rationale:** Readability
"""
        )
        sheet = stub_generate_decision_sheet(
            plan_md_path=plan,
            todo_id=1,
            tick_id="T3",
            state_dir=tmp_path / ".hermes",
        )
        q = sheet.questions[0]
        assert q.classification == "user-challenge"
        assert q.prompt == "Q1: Title vs heading?"
        assert len(q.options) == 3
        assert q.rationale == "Readability"
        assert q.answer is None

    def test_skips_blocks_missing_required_fields(self, tmp_path):
        """Blocks without classification, options, or recommendation are skipped."""
        plan = tmp_path / "plan.md"
        plan.write_text(
            """## Decisions

### Q1: Good block
**Classification:** taste
**Options:** A) Yes | B) No
**Recommendation:** A
**Rationale:** fine

### Q2: Missing classification
**Options:** A) X | B) Y
**Recommendation:** A

### Q3: Complete block
**Classification:** premise
**Options:** A) One | B) Two
**Recommendation:** B
**Rationale:** two is better
"""
        )
        sheet = stub_generate_decision_sheet(
            plan_md_path=plan,
            todo_id=1,
            tick_id="T4",
            state_dir=tmp_path / ".hermes",
        )
        assert len(sheet.questions) == 2
        assert sheet.questions[0].question_id == "q1"
        assert sheet.questions[1].question_id == "q2"


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


class TestRiskClassifier:
    def test_dependency_change_is_high_risk(self, tmp_path):
        todos = tmp_path / "TODOS.md"
        todos.write_text("- [ ] TODO-5: add new dependency requests for HTTP client\n")
        assert is_high_risk(todo_id="TODO-5", todos_md=todos.read_text(), state_dir=tmp_path / ".hermes") is True

    def test_blast_radius_keywords(self, tmp_path):
        todos = tmp_path / "TODOS.md"
        todos.write_text("- [ ] TODO-6: refactor authentication module\n")
        assert is_high_risk(todo_id="TODO-6", todos_md=todos.read_text(), state_dir=tmp_path / ".hermes") is True

    def test_rejection_history_is_high_risk(self, tmp_path):
        state = tmp_path / ".hermes"
        state.mkdir()
        write_rejection_sidecar(state_dir=state, tick_id="prior-tick", reason="bad", rejection_count=1)
        todos = tmp_path / "TODOS.md"
        todos.write_text("- [ ] TODO-7: add a simple utility function\n")
        assert is_high_risk(todo_id="TODO-7", todos_md=todos.read_text(), state_dir=state) is False

    def test_simple_task_is_low_risk(self, tmp_path):
        todos = tmp_path / "TODOS.md"
        todos.write_text("- [ ] TODO-8: update README formatting\n")
        assert is_high_risk(todo_id="TODO-8", todos_md=todos.read_text(), state_dir=tmp_path / ".hermes") is False

    def test_security_keyword_is_high_risk(self, tmp_path):
        todos = tmp_path / "TODOS.md"
        todos.write_text("- [ ] TODO-9: implement security audit logging\n")
        assert is_high_risk(todo_id="TODO-9", todos_md=todos.read_text(), state_dir=tmp_path / ".hermes") is True

    def test_missing_todo_id_is_high_risk(self, tmp_path):
        """Cannot find TODO in text — gate conservatively."""
        todos = tmp_path / "TODOS.md"
        todos.write_text("- [ ] TODO-1: something else\n")
        assert is_high_risk(todo_id="TODO-99", todos_md=todos.read_text(), state_dir=tmp_path / ".hermes") is True

    def test_plan_gate_phase_key(self):
        assert PLAN_GATE_PHASE_KEY == "phase_2b_plan_gate"


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
