"""Tests for CLI entrypoints."""

import subprocess
import shutil
import sys


def test_hermes_pipeline_entrypoint_exists():
    """Verify hermes-pipeline CLI entry point is registered and runs."""
    result = subprocess.run(
        [sys.executable, "-m", "hermes_pipeline.cli", "--version"],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # The module runs successfully; version output contains the package version
    assert "0." in result.stdout  # version string present


def test_hermes_pipeline_script_installed():
    """Verify hermes-pipeline script is installed in the environment."""
    script = shutil.which("hermes-pipeline")
    assert script is not None, "hermes-pipeline entrypoint script not found in PATH"
    result = subprocess.run([script, "--version"], capture_output=True, text=True)
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "hermes-pipeline" in result.stdout
