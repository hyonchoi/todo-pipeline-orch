"""Tests for _detect_backend in the eval runner (no real subprocesses)."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from hermes_pipeline.hermes_adapter import (
    ClaudeDependencyError,
    HermesDependencyError,
)


def test_detect_backend_hermes_preferred():
    """When both are available, hermes wins (primary backend)."""
    from tests.eval.runner import _detect_backend

    with patch("hermes_pipeline.hermes_adapter.check_hermes", return_value="0.3"):
        with patch("hermes_pipeline.hermes_adapter.check_claude", return_value="2.0"):
            assert _detect_backend() == "hermes"


def test_detect_backend_claude_fallback():
    """When hermes is missing, claude is used."""
    from tests.eval.runner import _detect_backend

    with patch(
        "hermes_pipeline.hermes_adapter.check_hermes",
        side_effect=HermesDependencyError("not found"),
    ):
        with patch("hermes_pipeline.hermes_adapter.check_claude", return_value="2.0"):
            assert _detect_backend() == "claude"


def test_detect_backend_none_when_both_missing():
    """When neither is available, returns None."""
    from tests.eval.runner import _detect_backend

    with patch(
        "hermes_pipeline.hermes_adapter.check_claude",
        side_effect=ClaudeDependencyError("not found"),
    ):
        with patch(
            "hermes_pipeline.hermes_adapter.check_hermes",
            side_effect=HermesDependencyError("not found"),
        ):
            assert _detect_backend() is None


def test_detect_backend_hermes_failure_still_fallback():
    """When claude fails and hermes --version fails, returns None."""
    from tests.eval.runner import _detect_backend

    with patch(
        "hermes_pipeline.hermes_adapter.check_claude",
        side_effect=ClaudeDependencyError("not found"),
    ):
        with patch(
            "hermes_pipeline.hermes_adapter.check_hermes",
            side_effect=HermesDependencyError("version failed"),
        ):
            assert _detect_backend() is None


@pytest.fixture
def runner(monkeypatch):
    # Keep the legacy import-time probe provider-free on the RED baseline too.
    with patch("hermes_pipeline.hermes_adapter.check_hermes", return_value="fake"):
        from tests.eval import runner
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("TPO_RUN_LIVE_EVALS", raising=False)
    monkeypatch.delattr(runner._get_backend, "_cached", raising=False)
    return runner


@pytest.mark.parametrize("opt_in", [None, "", "0", "true"])
def test_default_evals_never_detect_backend(runner, monkeypatch, opt_in):
    if opt_in is not None:
        monkeypatch.setenv("TPO_RUN_LIVE_EVALS", opt_in)
    with patch.object(runner, "_detect_backend", return_value="hermes") as detect:
        assert runner._get_backend() is None
    detect.assert_not_called()


@pytest.mark.parametrize("backend", ["hermes", "claude"])
def test_explicit_eval_opt_in_detects_backend(runner, monkeypatch, backend):
    monkeypatch.setenv("TPO_RUN_LIVE_EVALS", "1")
    with patch.object(runner, "_detect_backend", return_value=backend) as detect:
        assert runner._get_backend() == backend
    detect.assert_called_once_with()


@pytest.mark.parametrize("worker_value", ["task-sentinel", ""])
def test_worker_context_blocks_cached_eval_backend(runner, monkeypatch, worker_value):
    monkeypatch.setenv("TPO_RUN_LIVE_EVALS", "1")
    monkeypatch.setattr(runner._get_backend, "_cached", "claude", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_FUTURE_AUTHORITY", worker_value)
    with patch.object(runner, "_detect_backend") as detect:
        assert runner._get_backend() is None
    detect.assert_not_called()


@pytest.mark.parametrize("blocked_by", ["opt_in_removed", "worker_context"])
def test_fixture_rechecks_permission_before_provider_call(runner, monkeypatch, blocked_by):
    monkeypatch.setattr(runner._get_backend, "_cached", "claude", raising=False)
    if blocked_by == "worker_context":
        monkeypatch.setenv("TPO_RUN_LIVE_EVALS", "1")
        monkeypatch.setenv("HERMES_KANBAN_TASK_ID", "task-sentinel")
    with patch.object(runner, "call_agent") as call:
        with pytest.raises(pytest.skip.Exception):
            runner.test_selection_fixture(next(runner.FIXTURE_DIR.glob("*.md")))
    call.assert_not_called()


def test_pytest_eval_collection_and_execution_never_probe_clients_by_default(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    record = tmp_path / "calls"
    for client in ("hermes", "claude"):
        executable = tmp_path / client
        executable.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            f"Path({str(record)!r}).write_text('called')\n"
            "print('fake version')\n"
        )
        executable.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if key != "TPO_RUN_LIVE_EVALS" and not key.startswith("HERMES_KANBAN_")}
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    env["PYTEST_ADDOPTS"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/eval/runner.py", "-q"],
        cwd=Path(__file__).resolve().parents[2], env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert not record.exists(), "default pytest invoked an installed provider client"
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" in result.stdout
