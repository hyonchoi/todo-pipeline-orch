from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_pipeline.config import (
    CircuitBreakerConfig,
    Config,
    FullConfig,
    SelectionConfig,
)
from hermes_pipeline.decision import (
    HermesSelectionDecision,
    SelectionContext,
    run_selection,
)
from hermes_pipeline.decision.agent import AgentResult, PromptShaMismatch
from hermes_pipeline.hermes_adapter import (
    ClaudeCallError,
    ClaudeDependencyError,
    HermesCallError,
    HermesDependencyError,
)


def _synthetic_error(error_type, message="SECRET", returncode=None):
    error = error_type.__new__(error_type)
    Exception.__init__(error, message)
    if returncode is not None:
        error.returncode = returncode
    return error


def _cfg(state_dir: Path, prompt_path: Path, expected_sha=None) -> FullConfig:
    return FullConfig(
        base=Config(state_dir=state_dir),
        selection=SelectionConfig(
            model="m", max_tokens=100, auto_execute=False,
            prompt_path=str(prompt_path), expected_prompt_sha=expected_sha,
        ),
        circuit_breaker=CircuitBreakerConfig(),
    )

def _default_cfg(state_dir: Path) -> FullConfig:
    return FullConfig(
        base=Config(state_dir=state_dir),
        selection=SelectionConfig(model="m", max_tokens=100, auto_execute=False),
        circuit_breaker=CircuitBreakerConfig(),
    )

def _prompt(tmp_path: Path) -> Path:
    p = tmp_path / "p.md"
    p.write_text("PROMPT")
    return p

def _ctx(
    todos_md: str = "- [ ] **TODO-1: One**\n- [ ] **TODO-2: Two**",
    in_flight: list[str] | None = None,
) -> SelectionContext:
    return SelectionContext(todos_md, in_flight or [], [], {}, "demo")

def test_happy_path_persists_decision(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    p = _prompt(tmp_path)
    fake = AgentResult(
        parsed={
            "candidates_considered": ["TODO-1"],
            "picked": "TODO-1",
            "rationale": "ok",
            "blocked_reasons": {},
            "in_flight": [],
        },
        prompt_sha="sha",
    )
    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        d = run_selection(tick_id="01JA", ctx=_ctx(), cfg=_cfg(state, p))
    assert isinstance(d, HermesSelectionDecision)
    assert d.picked == "TODO-1"
    assert (state / "decisions" / "01JA.json").exists()

def test_default_prompt_is_bundled_package_data(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    fake = AgentResult(
        parsed={
            "candidates_considered": ["TODO-1"],
            "picked": "TODO-1",
            "rationale": "ok",
            "blocked_reasons": {},
            "in_flight": [],
        },
        prompt_sha="sha",
    )
    with patch("hermes_pipeline.decision.call_agent", return_value=fake) as call:
        d = run_selection(tick_id="01JDEFAULT", ctx=_ctx(), cfg=_default_cfg(state))

    assert d.picked == "TODO-1"
    prompt_path = call.call_args.kwargs["prompt_path"]
    assert prompt_path.name == "selection.md"
    assert prompt_path.parts[-3:] == ("data", "prompts", "selection.md")

def test_picked_not_in_todos_md_is_rejected(tmp_path):
    """LLM-output trust boundary: picked must appear in TODOS.md (NOT in the
    model's self-reported candidates_considered, which is also LLM output)."""
    state = tmp_path / "state"; state.mkdir()
    p = _prompt(tmp_path)
    fake = AgentResult(
        parsed={
            # Model agrees with itself — but TODO-999 is not in TODOS.md.
            "candidates_considered": ["TODO-999"],
            "picked": "TODO-999",
            "rationale": "I like this one",
            "blocked_reasons": {},
            "in_flight": [],
        },
        prompt_sha="sha",
    )
    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        d = run_selection(tick_id="01JC", ctx=_ctx(), cfg=_cfg(state, p))
    assert d.picked is None
    assert "pick_not_in_todos_md" in d.rationale


def test_picked_id_mentioned_only_outside_entry_header_is_rejected(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    p = _prompt(tmp_path)
    fake = AgentResult(
        parsed={
            "candidates_considered": ["TODO-999"],
            "picked": "TODO-999",
            "rationale": "mentioned in the entry",
            "blocked_reasons": {},
            "in_flight": [],
        },
        prompt_sha="sha",
    )
    todos_md = """\
- [ ] **TODO-1: Real entry**
  - **What:** Mentions TODO-999 in its body.
  - **Reference:** docs/TODO-999-notes.md
  - **Plan:** docs/TODO-999-plan.md
"""

    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        d = run_selection(
            tick_id="01JHEADER",
            ctx=_ctx(todos_md=todos_md),
            cfg=_cfg(state, p),
        )

    assert d.picked is None
    assert d.rationale.startswith("pick_not_in_todos_md")

def test_picked_already_in_flight_is_rejected(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    p = _prompt(tmp_path)
    fake = AgentResult(
        parsed={
            "candidates_considered": ["TODO-1"],
            "picked": "TODO-1",
            "rationale": "go",
            "blocked_reasons": {},
            "in_flight": ["TODO-1"],
        },
        prompt_sha="sha",
    )
    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        d = run_selection(
            tick_id="01JE",
            ctx=_ctx(in_flight=["TODO-1"]),
            cfg=_cfg(state, p),
        )
    assert d.picked is None
    assert "pick_already_in_flight" in d.rationale


def test_picked_must_belong_to_exact_compiled_eligible_set(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    p = _prompt(tmp_path)
    fake = AgentResult(
        parsed={
            "candidates_considered": ["TODO-1", "TODO-2"],
            "picked": "TODO-2",
            "rationale": "prefer two",
            "blocked_reasons": {},
            "in_flight": [],
        },
        prompt_sha="sha",
    )
    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        decision = run_selection(
            tick_id="01JELIGIBLE",
            ctx=_ctx(),
            cfg=_cfg(state, p),
            eligible_todo_ids=frozenset({"TODO-1"}),
        )

    assert decision.picked is None
    assert "pick_not_eligible" in decision.rationale


def test_compiled_identity_overwrites_model_candidate_claims_and_filters_blocked_ids(
    tmp_path,
):
    state = tmp_path / "state"
    state.mkdir()
    p = _prompt(tmp_path)
    fake = AgentResult(
        parsed={
            "candidates_considered": ["TODO-999", "TODO-2"],
            "picked": "TODO-1",
            "rationale": "one is ready",
            "blocked_reasons": {
                "TODO-1": "not actually blocked",
                "TODO-2": "excluded by compiler",
                "TODO-999": "hallucinated",
            },
            "in_flight": [],
        },
        prompt_sha="sha",
    )
    context = _ctx(
        "- [ ] **TODO-1: One**\n- [→] **TODO-3: Three**\n- [ ] **TODO-2: Two**"
    )

    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        decision = run_selection(
            tick_id="01JIDENTITY",
            ctx=context,
            cfg=_cfg(state, p),
            eligible_todo_ids=frozenset({"TODO-1", "TODO-3"}),
        )

    assert decision.candidates_considered == ["TODO-1", "TODO-3"]
    assert decision.blocked_reasons == {"TODO-1": "not actually blocked"}
    persisted = (state / "decisions" / "01JIDENTITY.json").read_text()
    assert "TODO-999" not in persisted
    assert "TODO-2" not in persisted

def test_api_error_persists_decision_with_picked_none(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    p = _prompt(tmp_path)
    class FakeAPIError(Exception):
        pass
    with patch(
        "hermes_pipeline.decision.call_agent",
        side_effect=FakeAPIError("SECRET_EXCEPTION_CANARY"),
    ):
        d = run_selection(tick_id="01JF", ctx=_ctx(), cfg=_cfg(state, p))
    assert d.picked is None
    assert d.rationale == "api_error: unexpected_error"
    assert "SECRET_EXCEPTION_CANARY" not in d.rationale
    assert "SECRET_EXCEPTION_CANARY" not in (
        state / "decisions" / "01JF.json"
    ).read_text()
    assert (state / "decisions" / "01JF.json").exists()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_synthetic_error(HermesCallError, returncode=1), "api_error: hermes_error"),
        (_synthetic_error(ClaudeCallError, returncode=2), "api_error: claude_error"),
        (HermesDependencyError("SECRET"), "api_error: dependency_error"),
        (ClaudeDependencyError("SECRET"), "api_error: dependency_error"),
        (subprocess.TimeoutExpired("SECRET", 1), "api_error: timeout"),
        (ConnectionError("SECRET"), "api_error: transport_error"),
    ],
)
def test_api_errors_use_stable_sanitized_codes(tmp_path, error, expected):
    state = tmp_path / "state"; state.mkdir()
    p = _prompt(tmp_path)
    with patch("hermes_pipeline.decision.call_agent", side_effect=error):
        decision = run_selection(tick_id="01JCODE", ctx=_ctx(), cfg=_cfg(state, p))

    assert decision.rationale == expected
    assert "SECRET" not in (state / "decisions" / "01JCODE.json").read_text()

def test_missing_api_key_persists_config_error(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    p = _prompt(tmp_path)
    with patch(
        "hermes_pipeline.decision.call_agent",
        side_effect=KeyError("ANTHROPIC_API_KEY"),
    ):
        d = run_selection(tick_id="01JG", ctx=_ctx(), cfg=_cfg(state, p))
    assert d.picked is None
    assert d.rationale == "config_error: missing_setting"
    assert "ANTHROPIC_API_KEY" not in d.rationale

def test_picked_with_invalid_shape_is_rejected(tmp_path):
    """A picked value not matching TODO-N shape is rejected."""
    state = tmp_path / "state"; state.mkdir()
    p = _prompt(tmp_path)
    fake = AgentResult(
        parsed={
            "candidates_considered": ["TODO-1"],
            "picked": "rm -rf /",  # injection-shaped garbage
            "rationale": "x",
            "blocked_reasons": {},
            "in_flight": [],
        },
        prompt_sha="sha",
    )
    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        d = run_selection(tick_id="01JD", ctx=_ctx(), cfg=_cfg(state, p))
    assert d.picked is None
    assert "invalid_pick_shape" in d.rationale

def test_sha_mismatch_returns_picked_none_and_alerts(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    p = _prompt(tmp_path)
    alerts = []
    with patch(
        "hermes_pipeline.decision.call_agent",
        side_effect=PromptShaMismatch("expected", "actual"),
    ), patch(
        "hermes_pipeline.decision._emit_sha_mismatch_alert",
        side_effect=lambda *a, **kw: alerts.append((a, kw)),
    ):
        d = run_selection(tick_id="01JB", ctx=_ctx(), cfg=_cfg(state, p, expected_sha="expected"))
    assert d.picked is None
    assert "SHA" in d.rationale or "sha" in d.rationale
    assert d.rationale.startswith("prompt_sha_mismatch:")
    assert len(alerts) == 1
