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
# Pulls every `TODO-N` token out of TODOS.md. Header lines, body references,
# checkbox lists — anything formatted as the canonical id. The agent's
# `candidates_considered` and `picked` fields are checked against THIS set,
# not the model's self-reported set (which is also LLM output).
_TODOS_ID_RE = _re.compile(r"\bTODO-\d+\b")

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
    timeout: int | None = None,
) -> HermesSelectionDecision:
    """Build prompt -> call agent -> persist immutable decision -> return.

    On `PromptShaMismatch`: return `picked=None`, fire Slack alert, do NOT
    raise. The caller treats this as a config-fault tick (not a no-progress
    tick) by inspecting the rationale prefix.

    Args:
        timeout: Hard ceiling (seconds) for the agent call. When None, the
            agent auto-derives a timeout from ``max_tokens``. Callers bound by
            a per-project tick budget pass an explicit value so the call cannot
            outlive the lock that protects it.
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
            timeout=timeout,
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
    except KeyError as e:
        # Config fault — missing env var. Persist a
        # decision so the next tick's `recent_decisions` carries the cause,
        # but do not crash the cron entrypoint.
        parsed = {
            "candidates_considered": [],
            "picked": None,
            "rationale": f"config_error: missing env var {e.args[0]!r}",
            "blocked_reasons": {},
            "in_flight": ctx.in_flight,
        }
        prompt_sha = ""
    except Exception as e:
        # Hermes call surface — 401/429/5xx/network/timeout/CLI errors —
        # plus any other transport error. The plan's edge-case contract:
        # produce picked=None with a distinct rationale; the circuit breaker
        # treats it as no-progress (caller responsibility).
        rationale = f"api_error: {type(e).__name__}: {str(e)[:200]}"
        try:
            prompt_sha = compute_prompt_sha(prompt_path)
        except OSError:
            prompt_sha = ""
        parsed = {
            "candidates_considered": [],
            "picked": None,
            "rationale": rationale,
            "blocked_reasons": {},
            "in_flight": ctx.in_flight,
        }

    # LLM-output trust boundary. Three failure modes to gate against:
    #   1. `picked` doesn't match the TODO-N shape (model returned a string,
    #      a dict, a hallucinated value).
    #   2. `picked` is shaped correctly but doesn't appear in TODOS.md at
    #      all — a hallucinated TODO id the model invented.
    #   3. `picked` is in TODOS.md but was filtered out (e.g., it's already
    #      in_flight from a prior tick).
    # Validate against the server-parsed TODO ids in `ctx.todos_md`, NOT
    # against the LLM-supplied `candidates_considered` (which is itself
    # untrusted output and can be made to agree with `picked` by injection).
    real_ids = set(_TODOS_ID_RE.findall(ctx.todos_md))
    in_flight_set = set(ctx.in_flight)
    picked = parsed.get("picked")
    if picked is not None:
        reason = None
        if not isinstance(picked, str) or not _TODO_ID_RE.match(picked):
            reason = f"invalid_pick_shape: picked={picked!r}"
        elif picked not in real_ids:
            reason = f"pick_not_in_todos_md: picked={picked!r} known={sorted(real_ids)}"
        elif picked in in_flight_set:
            reason = f"pick_already_in_flight: picked={picked!r}"
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
