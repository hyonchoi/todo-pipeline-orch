"""Eval runner — exercises real Anthropic API. Skipped without ANTHROPIC_API_KEY."""
from __future__ import annotations
import json
import os
from pathlib import Path
import pytest
from hermes_pipeline.decision.schema import SelectionContext
from hermes_pipeline.decision.agent import call_agent

FIXTURE_DIR = Path(__file__).parent / "selection"
PROMPT_PATH = Path(os.environ.get("SELECTION_PROMPT_PATH", ".hermes/prompts/selection.md"))


def _parse_fixture(p: Path) -> tuple[dict, str]:
    text = p.read_text()
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    import yaml
    return yaml.safe_load(fm), body.lstrip("\n")


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="eval suite requires ANTHROPIC_API_KEY",
)
@pytest.mark.parametrize("fixture_path", sorted(FIXTURE_DIR.glob("*.md")), ids=lambda p: p.stem)
def test_selection_fixture(fixture_path):
    meta, body = _parse_fixture(fixture_path)
    ctx = SelectionContext(
        todos_md=body,
        in_flight=meta.get("in_flight", []),
        recent_decisions=meta.get("recent_decisions", []),
        kanban_snapshot={"columns": []},
        project_slug="eval",
    )
    r = call_agent(
        ctx=ctx, prompt_path=PROMPT_PATH,
        model=os.environ.get("EVAL_MODEL", "claude-opus-4-7"),
        max_tokens=2000, expected_sha=None,
    )
    picked = r.parsed["picked"]
    if meta.get("expected_picked_is_none"):
        assert picked is None, f"expected None, got {picked!r}; rationale={r.parsed['rationale']!r}"
    else:
        picked_in = meta.get("expected_picked_in", [])
        assert picked in picked_in, (
            f"picked={picked!r} not in {picked_in!r}; "
            f"rationale={r.parsed['rationale']!r}"
        )
        for bad in meta.get("expected_picked_not", []):
            assert picked != bad, (
                f"picked={picked!r} should not be {bad!r}; "
                f"rationale={r.parsed['rationale']!r}"
            )
