from hermes_pipeline.cli import main

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


def test_config_init_creates_file(monkeypatch, tmp_path):
    """tpo config init creates skeleton file at default path."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_DIR", str(xdg))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    exit_code = main(["config", "init"])
    assert exit_code == 0
    assert (xdg / "tpo" / "config.yaml").exists()
    content = (xdg / "tpo" / "config.yaml").read_text()
    assert "projects_dir" in content


def test_config_init_refuses_existing(monkeypatch, tmp_path):
    """tpo config init refuses to overwrite without --force."""
    xdg = tmp_path / "xdg"
    (xdg / "tpo").mkdir(parents=True)
    (xdg / "tpo" / "config.yaml").write_text("existing")
    monkeypatch.setenv("XDG_CONFIG_DIR", str(xdg))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    exit_code = main(["config", "init"])
    assert exit_code == 1


def test_config_init_force_overwrites(monkeypatch, tmp_path):
    """tpo config init --force overwrites existing file."""
    xdg = tmp_path / "xdg"
    (xdg / "tpo").mkdir(parents=True)
    (xdg / "tpo" / "config.yaml").write_text("existing")
    monkeypatch.setenv("XDG_CONFIG_DIR", str(xdg))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    exit_code = main(["config", "init", "--force"])
    assert exit_code == 0
    content = (xdg / "tpo" / "config.yaml").read_text()
    assert "existing" not in content


def test_config_init_rejects_symlink(monkeypatch, tmp_path):
    """tpo config init refuses symlink paths before writing."""
    link = tmp_path / "config.yaml"
    link.symlink_to(tmp_path / "missing-target.yaml")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(link))
    exit_code = main(["config", "init", "--force"])
    assert exit_code == 2
    assert not (tmp_path / "missing-target.yaml").exists()


# -- path --


def test_config_path_no_file(monkeypatch, tmp_path, capsys):
    """tpo config path shows default when no file exists."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    exit_code = main(["config", "path"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No config file found" in captured.out


def test_config_path_with_file(monkeypatch, tmp_path, capsys):
    """tpo config path shows existing file."""
    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "path"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Using:" in captured.out
    assert str(f) in captured.out


# -- get --


def test_config_get_default(monkeypatch, tmp_path, capsys):
    """tpo config get shows default when no config file."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("PIPELINE_SLACK_CHANNEL", raising=False)
    exit_code = main(["config", "get", "slack_channel"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "slack_channel:" in captured.out
    assert "default" in captured.out.lower()


def test_config_get_from_file(monkeypatch, tmp_path, capsys):
    """tpo config get shows value from config file."""
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: '#config-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.delenv("PIPELINE_SLACK_CHANNEL", raising=False)
    exit_code = main(["config", "get", "slack_channel"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "#config-alerts" in captured.out


def test_config_get_env_override(monkeypatch, tmp_path, capsys):
    """tpo config get shows env var override with attribution."""
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: '#config-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#env-alerts")
    exit_code = main(["config", "get", "slack_channel"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "#env-alerts" in captured.out
    assert "PIPELINE_SLACK_CHANNEL" in captured.out or "env" in captured.out


def test_config_get_invalid_key(monkeypatch, tmp_path):
    """tpo config get rejects unknown key."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    exit_code = main(["config", "get", "nonexistent"])
    assert exit_code != 0


def test_config_get_broken_config_recovery(monkeypatch, tmp_path, capsys):
    """tpo config get recovers gracefully when config has errors."""
    f = tmp_path / "config.yaml"
    f.write_text("badkey: value\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "get", "slack_channel"])
    assert exit_code == 0
    captured = capsys.readouterr()
    # Verify the default value is shown (recovery succeeded)
    assert "slack_channel:" in captured.out
    # Verify the warning/fallback message appeared (recovery path exercised)
    assert (
        "warning" in captured.out.lower()
        or "fallback" in captured.out.lower()
        or "error" in captured.out.lower()
    )


def test_config_get_broken_config_still_applies_env(monkeypatch, tmp_path, capsys):
    """tpo config get reports env values even when the config file is broken."""
    f = tmp_path / "config.yaml"
    f.write_text("badkey: value\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#env-alerts")
    exit_code = main(["config", "get", "slack_channel"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "#env-alerts" in captured.out
    assert "PIPELINE_SLACK_CHANNEL" in captured.out


# -- set --


def test_config_set_creates_file(monkeypatch, tmp_path, capsys):
    """tpo config set auto-creates config file if missing."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "config.yaml"))
    exit_code = main(["config", "set", "slack_channel", "#config-alerts"])
    assert exit_code == 0
    assert (tmp_path / "config.yaml").exists()
    captured = capsys.readouterr()
    assert "#config-alerts" in captured.out


def test_config_set_overrides_value(monkeypatch, tmp_path):
    """tpo config set writes value to existing file."""
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: '#old-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "slack_channel", "#config-alerts"])
    assert exit_code == 0
    raw = yaml.safe_load(f.read_text())
    assert raw["slack_channel"] == "#config-alerts"


def test_config_set_uncomments_existing(monkeypatch, tmp_path):
    """tpo config set uncomments an existing commented key."""
    from hermes_pipeline.config_loader import SKELETON

    f = tmp_path / "config.yaml"
    f.write_text(SKELETON)
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "projects_dir", "/opt/projects"])
    assert exit_code == 0
    content = f.read_text()
    assert "# projects_dir:" not in content
    assert "projects_dir: /opt/projects" in content


def test_config_set_preserves_comments(monkeypatch, tmp_path):
    """tpo config set preserves unrelated comments."""
    f = tmp_path / "config.yaml"
    f.write_text("# my comment\nslack_channel: '#old-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "slack_channel", "#config-alerts"])
    assert exit_code == 0
    assert "# my comment" in f.read_text()


def test_config_set_updates_active_duplicate_after_skeleton(monkeypatch, tmp_path):
    """tpo config set updates the effective active key when duplicates exist."""
    from hermes_pipeline.config import Config
    from hermes_pipeline.config_loader import SKELETON

    f = tmp_path / "config.yaml"
    f.write_text(SKELETON + "\nslack_channel: '#old-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "slack_channel", "#new-alerts"])
    assert exit_code == 0
    assert Config.from_env().slack_channel == "#new-alerts"


def test_config_set_invalid_key(monkeypatch, tmp_path):
    """tpo config set rejects unknown key."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "config.yaml"))
    exit_code = main(["config", "set", "nonexistent", "value"])
    assert exit_code != 0


def test_config_set_path_type_coercion(monkeypatch, tmp_path):
    """tpo config set coerces string to Path type."""
    from pathlib import Path

    from hermes_pipeline.config_loader import load_global_config

    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "projects_dir", "/opt/projects"])
    assert exit_code == 0
    cfg = load_global_config()
    assert cfg.projects_dir == Path("/opt/projects")


def test_config_set_int_type_coercion(monkeypatch, tmp_path):
    """tpo config set coerces string to int type."""
    from hermes_pipeline.config_loader import load_global_config

    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "log_retention_days", "14"])
    assert exit_code == 0
    cfg = load_global_config()
    assert cfg.log_retention_days == 14


def test_config_set_symlink_rejected(monkeypatch, tmp_path):
    """tpo config set rejects symlinked config file."""
    real = tmp_path / "real.yaml"
    real.write_text("")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    monkeypatch.setenv("TPO_CONFIG_FILE", str(link))
    exit_code = main(["config", "set", "slack_channel", "#test"])
    assert exit_code != 0


def test_config_set_dangling_symlink_rejected_before_write(monkeypatch, tmp_path):
    """tpo config set refuses dangling symlinks before auto-creating a file."""
    link = tmp_path / "config.yaml"
    target = tmp_path / "missing-target.yaml"
    link.symlink_to(target)
    monkeypatch.setenv("TPO_CONFIG_FILE", str(link))
    exit_code = main(["config", "set", "slack_channel", "#test"])
    assert exit_code == 2
    assert not target.exists()


def test_config_set_yaml_special_chars_quoted(monkeypatch, tmp_path):
    """tpo config set quotes values with YAML-special characters."""
    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "slack_channel", "#general: alerts"])
    assert exit_code == 0
    raw = yaml.safe_load(f.read_text())
    assert raw["slack_channel"] == "#general: alerts"


def test_config_set_path_special_chars_quoted(monkeypatch, tmp_path):
    """tpo config set quotes Path values with YAML-special characters."""
    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "projects_dir", "/tmp/foo #bar"])
    assert exit_code == 0
    raw = yaml.safe_load(f.read_text())
    assert raw["projects_dir"] == "/tmp/foo #bar"
