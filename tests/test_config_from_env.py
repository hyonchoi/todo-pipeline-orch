from __future__ import annotations

from pathlib import Path

from hermes_pipeline.config import Config


def test_from_env_layer2_config_file(monkeypatch, tmp_path):
    """Config file overrides default, env overrides file."""
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: '#config-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    cfg = Config.from_env()
    assert cfg.slack_channel == "#config-alerts"


def test_from_env_layer3_env_overrides_file(monkeypatch, tmp_path):
    """Env var overrides config file value."""
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: '#config-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#env-alerts")
    cfg = Config.from_env()
    assert cfg.slack_channel == "#env-alerts"


def test_from_env_no_projects_dir_env_var(monkeypatch):
    """Without PIPELINE_PROJECTS_DIR, projects_dir keeps its default."""
    monkeypatch.delenv("PIPELINE_PROJECTS_DIR", raising=False)
    cfg = Config.from_env()
    assert cfg.projects_dir == Path.home() / "projects"


def test_from_env_pipeline_projects_dir_deprecated_alias(monkeypatch, tmp_path):
    """PIPELINE_PROJECTS_DIR remains a deprecated env override for patch compatibility."""
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    cfg = Config.from_env()
    assert cfg.projects_dir == tmp_path / "projects"


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
        monkeypatch.setenv("PIPELINE_STATE_DIR", "~/state")
        cfg = Config.from_env()
        assert cfg.state_dir == tmp_path / "state"
    finally:
        if orig_home:
            os.environ["HOME"] = orig_home
