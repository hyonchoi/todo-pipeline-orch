"""Tests for plan gate I/O helpers and stub decision sheet generator."""
import pytest
from pathlib import Path

from hermes_pipeline.gates import (
    stub_generate_decision_sheet,
    write_decision_sheet,
    read_decision_sheet,
    write_rejection_sidecar,
    read_rejection_sidecar,
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
