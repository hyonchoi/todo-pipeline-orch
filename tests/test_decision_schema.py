from __future__ import annotations
import json
import pytest

from hermes_pipeline.decision import HermesSelectionDecision, SelectionContext
from hermes_pipeline.decision.schema import (
    DecisionQuestion,
    DecisionSheet,
    PlanGateError,
    _Option,
    validate_decision_sheet,
)


# ---------------------------------------------------------------------------
# Legacy tests (HermesSelectionDecision, SelectionContext)
# ---------------------------------------------------------------------------

def test_decision_roundtrips_json():
    d = HermesSelectionDecision(
        tick_id="01JABCDXYZ",
        timestamp="2026-06-13T12:00:00Z",
        model="claude-opus-4-7",
        prompt_sha="deadbeef",
        candidates_considered=["TODO-1", "TODO-2"],
        picked="TODO-2",
        rationale="TODO-2 unblocks the merge gate work.",
        blocked_reasons={"TODO-3": "depends on TODO-2"},
        in_flight=[],
    )
    parsed = HermesSelectionDecision.from_json(d.to_json())
    assert parsed == d


def test_decision_picked_none_is_valid():
    d = HermesSelectionDecision(
        tick_id="t",
        timestamp="2026-06-13T12:00:00Z",
        model="claude-opus-4-7",
        prompt_sha="x",
        candidates_considered=[],
        picked=None,
        rationale="no eligible TODOs",
        blocked_reasons={},
        in_flight=[],
    )
    assert json.loads(d.to_json())["picked"] is None


def test_selection_context_construct():
    ctx = SelectionContext(
        todos_md="- TODO-1: do thing",
        in_flight=[],
        recent_decisions=[],
        kanban_snapshot={"columns": []},
        project_slug="demo",
    )
    assert ctx.project_slug == "demo"


# ---------------------------------------------------------------------------
# DecisionQuestion tests
# ---------------------------------------------------------------------------

class TestDecisionQuestion:
    def test_valid_question(self):
        q = DecisionQuestion(
            question_id="q1",
            classification="taste",
            prompt="Which approach?",
            options=[
                _Option(label="A", description="Approach A"),
                _Option(label="B", description="Approach B"),
            ],
            recommendation="A",
            rationale="A is simpler",
            answer=None,
        )
        assert q.question_id == "q1"
        assert q.answer is None

    def test_rejection_bad_classification(self):
        with pytest.raises(PlanGateError, match="classification"):
            DecisionQuestion(
                question_id="q1",
                classification="invalid_type",  # type: ignore[arg-type]
                prompt="Q?",
                options=[
                    _Option(label="A", description="A"),
                    _Option(label="B", description="B"),
                ],
                recommendation="A",
                rationale="r",
                answer=None,
            )

    def test_rejection_single_option(self):
        with pytest.raises(PlanGateError, match="options"):
            DecisionQuestion(
                question_id="q1",
                classification="taste",
                prompt="Q?",
                options=[_Option(label="A", description="Only option")],
                recommendation="A",
                rationale="r",
                answer=None,
            )

    def test_rejection_recommendation_not_in_options(self):
        with pytest.raises(PlanGateError, match="recommendation"):
            DecisionQuestion(
                question_id="q1",
                classification="taste",
                prompt="Q?",
                options=[
                    _Option(label="A", description="A"),
                    _Option(label="B", description="B"),
                ],
                recommendation="C",
                rationale="r",
                answer=None,
            )

    def test_rejection_answer_not_in_options(self):
        with pytest.raises(PlanGateError, match="answer"):
            DecisionQuestion(
                question_id="q1",
                classification="taste",
                prompt="Q?",
                options=[
                    _Option(label="A", description="A"),
                    _Option(label="B", description="B"),
                ],
                recommendation="A",
                rationale="r",
                answer="Z",
            )

    def test_valid_with_answer(self):
        q = DecisionQuestion(
            question_id="q2",
            classification="premise",
            prompt="Which DB?",
            options=[
                _Option(label="SQLite", description="Local"),
                _Option(label="PostgreSQL", description="Remote"),
            ],
            recommendation="SQLite",
            rationale="Simpler for MVP",
            answer="PostgreSQL",
        )
        assert q.answer == "PostgreSQL"


# ---------------------------------------------------------------------------
# DecisionSheet tests
# ---------------------------------------------------------------------------

class TestDecisionSheet:
    def test_valid_sheet(self):
        sheet = DecisionSheet(
            schema_version="1.0",
            todo_id=5,
            tick_id="01HQXXXX",
            questions=[
                DecisionQuestion(
                    question_id="q1",
                    classification="taste",
                    prompt="Which approach?",
                    options=[
                        _Option(label="A", description="A"),
                        _Option(label="B", description="B"),
                    ],
                    recommendation="A",
                    rationale="simpler",
                )
            ],
        )
        assert sheet.todo_id == 5

    def test_rejection_empty_questions(self):
        with pytest.raises(PlanGateError, match="questions"):
            DecisionSheet(
                schema_version="1.0",
                todo_id=5,
                tick_id="abc",
                questions=[],
            )

    def test_rejection_bad_schema_version(self):
        with pytest.raises(PlanGateError, match="schema_version"):
            DecisionSheet(
                schema_version="2.0",
                todo_id=5,
                tick_id="abc",
                questions=[
                    DecisionQuestion(
                        question_id="q1",
                        classification="taste",
                        prompt="Q?",
                        options=[
                            _Option("A", "A"),
                            _Option("B", "B"),
                        ],
                        recommendation="A",
                        rationale="r",
                    )
                ],
            )

    def test_rejection_negative_todo_id(self):
        with pytest.raises(PlanGateError, match="todo_id"):
            DecisionSheet(
                schema_version="1.0",
                todo_id=-1,
                tick_id="abc",
                questions=[
                    DecisionQuestion(
                        question_id="q1",
                        classification="taste",
                        prompt="Q?",
                        options=[
                            _Option("A", "A"),
                            _Option("B", "B"),
                        ],
                        recommendation="A",
                        rationale="r",
                    )
                ],
            )

    def test_rejection_duplicate_question_ids(self):
        q = DecisionQuestion(
            question_id="q1",
            classification="taste",
            prompt="Q?",
            options=[_Option("A", "A"), _Option("B", "B")],
            recommendation="A",
            rationale="r",
        )
        with pytest.raises(PlanGateError, match="duplicate"):
            DecisionSheet(
                schema_version="1.0",
                todo_id=5,
                tick_id="abc",
                questions=[q, q],
            )

    def test_to_json_and_from_json_roundtrip(self):
        sheet = DecisionSheet(
            schema_version="1.0",
            todo_id=5,
            tick_id="abc",
            questions=[
                DecisionQuestion(
                    question_id="q1",
                    classification="taste",
                    prompt="Q?",
                    options=[
                        _Option("A", "A"),
                        _Option("B", "B"),
                    ],
                    recommendation="A",
                    rationale="r",
                )
            ],
        )
        j = sheet.to_json()
        loaded = DecisionSheet.from_json(j)
        assert loaded.todo_id == sheet.todo_id
        assert loaded.questions[0].question_id == "q1"

    def test_validate_decision_sheet_from_dict(self):
        data = {
            "schema_version": "1.0",
            "todo_id": 5,
            "tick_id": "abc",
            "questions": [
                {
                    "question_id": "q1",
                    "classification": "taste",
                    "prompt": "Q?",
                    "options": [
                        {"label": "A", "description": "A"},
                        {"label": "B", "description": "B"},
                    ],
                    "recommendation": "A",
                    "rationale": "r",
                    "answer": None,
                }
            ],
        }
        sheet = validate_decision_sheet(data)
        assert isinstance(sheet, DecisionSheet)
        assert sheet.questions[0].answer is None

    def test_validate_decision_sheet_missing_answer_defaults_to_none(self):
        data = {
            "schema_version": "1.0",
            "todo_id": 1,
            "tick_id": "t1",
            "questions": [
                {
                    "question_id": "q1",
                    "classification": "mechanical",
                    "prompt": "Sync or async?",
                    "options": [
                        {"label": "sync", "description": "Sync"},
                        {"label": "async", "description": "Async"},
                    ],
                    "recommendation": "sync",
                    "rationale": "simpler",
                    # no "answer" key
                }
            ],
        }
        sheet = validate_decision_sheet(data)
        assert sheet.questions[0].answer is None

    def test_sheet_is_frozen(self):
        """Dataclasses should be immutable after construction."""
        sheet = DecisionSheet(
            schema_version="1.0",
            todo_id=1,
            tick_id="t1",
            questions=[
                DecisionQuestion(
                    question_id="q1",
                    classification="taste",
                    prompt="Q?",
                    options=[_Option("A", "A"), _Option("B", "B")],
                    recommendation="A",
                    rationale="r",
                )
            ],
        )
        with pytest.raises(AttributeError):
            sheet.todo_id = 99  # type: ignore

    def test_rejection_non_integer_todo_id(self):
        """todo_id that is a string should raise PlanGateError, not TypeError."""
        with pytest.raises(PlanGateError, match="todo_id must be int"):
            DecisionSheet(
                schema_version="1.0",
                todo_id="5",  # type: ignore[arg-type]
                tick_id="abc",
                questions=[
                    DecisionQuestion(
                        question_id="q1",
                        classification="taste",
                        prompt="Q?",
                        options=[_Option("A", "A"), _Option("B", "B")],
                        recommendation="A",
                        rationale="r",
                    )
                ],
            )

    def test_rejection_zero_todo_id(self):
        with pytest.raises(PlanGateError, match="todo_id"):
            DecisionSheet(
                schema_version="1.0",
                todo_id=0,
                tick_id="abc",
                questions=[
                    DecisionQuestion(
                        question_id="q1",
                        classification="taste",
                        prompt="Q?",
                        options=[_Option("A", "A"), _Option("B", "B")],
                        recommendation="A",
                        rationale="r",
                    )
                ],
            )


# ---------------------------------------------------------------------------
# validate_decision_sheet missing-key tests
# ---------------------------------------------------------------------------

class TestValidateDecisionSheetMissingKeys:
    def test_missing_schema_version_raises_plangateerror(self):
        data = {
            "todo_id": 5,
            "tick_id": "abc",
            "questions": [],
        }
        with pytest.raises(PlanGateError, match="schema_version"):
            validate_decision_sheet(data)

    def test_missing_todo_id_raises_plangateerror(self):
        data = {
            "schema_version": "1.0",
            "tick_id": "abc",
            "questions": [],
        }
        with pytest.raises(PlanGateError, match="todo_id"):
            validate_decision_sheet(data)

    def test_missing_tick_id_raises_plangateerror(self):
        data = {
            "schema_version": "1.0",
            "todo_id": 5,
            "questions": [],
        }
        with pytest.raises(PlanGateError, match="tick_id"):
            validate_decision_sheet(data)

    def test_missing_question_question_id_raises_plangateerror(self):
        data = {
            "schema_version": "1.0",
            "todo_id": 1,
            "tick_id": "t1",
            "questions": [
                {
                    "classification": "taste",
                    "prompt": "Q?",
                    "options": [
                        {"label": "A", "description": "A"},
                        {"label": "B", "description": "B"},
                    ],
                    "recommendation": "A",
                    "rationale": "r",
                }
            ],
        }
        with pytest.raises(PlanGateError, match="question_id"):
            validate_decision_sheet(data)

    def test_missing_option_label_raises_plangateerror(self):
        data = {
            "schema_version": "1.0",
            "todo_id": 1,
            "tick_id": "t1",
            "questions": [
                {
                    "question_id": "q1",
                    "classification": "taste",
                    "prompt": "Q?",
                    "options": [
                        {"description": "A"},  # missing "label"
                        {"label": "B", "description": "B"},
                    ],
                    "recommendation": "A",
                    "rationale": "r",
                }
            ],
        }
        with pytest.raises(PlanGateError, match="label"):
            validate_decision_sheet(data)

    def test_missing_question_classification_raises_plangateerror(self):
        data = {
            "schema_version": "1.0",
            "todo_id": 1,
            "tick_id": "t1",
            "questions": [
                {
                    "question_id": "q1",
                    "prompt": "Q?",
                    "options": [
                        {"label": "A", "description": "A"},
                        {"label": "B", "description": "B"},
                    ],
                    "recommendation": "A",
                    "rationale": "r",
                }
            ],
        }
        with pytest.raises(PlanGateError, match="classification"):
            validate_decision_sheet(data)


# ---------------------------------------------------------------------------
# PlanGateError tests
# ---------------------------------------------------------------------------

class TestPlanGateError:
    def test_is_exception(self):
        err = PlanGateError("something went wrong")
        assert isinstance(err, Exception)
        assert str(err) == "something went wrong"
