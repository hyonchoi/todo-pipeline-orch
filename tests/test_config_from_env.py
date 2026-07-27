from __future__ import annotations

from pathlib import Path

from hermes_pipeline.config import Config


def _isolate_implicit_global_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("XDG_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


def test_from_env_layer2_config_file(monkeypatch, tmp_path):
    """Config file overrides default, env overrides file."""
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: '#config-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    cfg = Config.from_env()
    assert cfg.slack_channel == "#config-alerts"


def test_from_env_pipeline_env_does_not_override_file(monkeypatch, tmp_path):
    """PIPELINE_* env vars do not override global config file entries."""
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: '#config-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#env-alerts")
    cfg = Config.from_env()
    assert cfg.slack_channel == "#config-alerts"


def test_from_env_projects_dir_default(monkeypatch, tmp_path):
    """Without config file, projects_dir keeps its default."""
    _isolate_implicit_global_config(monkeypatch, tmp_path)
    monkeypatch.delenv("PIPELINE_PROJECTS_DIR", raising=False)
    cfg = Config.from_env()
    assert cfg.projects_dir == Path.home() / "projects"


def test_from_env_pipeline_projects_dir_compat_alias(monkeypatch, tmp_path):
    """PIPELINE_PROJECTS_DIR remains a deprecated fallback when no file sets it."""
    _isolate_implicit_global_config(monkeypatch, tmp_path)
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    cfg = Config.from_env()
    assert cfg.projects_dir == tmp_path / "projects"


def test_from_env_config_projects_dir_beats_compat_alias(monkeypatch, tmp_path):
    """File config wins over the deprecated PIPELINE_PROJECTS_DIR alias."""
    f = tmp_path / "config.yaml"
    f.write_text(f"projects_dir: {tmp_path / 'from-file'}\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "from-env"))
    cfg = Config.from_env()
    assert cfg.projects_dir == tmp_path / "from-file"


def test_from_env_no_config_file_uses_default(monkeypatch):
    """Without config file, from_env returns default."""
    monkeypatch.setenv("TPO_CONFIG_FILE", "/nonexistent/path/config.yaml")
    cfg = Config.from_env()
    assert cfg == Config.default()


def test_from_env_config_file_path_expansion(monkeypatch, tmp_path):
    """Path config values should expand ~ correctly."""
    import os
    orig_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        f = tmp_path / "config.yaml"
        f.write_text("state_dir: ~/state\n")
        monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
        monkeypatch.setenv("PIPELINE_STATE_DIR", "~/state")
        cfg = Config.from_env()
        assert cfg.state_dir == tmp_path / "state"
    finally:
        if orig_home:
            os.environ["HOME"] = orig_home
