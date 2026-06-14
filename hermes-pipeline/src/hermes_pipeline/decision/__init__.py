"""Hermes-agent selection sub-package — public API."""
from __future__ import annotations
import datetime as _dt
import re as _re
import subprocess
from pathlib import Path as _P
from .schema import HermesSelectionDecision, SelectionContext, Outcome
from .agent import call_agent, compute_prompt_sha, PromptShaMismatch
from . import store as _store

_TODO_ID_RE = _re.compile(r"^TODO-\d+$")

__all__ = [
    "HermesSelectionDecision",
    "SelectionContext",
    "Outcome",
    "run_selection",
]

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _emit_sha_mismatch_alert(*, tick_id: str, expected: str, actual: str) -> None:
    msg = (
        f"[pipeline-tick {tick_id}] PROMPT SHA MISMATCH: "
        f"expected={expected[:12]} actual={actual[:12]}. "
        "Selection skipped (NOT counted as no-progress). "
        "Check Hermes config repo for prompt drift."
    )
    try:
        subprocess.run(
            ["hermes", "chan", "message", "alerts", msg],
            timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

def run_selection(
    *,
    tick_id: str,
    ctx: SelectionContext,
    cfg,
) -> HermesSelectionDecision:
    """Build prompt -> call agent -> persist immutable decision -> return.

    On `PromptShaMismatch`: return `picked=None`, fire Slack alert, do NOT
    raise. The caller treats this as a config-fault tick (not a no-progress
    tick) by inspecting the rationale prefix.
    """
    state_dir = _P(cfg.base.state_dir)
    prompt_path = _P(cfg.selection.prompt_path)
    model = cfg.selection.model

    try:
        result = call_agent(
            ctx=ctx,
            prompt_path=prompt_path,
            model=model,
            max_tokens=cfg.selection.max_tokens,
            expected_sha=cfg.selection.expected_prompt_sha,
        )
        parsed = result.parsed
        prompt_sha = result.prompt_sha
    except PromptShaMismatch as e:
        _emit_sha_mismatch_alert(tick_id=tick_id, expected=e.expected, actual=e.actual)
        parsed = {
            "candidates_considered": [],
            "picked": None,
            "rationale": f"prompt_sha_mismatch: expected={e.expected[:12]} actual={e.actual[:12]}",
            "blocked_reasons": {},
            "in_flight": ctx.in_flight,
        }
        prompt_sha = e.actual

    # LLM-output trust boundary: the model is free to return anything for
    # `picked`. Reject values that don't match the TODO-N shape or that
    # aren't in the candidate set we presented. On reject, null `picked`
    # and prepend the reason to `rationale` so downstream sees a "no pick"
    # config-fault tick rather than a hallucinated TODO id.
    picked = parsed.get("picked")
    if picked is not None:
        candidates = parsed.get("candidates_considered") or []
        reason = None
        if not isinstance(picked, str) or not _TODO_ID_RE.match(picked):
            reason = f"invalid_pick_shape: picked={picked!r}"
        elif picked not in candidates:
            reason = f"pick_not_in_candidates: picked={picked!r} candidates={candidates}"
        if reason is not None:
            parsed["picked"] = None
            parsed["rationale"] = f"{reason} | {parsed.get('rationale', '')}".rstrip(" |")

    decision = HermesSelectionDecision(
        tick_id=tick_id,
        timestamp=_now_iso(),
        model=model,
        prompt_sha=prompt_sha,
        candidates_considered=parsed["candidates_considered"],
        picked=parsed["picked"],
        rationale=parsed["rationale"],
        blocked_reasons=parsed["blocked_reasons"],
        in_flight=ctx.in_flight,
    )
    _store.persist(state_dir, decision)
    _store.rotate_if_needed(state_dir, hot_cap=50)
    return decision
