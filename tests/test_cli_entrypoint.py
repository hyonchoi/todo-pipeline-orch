"""Tests for CLI entrypoints."""

import re
import subprocess
import sys


def test_cli_entrypoint_module_runs():
    """Verify the hermes_pipeline.cli module is runnable as a CLI entry point."""
    result = subprocess.run(
        [sys.executable, "-m", "hermes_pipeline.cli", "--version"],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert re.search(r"\d+\.\d+\.\d+", result.stdout), f"stdout: {result.stdout}"
