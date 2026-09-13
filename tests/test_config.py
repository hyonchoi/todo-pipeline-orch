from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pipeline.cli import _load_toml_overlay
from hermes_pipeline.config import Config


def test_defaults():
    c = Config.default()
    assert c.projects_dir == Path.home() / "projects"
    assert c.state_dir == Path.home() / ".hermes"
    assert c.log_file_subpath == "pipeline.log"
    assert c.log_retention_days == 7
    assert c.slack_channel == ""


# From test_config_from_env.py

def _isolate_implicit_global_config(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_DIR", str(tmp_path / "xdg-config"))
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
    """PIPELINE_* env vars do not override global config file entries.

    Covers: projects_dir, state_dir, slack_channel env var non-override.
    """
    f = tmp_path / "config.yaml"
    f.write_text(
        f"projects_dir: {tmp_path / 'from-file'}\n"
        f"state_dir: {tmp_path / 'state-from-file'}\n"
        "slack_channel: '#config-alerts'\n"
    )
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#env-alerts")
    cfg = Config.from_env()
    assert cfg.projects_dir == tmp_path / "from-file"
    assert cfg.state_dir == tmp_path / "state-from-file"
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


# From test_config_toml.py

def _write(p: Path, body: str) -> Path:
    p.write_text(body)
    return p


def test_loads_selection_section(tmp_path):
    from hermes_pipeline.config import load_toml_overlay

    f = _write(tmp_path / "config.toml", """
[selection]
model = "claude-opus-4-7"
max_tokens = 4000
auto_execute = false
prompt_path = ".hermes/prompts/selection.md"
expected_prompt_sha = "abc123"

[circuit_breaker]
no_progress_threshold = 3
alert_dedup_hours = 24
""")
    cfg = load_toml_overlay(Config.default(), f)
    assert cfg.selection.model == "claude-opus-4-7"
    assert cfg.selection.auto_execute is False
    assert cfg.selection.expected_prompt_sha == "abc123"
    assert cfg.circuit_breaker.no_progress_threshold == 3


def test_missing_optional_fields_use_defaults(tmp_path):
    from hermes_pipeline.config import load_toml_overlay

    f = _write(tmp_path / "config.toml", '[selection]\nmodel = "claude-opus-4-7"\n')
    cfg = load_toml_overlay(Config.default(), f)
    assert cfg.selection.auto_execute is False           # default
    assert cfg.selection.expected_prompt_sha is None     # optional
    assert cfg.circuit_breaker.no_progress_threshold == 3  # default


def test_malformed_toml_raises_with_path(tmp_path):
    from hermes_pipeline.config import load_toml_overlay

    f = _write(tmp_path / "config.toml", "[selection\nmodel = ")
    with pytest.raises(ValueError) as ei:
        load_toml_overlay(Config.default(), f)
    assert str(f) in str(ei.value)


# From test_tick_subcommand_edge.py: TestLoadTomlOverlay

class TestLoadTomlOverlay:
    """Tests for _load_toml_overlay() — TOML config loading."""

    def test_missing_config_file(self, tmp_path, mocker):
        """Missing config.toml returns (None, CircuitBreakerConfig)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = mocker.MagicMock()
        full_cfg, cb_cfg = _load_toml_overlay(state_dir, config)

        assert full_cfg is None
        assert cb_cfg is not None

    def test_config_file_exception(self, tmp_path, mocker):
        """Exception loading config.toml returns (None, CircuitBreakerConfig)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        mocker.patch(
            "hermes_pipeline.config.load_toml_overlay",
            side_effect=ValueError("bad toml"),
        )

        config = mocker.MagicMock()
        full_cfg, cb_cfg = _load_toml_overlay(state_dir, config)

        assert full_cfg is None
        assert cb_cfg is not None

    def test_valid_config_file(self, tmp_path, mocker):
        """Valid config.toml returns (FullConfig, CircuitBreakerConfig)."""
        from hermes_pipeline.config import (
            CircuitBreakerConfig,
            FullConfig,
            SelectionConfig,
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        full_cfg = FullConfig(
            base=mocker.MagicMock(),
            selection=SelectionConfig(),
            circuit_breaker=CircuitBreakerConfig(),
        )

        mocker.patch(
            "hermes_pipeline.config.load_toml_overlay",
            return_value=full_cfg,
        )

        config = mocker.MagicMock()
        result_full, result_cb = _load_toml_overlay(state_dir, config)

        assert result_full is not None
        assert result_cb is full_cfg.circuit_breaker
