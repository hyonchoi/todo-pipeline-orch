"""Locks in the hermes-identity / phase-profiles directory split (TODO-32).

Regression coverage for the risk this refactor exists to close: identity
data and phase-orchestration configs must never re-mix under one namespace,
and the unknown-profile error's "Available profiles" listing must never
list identity-only directory names as if they were phase profiles.
"""
from __future__ import annotations

import pytest

from hermes_pipeline.contract import ContractSchemaError, bundled_profile_dir
from hermes_pipeline.phases import resolve_profile_phases_path


def test_bundled_profile_dir_resolves_under_hermes_identity():
    path = bundled_profile_dir()
    assert path.parts[-2:] == ("hermes-identity", "pipeline")
    assert (path / "SOUL.md").is_file()


def test_resolve_profile_phases_path_resolves_under_phase_profiles():
    path = resolve_profile_phases_path("gstack")
    assert "phase-profiles" in path.parts
    assert "gstack" in path.parts
    assert path.name == "phases.yaml"
    assert path.is_file()


def test_unknown_profile_error_excludes_identity_directory_names():
    with pytest.raises(ContractSchemaError) as exc_info:
        resolve_profile_phases_path("does-not-exist")
    message = str(exc_info.value)
    available_profiles_part = message.split("Use --profile")[0]
    assert "pipeline" not in available_profiles_part
    assert "hermes-identity" not in message
    assert "gstack" in message
    assert "agent-skills" in message
    assert "native-sdd" in message
