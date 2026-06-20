"""Tests for _detect_backend in the eval runner (no real subprocesses)."""
from __future__ import annotations
from unittest.mock import patch

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
