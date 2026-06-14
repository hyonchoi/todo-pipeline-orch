"""Prompt build, SHA pin, Anthropic API call, response parse."""
from __future__ import annotations
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from .schema import SelectionContext

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

def _anthropic_call(*, model: str, max_tokens: int, prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

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
) -> AgentResult:
    actual_sha = compute_prompt_sha(prompt_path)
    if expected_sha is not None and expected_sha != actual_sha:
        raise PromptShaMismatch(expected_sha, actual_sha)
    rendered = build_prompt(prompt_path, ctx)
    raw = _anthropic_call(model=model, max_tokens=max_tokens, prompt=rendered)
    return AgentResult(parsed=_parse(raw), prompt_sha=actual_sha, raw_response=raw)
