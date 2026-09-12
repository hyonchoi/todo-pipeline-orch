from __future__ import annotations

from importlib.resources import files


def test_pipeline_soul_requires_registered_supervisor_dispatch():
    soul = (
        files("hermes_pipeline")
        .joinpath("data", "hermes-identity", "pipeline", "SOUL.md")
        .read_text(encoding="utf-8")
    )

    assert "Registered Execution" in soul
    assert "You are the Hermes dispatcher" in soul
    assert "tpo-agent-supervisor" in soul
    assert "invoke or reconnect" in soul
    assert "generation" in soul
    assert "completion_allowed" in soul
    assert "ai-coding-agents" not in soul
    assert "codex exec" not in soul
    assert "claude -p" not in soul
    assert "Do not implement, review, ship, or edit phase work directly" in soul
