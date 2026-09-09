import pytest

from hermes_pipeline.cli import main
from hermes_pipeline.config_loader import load_global_config

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


def test_config_init_creates_file(monkeypatch, tmp_path):
    """tpo config init creates skeleton file at default path."""
    xdg = tmp_path / "xdg"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
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
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_DIR", str(xdg))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    exit_code = main(["config", "init"])
    assert exit_code == 1


def test_config_init_force_overwrites(monkeypatch, tmp_path):
    """tpo config init --force overwrites existing file."""
    xdg = tmp_path / "xdg"
    (xdg / "tpo").mkdir(parents=True)
    (xdg / "tpo" / "config.yaml").write_text("existing")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
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


def test_config_get_initialized_default_from_file(monkeypatch, tmp_path, capsys):
    """Active default-valued entries are still attributed to the config file."""
    f = tmp_path / "config.yaml"
    f.write_text("projects_dir: ~/projects\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "get", "projects_dir"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "projects_dir:" in captured.out
    assert "from file" in captured.out


def test_config_get_pipeline_projects_dir_compat_alias(monkeypatch, tmp_path, capsys):
    """tpo config get reports the deprecated projects_dir env fallback."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "projects"))
    exit_code = main(["config", "get", "projects_dir"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert str(tmp_path / "projects") in captured.out
    assert "PIPELINE_PROJECTS_DIR" in captured.out


def test_config_get_env_override(monkeypatch, tmp_path, capsys):
    """tpo config get ignores PIPELINE_* env vars for config entries."""
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: '#config-alerts'\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#env-alerts")
    exit_code = main(["config", "get", "slack_channel"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "#config-alerts" in captured.out
    assert "from file" in captured.out
    assert "PIPELINE_SLACK_CHANNEL" not in captured.out


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
    """tpo config get falls back to defaults when the config file is broken."""
    f = tmp_path / "config.yaml"
    f.write_text("badkey: value\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#env-alerts")
    exit_code = main(["config", "get", "slack_channel"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "slack_channel:" in captured.out
    assert "#env-alerts" not in captured.out
    assert "from default" in captured.out


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


def test_config_init_emits_prompt_client(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert main(["config", "init"]) == 0
    assert "prompt_client: claude\n" in path.read_text()


def test_config_get_prompt_client_reports_default_source(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    assert main(["config", "get", "prompt_client"]) == 0
    output = capsys.readouterr().out
    assert "claude" in output
    assert "default" in output


def test_config_set_prompt_client_round_trips(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert main(["config", "set", "prompt_client", "codex"]) == 0
    assert load_global_config().prompt_client == "codex"
    assert main(["config", "get", "prompt_client"]) == 0
    output = capsys.readouterr().out
    assert "codex" in output
    assert str(path) in output


@pytest.mark.parametrize("value", ["Claude", "CODEX", "cursor", "null"])
def test_config_set_rejects_invalid_prompt_client(monkeypatch, tmp_path, value):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert main(["config", "set", "prompt_client", value]) == 2
    assert not path.exists()


def test_config_init_emits_agent_policy_mode(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert main(["config", "init"]) == 0
    assert "agent_policy_mode: inherit\n" in path.read_text()


def test_config_get_agent_policy_mode_default(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    assert main(["config", "get", "agent_policy_mode"]) == 0
    output = capsys.readouterr().out
    assert "inherit" in output
    assert "default" in output


@pytest.mark.parametrize("value", ["delegated", "inherit"])
def test_config_set_agent_policy_mode_round_trips(monkeypatch, tmp_path, capsys, value):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert main(["config", "set", "agent_policy_mode", value]) == 0
    assert load_global_config().agent_policy_mode == value
    assert main(["config", "get", "agent_policy_mode"]) == 0
    output = capsys.readouterr().out
    assert value in output
    assert str(path) in output


@pytest.mark.parametrize("value", ["Inherit", "DELEGATED", "auto", "null", ""])
def test_config_set_rejects_invalid_agent_policy_mode(monkeypatch, tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text("agent_policy_mode: inherit\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    assert main(["config", "set", "agent_policy_mode", value]) == 2
    assert path.read_text() == "agent_policy_mode: inherit\n"


@pytest.mark.parametrize("arguments", [[], ["--all"]])
@pytest.mark.parametrize("content", [None, "", "log_retention_days: 7\nslack_channel: '#file'\nprompt_client: codex\n"])
def test_config_get_all_ordered_values(monkeypatch, tmp_path, capsys, arguments, content):
    from dataclasses import fields
    from unittest.mock import Mock

    from hermes_pipeline import config_loader
    from hermes_pipeline.config import Config

    path = tmp_path / "config.yaml"
    if content is not None:
        path.write_text(content)
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    monkeypatch.delenv("PIPELINE_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#ignored")
    loader = Mock(wraps=config_loader.load_global_config_with_active_keys)
    monkeypatch.setattr(config_loader, "load_global_config_with_active_keys", loader)

    assert main(["config", "get", *arguments]) == 0
    captured = capsys.readouterr()
    overrides = {"log_retention_days": 7, "slack_channel": "#file", "prompt_client": "codex"} if content else {}
    expected = []
    for field in fields(Config):
        key = field.name
        source = f"file: {path}" if key in overrides else "default"
        value = overrides.get(key, getattr(Config.default(), key))
        expected.append(f"{key}: {value} (from {source})")
    assert captured.out.splitlines() == expected
    assert captured.err == ""
    loader.assert_called_once_with()
    # Every listed key must also be accepted by single-key reads, identically.
    for line in expected:
        assert main(["config", "get", line.split(":", 1)[0]]) == 0
        assert capsys.readouterr().out == line + "\n"
    assert path.read_text() == content if content is not None else not path.exists()


@pytest.mark.parametrize("arguments", [[], ["--all"], ["projects_dir"]])
@pytest.mark.parametrize("env_value", [None, "~/environment-projects", ""])
@pytest.mark.parametrize("content", [None, "projects_dir: ~/file-projects\n", "broken: [", "log_retention_days: invalid\n", "badkey: value\n"])
def test_config_get_projects_precedence_and_recovery(
    monkeypatch, tmp_path, capsys, arguments, env_value, content
):
    from pathlib import Path
    from unittest.mock import Mock

    from hermes_pipeline import config_loader
    from hermes_pipeline.config import Config

    path = tmp_path / "config.yaml"
    if content is not None:
        path.write_text(content)
    monkeypatch.setenv("TPO_CONFIG_FILE", str(path))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#ignored")
    if env_value is None:
        monkeypatch.delenv("PIPELINE_PROJECTS_DIR", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_PROJECTS_DIR", env_value)
    loader = Mock(wraps=config_loader.load_global_config_with_active_keys)
    monkeypatch.setattr(config_loader, "load_global_config_with_active_keys", loader)
    assert main(["config", "get", *arguments]) == 0
    captured = capsys.readouterr()
    loader.assert_called_once_with()
    if content and content.startswith("projects_dir:"):
        value, source = Path("~/file-projects").expanduser(), f"file: {path}"
    elif env_value is not None:
        value, source = Path(env_value).expanduser(), "env: PIPELINE_PROJECTS_DIR"
    else:
        value, source = Config.default().projects_dir, "default"
    assert f"projects_dir: {value} (from {source})\n" in captured.out
    broken = content is not None and not content.startswith("projects_dir:")
    assert captured.out.count("Warning: config file has errors:") == int(broken)
    assert captured.out.count("Falling back to defaults.") == int(broken)
    if not arguments or arguments == ["--all"]:
        assert "slack_channel:  (from default)\n" in captured.out
        assert "log_retention_days: 7 (from default)\n" in captured.out
    assert captured.err == ""
    assert path.read_text() == content if content is not None else not path.exists()


@pytest.mark.parametrize("arguments", [["nonexistent"], ["slack_channel", "--all"]])
def test_config_get_invalid_arguments_before_load(monkeypatch, capsys, arguments):
    from unittest.mock import Mock

    from hermes_pipeline import config_loader

    loader = Mock(side_effect=AssertionError("must validate before loading"))
    monkeypatch.setattr(config_loader, "load_global_config_with_active_keys", loader)
    assert main(["config", "get", *arguments]) == 2
    captured = capsys.readouterr()
    assert "Error:" in captured.err
    assert captured.out == ""
    loader.assert_not_called()


def test_config_get_help_documents_aggregate_forms(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["config", "get", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "[key]" in output
    assert "--all" in output
    assert "Omit key" in output
