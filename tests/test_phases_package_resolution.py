"""Verify load_phases() resolves phases.yaml from in-package data, not repo root."""
from __future__ import annotations
import os
from pathlib import Path

from hermes_pipeline.phases import load_phases


def test_load_phases_uses_package_data(tmp_path):
    """load_phases() resolves phases.yaml from in-package data, not repo root."""
    # Should load even from a CWD that has no configs/ directory
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        phases = load_phases()
        assert len(phases) > 0
        assert phases[0].phase_key == "phase_2_autoplan"
    finally:
        os.chdir(original_cwd)
