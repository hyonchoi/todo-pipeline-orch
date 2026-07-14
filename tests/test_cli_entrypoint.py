"""Tests for CLI entrypoints."""

import subprocess
import sys


def test_hermes_pipeline_entrypoint_exists():
    """Verify hermes-pipeline CLI entry point is registered."""
    result = subprocess.run(
        [sys.executable, "-m", "hermes_pipeline.cli", "--version"],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "0.4.10" in result.stdout
