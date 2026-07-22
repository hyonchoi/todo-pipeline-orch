"""Tests for phases._run_hermes_subprocess — subprocess wrapper survives null-mode deletion."""
from __future__ import annotations

import pytest
from hermes_pipeline import phases as phases_mod


def test_run_hermes_subprocess_wraps_hermes_agent_result(monkeypatch):
    """_run_hermes_subprocess must unwrap HermesAgentResult into a dict."""
    from hermes_pipeline.hermes_adapter import HermesAgentResult
    monkeypatch.setattr(
        "hermes_pipeline.hermes_adapter.hermes_agent_call",
        lambda **kw: HermesAgentResult(
            returncode=0, stdout="ok", stderr="", timed_out=False,
        ),
    )
    result = phases_mod._run_hermes_subprocess(
        prompt="test", tools="Read", turns=5, timeout=30, cwd="/tmp",
    )
    assert result == {
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "timed_out": False,
    }


def test_run_hermes_subprocess_timed_out_flag_propagates(monkeypatch):
    """_run_hermes_subprocess must preserve the timed_out flag."""
    from hermes_pipeline.hermes_adapter import HermesAgentResult
    monkeypatch.setattr(
        "hermes_pipeline.hermes_adapter.hermes_agent_call",
        lambda **kw: HermesAgentResult(
            returncode=-1, stdout="", stderr="[killed on timeout]", timed_out=True,
        ),
    )
    result = phases_mod._run_hermes_subprocess(
        prompt="test", tools="Read", turns=5, timeout=30, cwd="/tmp",
    )
    assert result["timed_out"] is True
    assert result["returncode"] == -1


def test_run_hermes_subprocess_propagates_exception(monkeypatch):
    """_run_hermes_subprocess should propagate exceptions from hermes_agent_call."""
    monkeypatch.setattr(
        "hermes_pipeline.hermes_adapter.hermes_agent_call",
        lambda **kw: (_ for _ in ()).throw(FileNotFoundError("hermes not found")),
    )
    with pytest.raises(FileNotFoundError, match="hermes not found"):
        phases_mod._run_hermes_subprocess(
            prompt="test", tools="Read", turns=5, timeout=30, cwd="/tmp",
        )
