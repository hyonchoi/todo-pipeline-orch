from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pipeline.config import Config


def test_from_env_layer2_config_file(monkeypatch, tmp_path):
    """Config file overrides default, env overrides file."""
    f = tmp_path / "config.yaml"
    f.write_text("claude_cmd: claude-code\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    cfg = Config.from_env()
    assert cfg.claude_cmd == "claude-code"


def test_from_env_layer3_env_overrides_file(monkeypatch, tmp_path):
    """Env var overrides config file value."""
    f = tmp_path / "config.yaml"
    f.write_text("claude_cmd: claude-code\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_CLAUDE_CMD", "claude-override")
    cfg = Config.from_env()
    assert cfg.claude_cmd == "claude-override"


def test_from_env_no_projects_dir_env_var():
    """PIPELINE_PROJECTS_DIR should not be in env_map."""
    cfg = Config.from_env()
    assert cfg.projects_dir == Path.home() / "projects"


def test_from_env_pipeline_projects_dir_ignored(monkeypatch, tmp_path):
    """PIPELINE_PROJECTS_DIR env var should be ignored."""
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "ignored"))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    cfg = Config.from_env()
    assert cfg.projects_dir == Path.home() / "projects"


def test_from_env_kanban_literal_validation_env(monkeypatch, tmp_path):
    """Env var with invalid kanban_adapter value should raise."""
    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_KANBAN_ADAPTER", "banana")
    with pytest.raises(ValueError, match="must be one of"):
        Config.from_env()


def test_from_env_no_config_file_uses_default(monkeypatch):
    """Without config file, from_env returns default."""
    monkeypatch.setenv("TPO_CONFIG_FILE", "/nonexistent/path/config.yaml")
    cfg = Config.from_env()
    assert cfg == Config.default()


def test_from_env_env_var_path_expansion(monkeypatch, tmp_path):
    """Path env vars should expand ~ correctly."""
    import os
    orig_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        f = tmp_path / "config.yaml"
        f.write_text("")
        monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
        monkeypatch.setenv("PIPELINE_LOCK_DIR", "~/locks")
        cfg = Config.from_env()
        assert cfg.lock_dir == tmp_path / "locks"
    finally:
        if orig_home:
            os.environ["HOME"] = orig_home
