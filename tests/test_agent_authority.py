import hashlib
import os

import pytest

from hermes_pipeline import agent_authority as authority
from hermes_pipeline.agent_execution import ExecutionError


def test_profile_authority_ignores_environment_overrides(tmp_path, monkeypatch):
    home = tmp_path / "account"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(authority, "account_home", lambda: home)
    for key in ("HOME", "XDG_CONFIG_DIR", "HERMES_HOME", "TPO_CONFIG_FILE"):
        monkeypatch.setenv(key, str(tmp_path / "forged"))
    expected = home / ".hermes" / "agent-executions" / hashlib.sha256(os.fsencode(project)).hexdigest()
    assert authority.profile_root(project) == expected


def test_profile_authority_supports_trusted_conventional_configuration(tmp_path, monkeypatch):
    home = tmp_path / "account"
    source = home / ".config" / "tpo" / "config.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("state_dir: ~/custom-state\n")
    monkeypatch.setattr(authority, "account_home", lambda: home)
    monkeypatch.setenv("HOME", str(tmp_path / "forged"))
    assert authority.profile_root(tmp_path).parent == home / "custom-state" / "agent-executions"


def test_profile_authority_rejects_relative_or_symlink_configuration(tmp_path, monkeypatch):
    home = tmp_path / "account"
    source = home / ".config" / "tpo" / "config.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("state_dir: relative-path\n")
    monkeypatch.setattr(authority, "account_home", lambda: home)
    with pytest.raises(ExecutionError):
        authority.profile_root(tmp_path)
    source.unlink()
    forged = tmp_path / "forged.yaml"
    forged.write_text("state_dir: /tmp/forged\n")
    source.symlink_to(forged)
    with pytest.raises(ExecutionError):
        authority.profile_root(tmp_path)
