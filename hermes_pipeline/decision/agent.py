"""Prompt build, SHA pin, Hermes API call, response parse."""
from __future__ import annotations
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from .schema import SelectionContext

log = logging.getLogger(__name__)
MAX_TRACE_CHARS = 2000  # Truncate agent payloads to this length in logs

class PromptShaMismatch(Exception):
    """Raised when expected_prompt_sha != actual prompt SHA. NOT a no-progress event."""
    def __init__(self, expected: str, actual: str):
        super().__init__(f"prompt SHA mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual

@dataclass(frozen=True)
class AgentResult:
    parsed: dict
    prompt_sha: str
    raw_response: str

def compute_prompt_sha(prompt_path: Path) -> str:
    return hashlib.sha256(Path(prompt_path).read_bytes()).hexdigest()

_FENCE_TAGS = ("</todos_md_content>", "</recent_decisions>", "</in_flight>", "</kanban_snapshot>")

def _fence_safe(s: str) -> str:
    """Neutralize any closing fence tag inside untrusted content.

    The prompt template anchors LLM-untrusted regions inside `<tag>...</tag>`
    fences. A TODOS.md containing the literal closing tag would let an
    attacker break out of the fenced region and inject instructions. We
    inject a zero-width space after the leading `<` so the string is
    visibly identical but no longer matches the parser's closing tag.
    """
    out = s
    for tag in _FENCE_TAGS:
        out = out.replace(tag, tag[0] + "​" + tag[1:])
    return out

def build_prompt(prompt_path: Path, ctx: SelectionContext) -> str:
    body = Path(prompt_path).read_text()
    parts = [
        body,
        "",
        "<todos_md_content>",
        _fence_safe(ctx.todos_md),
        "</todos_md_content>",
        "",
        "<recent_decisions>",
        _fence_safe(json.dumps(ctx.recent_decisions, indent=2, sort_keys=True)),
        "</recent_decisions>",
        "",
        "<in_flight>",
        _fence_safe(json.dumps(ctx.in_flight)),
        "</in_flight>",
        "",
        "<kanban_snapshot>",
        _fence_safe(json.dumps(ctx.kanban_snapshot, indent=2, sort_keys=True)),
        "</kanban_snapshot>",
        "",
        f"project_slug: {ctx.project_slug}",
    ]
    return "\n".join(parts)

TOKENS_PER_SECOND = 100  # estimated throughput for timeout estimation
MIN_TIMEOUT_SECONDS = 30  # minimum hermes call timeout
MAX_TIMEOUT_SECONDS = 300  # maximum hermes call timeout (5 minutes)

def _api_call(*, model: str, max_tokens: int, prompt: str, backend: str = "hermes") -> str:
    from .. import hermes_adapter

    timeout = min(max(max_tokens // TOKENS_PER_SECOND, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)

    if backend == "claude":
        return hermes_adapter.claude_call(model=model, prompt=prompt, timeout=timeout)

    # Default: hermes
    return hermes_adapter.hermes_call(model=model, prompt=prompt, timeout=timeout)

def _parse(raw: str) -> dict:
    body = raw.strip()
    if body.startswith("```"):
        body = body.split("```", 2)[1]
        if body.lstrip().lower().startswith("json"):
            body = body.split("\n", 1)[1]
        body = body.rsplit("```", 1)[0]
    try:
        d = json.loads(body)
        return {
            "candidates_considered": list(d.get("candidates_considered", [])),
            "picked": d.get("picked"),
            "rationale": str(d.get("rationale", "")),
            "blocked_reasons": dict(d.get("blocked_reasons", {})),
            "in_flight": list(d.get("in_flight", [])),
        }
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        return {
            "candidates_considered": [],
            "picked": None,
            "rationale": f"parse error: {e}; raw response (truncated): {raw[:300]}",
            "blocked_reasons": {},
            "in_flight": [],
        }

def call_agent(
    *,
    ctx: SelectionContext,
    prompt_path: Path,
    model: str,
    max_tokens: int,
    expected_sha: str | None,
    backend: str = "hermes",
) -> AgentResult:
    """Call the selection agent via the specified backend.

    Args:
        ctx: Selection context (todos, in-flight, etc.).
        prompt_path: Path to the prompt markdown template.
        model: Model identifier.
        max_tokens: Maximum tokens for the response.
        expected_sha: Expected SHA of the prompt (for pin verification).
        backend: API backend to use — "hermes" or "claude".

    Returns:
        AgentResult with parsed response, prompt SHA, and raw output.
    """
    actual_sha = compute_prompt_sha(prompt_path)
    if expected_sha is not None and expected_sha != actual_sha:
        raise PromptShaMismatch(expected_sha, actual_sha)
    rendered = build_prompt(prompt_path, ctx)
    # DEBUG-level so --debug surfaces raw agent prompts/responses to stderr
    # and the file handler. Truncated to MAX_TRACE_CHARS to avoid bloating logs.
    log.debug("agent prompt (truncated to %d chars): %s", MAX_TRACE_CHARS, rendered[:MAX_TRACE_CHARS])
    raw = _api_call(model=model, max_tokens=max_tokens, prompt=rendered, backend=backend)
    log.debug("agent raw response (truncated to %d chars): %s", MAX_TRACE_CHARS, raw[:MAX_TRACE_CHARS])
    return AgentResult(parsed=_parse(raw), prompt_sha=actual_sha, raw_response=raw)
