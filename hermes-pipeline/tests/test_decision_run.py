from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import patch
from hermes_pipeline.decision import (
    run_selection, HermesSelectionDecision, SelectionContext,
)
from hermes_pipeline.decision.agent import AgentResult, PromptShaMismatch
from hermes_pipeline.config import Config, FullConfig, SelectionConfig, CircuitBreakerConfig

def _cfg(state_dir: Path, prompt_path: Path, expected_sha=None) -> FullConfig:
    return FullConfig(
        base=Config(state_dir=state_dir),
        selection=SelectionConfig(
            model="m", max_tokens=100, auto_execute=False,
            prompt_path=str(prompt_path), expected_prompt_sha=expected_sha,
        ),
        circuit_breaker=CircuitBreakerConfig(),
    )

def _prompt(tmp_path: Path) -> Path:
    p = tmp_path / "p.md"
    p.write_text("PROMPT")
    return p

def _ctx() -> SelectionContext:
    return SelectionContext("- TODO-1", [], [], {}, "demo")

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
        raw_response="{}",
    )
    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        d = run_selection(tick_id="01JA", ctx=_ctx(), cfg=_cfg(state, p))
    assert isinstance(d, HermesSelectionDecision)
    assert d.picked == "TODO-1"
    assert (state / "decisions" / "01JA.json").exists()

def test_picked_not_in_candidates_is_rejected(tmp_path):
    """LLM-output trust boundary: picked must be in candidates_considered."""
    state = tmp_path / "state"; state.mkdir()
    p = _prompt(tmp_path)
    fake = AgentResult(
        parsed={
            "candidates_considered": ["TODO-1", "TODO-2"],
            "picked": "TODO-999",  # hallucinated
            "rationale": "I like this one",
            "blocked_reasons": {},
            "in_flight": [],
        },
        prompt_sha="sha", raw_response="{}",
    )
    with patch("hermes_pipeline.decision.call_agent", return_value=fake):
        d = run_selection(tick_id="01JC", ctx=_ctx(), cfg=_cfg(state, p))
    assert d.picked is None
    assert "pick_not_in_candidates" in d.rationale

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
        prompt_sha="sha", raw_response="{}",
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
