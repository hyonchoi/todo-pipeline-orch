"""Eval runner — exercises real Anthropic API via hermes or Claude Code CLI.
Skipped only when neither backend is available.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import pytest
from hermes_pipeline.decision.schema import SelectionContext
from hermes_pipeline.decision.agent import call_agent

FIXTURE_DIR = Path(__file__).parent / "selection"
PROMPT_PATH = Path(os.environ.get("SELECTION_PROMPT_PATH", ".hermes/prompts/selection.md"))


def _detect_backend() -> str | None:
    """Detect which backend is available for eval API calls.

    Priority:
    1. hermes (if installed) — primary backend; the project is built on it.
    2. Claude Code CLI (if installed) — fallback when hermes isn't available.

    Both handle authentication internally (keychain, OAuth, or env vars).

    Returns:
        "hermes" or "claude" if available, None otherwise.
    """
    from hermes_pipeline import hermes_adapter

    # Try hermes first (primary backend)
    try:
        hermes_adapter.check_hermes()
        return "hermes"
    except hermes_adapter.HermesDependencyError:
        pass

    # Fall back to Claude Code CLI
    try:
        hermes_adapter.check_claude()
        return "claude"
    except hermes_adapter.ClaudeDependencyError:
        pass

    return None


def _get_backend() -> str | None:
    """Lazily detect and cache the eval backend (avoids import-time subprocesses)."""
    if not hasattr(_get_backend, "_cached"):
        _get_backend._cached = _detect_backend()  # type: ignore[attr-defined]
    return _get_backend._cached  # type: ignore[attr-defined]


def _backend_available() -> bool:
    """Check if any eval backend is available (for pytest skipif)."""
    return _get_backend() is not None


def _parse_fixture(p: Path) -> tuple[dict, str]:
    text = p.read_text()
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    import yaml
    return yaml.safe_load(fm), body.lstrip("\n")


@pytest.mark.skipif(
    not _backend_available(),
    reason="eval suite requires hermes or Claude Code CLI",
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
        model=os.environ.get("EVAL_MODEL", "auto"),
        max_tokens=2000, expected_sha=None,
        backend=_get_backend(),
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
