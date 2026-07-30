from __future__ import annotations

from importlib.resources import files


def test_pipeline_soul_requires_external_client_delegation():
    soul = (
        files("hermes_pipeline")
        .joinpath("data", "hermes-identity", "pipeline", "SOUL.md")
        .read_text(encoding="utf-8")
    )

    assert "External Client Delegation" in soul
    assert "You are the Hermes dispatcher" in soul
    assert "Codex phases must run via `codex exec" in soul
    assert "Claude Code phases must run via `claude -p" in soul
    assert "Do not implement, review, ship, or edit phase work directly" in soul
