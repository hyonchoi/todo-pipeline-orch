"""Hermes-agent selection sub-package — public API."""
from __future__ import annotations

import datetime as _dt
import re as _re
import subprocess
from contextlib import nullcontext as _nullcontext
from importlib.resources import as_file as _as_file
from importlib.resources import files as _resource_files
from pathlib import Path as _P

from hermes_pipeline.hermes_adapter import (
    AgentClientDependencyError,
    ClaudeCallError,
    HermesCallError,
    HermesDependencyError,
)
from hermes_pipeline.todos_md import parse_todo_entries, todo_entry_ids

from . import store as _store
from .agent import PromptShaMismatch, call_agent, compute_prompt_sha
from .schema import HermesSelectionDecision, Outcome, SelectionContext

_TODO_ID_RE = _re.compile(r"^TODO-\d+$")

__all__ = [
    "HermesSelectionDecision",
    "Outcome",
    "SelectionContext",
    "record_no_candidates",
    "run_selection",
]


def record_no_candidates(
    *, tick_id: str, ctx: SelectionContext, cfg, blocked_reasons: dict[str, str]
) -> HermesSelectionDecision:
    """Persist a deterministic no-pick without invoking the selection agent."""
    decision = HermesSelectionDecision(
        tick_id=tick_id,
        timestamp=_now_iso(),
        model=cfg.selection.model,
        prompt_sha="",
        candidates_considered=[],
        picked=None,
        rationale="no_eligible_candidates",
        blocked_reasons=blocked_reasons,
        in_flight=ctx.in_flight,
    )
    state_dir = _P(cfg.base.state_dir)
    _store.persist(state_dir, decision)
    _store.rotate_if_needed(state_dir, hot_cap=50)
    return decision


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

def _emit_sha_mismatch_alert(*, tick_id: str, expected: str, actual: str) -> None:
    msg = (
        f"[pipeline-tick {tick_id}] PROMPT SHA MISMATCH: "
        f"expected={expected[:12]} actual={actual[:12]}. "
        "Selection skipped (NOT counted as no-progress). "
        "Check TPO selection prompt for drift."
    )
    try:
        subprocess.run(
            ["hermes", "chan", "message", "alerts", msg],
            timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

def _selection_prompt_path(prompt_path: str | None):
    if prompt_path:
        return _nullcontext(_P(prompt_path))
    return _as_file(
        _resource_files("hermes_pipeline.data").joinpath("prompts", "selection.md")
    )


def _api_error_code(exc: Exception) -> str:
    if isinstance(exc, HermesCallError):
        return "hermes_error"
    if isinstance(exc, ClaudeCallError):
        return "claude_error"
    if isinstance(exc, (HermesDependencyError, AgentClientDependencyError, FileNotFoundError)):
        return "dependency_error"
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        return "timeout"
    if isinstance(exc, OSError):
        return "transport_error"
    return "unexpected_error"

def run_selection(
    *,
    tick_id: str,
    ctx: SelectionContext,
    cfg,
    timeout: int | None = None,
    eligible_todo_ids: frozenset[str] | None = None,
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
    model = cfg.selection.model

    with _selection_prompt_path(cfg.selection.prompt_path) as prompt_path:
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
        except KeyError:
            # Config fault — missing required setting. Persist a
            # decision so the next tick's `recent_decisions` carries the cause,
            # but do not crash the cron entrypoint.
            parsed = {
                "candidates_considered": [],
                "picked": None,
                "rationale": "config_error: missing_setting",
                "blocked_reasons": {},
                "in_flight": ctx.in_flight,
            }
            prompt_sha = ""
        except Exception as e:
            # Hermes call surface — 401/429/5xx/network/timeout/CLI errors —
            # plus any other transport error. The plan's edge-case contract:
            # produce picked=None with a distinct rationale; the circuit breaker
            # treats it as no-progress (caller responsibility).
            rationale = f"api_error: {_api_error_code(e)}"
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
    #   2. `picked` is shaped correctly but isn't a declared TODOS.md entry.
    #   3. `picked` is a declared entry but was filtered out (e.g., it's already
    #      in_flight from a prior tick).
    # Validate against the server-parsed TODO ids in `ctx.todos_md`, NOT
    # against the LLM-supplied `candidates_considered` (which is itself
    # untrusted output and can be made to agree with `picked` by injection).
    ordered_real_ids = [entry.todo_id for entry in parse_todo_entries(ctx.todos_md)]
    real_ids = todo_entry_ids(ctx.todos_md)
    allowed_ids = set(eligible_todo_ids) if eligible_todo_ids is not None else real_ids
    in_flight_set = set(ctx.in_flight)
    picked = parsed.get("picked")
    if picked is not None:
        reason = None
        if not isinstance(picked, str) or not _TODO_ID_RE.match(picked):
            reason = f"invalid_pick_shape: picked={picked!r}"
        elif picked not in real_ids:
            reason = f"pick_not_in_todos_md: picked={picked!r} known={sorted(real_ids)}"
        elif picked not in allowed_ids:
            reason = f"pick_not_eligible: picked={picked!r} eligible={sorted(allowed_ids)}"
        elif picked in in_flight_set:
            reason = f"pick_already_in_flight: picked={picked!r}"
        if reason is not None:
            parsed["picked"] = None
            parsed["rationale"] = f"{reason} | {parsed.get('rationale', '')}".rstrip(" |")

    if eligible_todo_ids is not None:
        parsed["candidates_considered"] = [
            todo_id for todo_id in ordered_real_ids if todo_id in allowed_ids
        ]
    blocked_reasons = parsed.get("blocked_reasons", {})
    parsed["blocked_reasons"] = {
        todo_id: reason
        for todo_id, reason in blocked_reasons.items()
        if todo_id in allowed_ids and isinstance(reason, str)
    }

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
