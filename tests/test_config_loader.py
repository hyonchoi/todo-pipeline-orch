from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pipeline.config import Config
from hermes_pipeline.config_loader import (
    SKELETON,
    _coerce_value,
    _format_value,
    _get_literal_values,
    _search_paths,
    default_config_path,
    find_config_file,
    load_global_config,
    validate_config_key,
    validate_config_value,
)

# ============================================================
# XDG search path tests
# ============================================================


def test_search_paths_uses_xdg(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_DIR", str(xdg))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    paths = _search_paths()
    assert paths[0] == xdg / "tpo" / "config.yaml"


def test_search_paths_default(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    paths = _search_paths()
    assert paths[0] == Path.home() / ".config" / "tpo" / "config.yaml"


def test_search_paths_includes_legacy_hermes_fallback(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    paths = _search_paths()
    assert paths == [
        Path.home() / ".config" / "tpo" / "config.yaml",
        Path.home() / ".tpo" / "config.yaml",
        Path.home() / ".hermes" / "tpo.yaml",
    ]


def test_search_paths_uses_hermes_home_for_legacy_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    paths = _search_paths()
    assert paths[2] == tmp_path / "hermes" / "tpo.yaml"


def test_tpo_config_file_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.yaml"
    monkeypatch.setenv("TPO_CONFIG_FILE", str(custom))
    paths = _search_paths()
    assert paths[0] == custom
    assert len(paths) == 1


def test_find_config_file_returns_first_existing(monkeypatch, tmp_path):
    custom = tmp_path / "custom.yaml"
    custom.write_text("projects_dir: /opt")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(custom))
    result = find_config_file()
    assert result == custom


def test_find_config_file_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "nope.yaml"))
    assert find_config_file() is None


def test_default_config_path_returns_first_path(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    assert default_config_path() == Path.home() / ".config" / "tpo" / "config.yaml"


# ============================================================
# _get_literal_values tests
# ============================================================


class FakeField:
    def __init__(self, args):
        self.args = args


def test_get_literal_values_returns_set():
    result = _get_literal_values("Literal['null', 'hermes']", "kanban_adapter")
    # typing.get_args on a string returns empty, so None
    # The real caller passes actual types; this just exercises the path
    assert result is None  # string has no get_args


def test_get_literal_values_no_args():
    result = _get_literal_values(str, "slack_channel")
    assert result is None


# ============================================================
# _coerce_value tests
# ============================================================


def test_coerce_path_expands_tilde():
    result = _coerce_value("~/mydir", Path, "projects_dir", Path("<test>"))
    assert result == Path.home() / "mydir"


def test_coerce_path_plain_string():
    result = _coerce_value("/opt/data", Path, "projects_dir", Path("<test>"))
    assert result == Path("/opt/data")


def test_coerce_int_from_string():
    assert _coerce_value("42", int, "default_timeout", Path("<test>")) == 42


def test_coerce_int_from_int():
    assert _coerce_value(1800, int, "default_timeout", Path("<test>")) == 1800


def test_coerce_float():
    assert _coerce_value("3.14", float, "some_float", Path("<test>")) == 3.14


def test_coerce_bool_true_variants():
    for v in [True, "true", "True", "1", "yes", "Yes"]:
        assert _coerce_value(v, bool, "k", Path("<t>")) is True


def test_coerce_bool_false_variants():
    for v in [False, "false", "False", "0", "no", "No"]:
        assert _coerce_value(v, bool, "k", Path("<t>")) is False


def test_coerce_bool_invalid_string():
    with pytest.raises(ValueError, match="cannot coerce"):
        _coerce_value("maybe", bool, "k", Path("<t>"))


def test_coerce_string_value():
    assert _coerce_value("hello", str, "slack_channel", Path("<t>")) == "hello"


def test_coerce_null_to_string_raises():
    with pytest.raises(ValueError, match="null"):
        _coerce_value(None, str, "slack_channel", Path("<t>"))


def test_coerce_literal_valid():
    class _Test:
        pass
    # Simulate Literal["null", "hermes"] by passing the type directly
    # _get_literal_values won't find args on this mock, so falls through
    # Let's test the Literal path properly:
    import typing
    KanbanAdapterName = typing.Literal["null", "hermes"]
    field_type = KanbanAdapterName
    # _coerce_value's else branch handles Literals
    result = _coerce_value("hermes", field_type, "kanban_adapter", Path("<t>"))
    assert result == "hermes"


def test_coerce_literal_null_quoted():
    import typing
    KanbanAdapterName = typing.Literal["null", "hermes"]
    result = _coerce_value("null", KanbanAdapterName, "kanban_adapter", Path("<t>"))
    assert result == "null"


def test_coerce_literal_invalid():
    import typing
    KanbanAdapterName = typing.Literal["null", "hermes"]
    with pytest.raises(ValueError, match="must be one of"):
        _coerce_value("jira", KanbanAdapterName, "kanban_adapter", Path("<t>"))


# ============================================================
# validate_config_key tests
# ============================================================


def test_validate_config_key_valid():
    assert validate_config_key("projects_dir") == "projects_dir"


def test_validate_config_key_invalid():
    with pytest.raises(ValueError, match="unknown config key"):
        validate_config_key("nonexistent_field")


# ============================================================
# validate_config_value tests
# ============================================================


def test_validate_config_value_path():
    result = validate_config_value("/opt/projects", "projects_dir")
    assert result == Path("/opt/projects")


def test_validate_config_value_int():
    assert validate_config_value("3600", "default_timeout") == 3600


def test_validate_config_value_string():
    assert validate_config_value("#general", "slack_channel") == "#general"


def test_validate_config_value_kanban_adapter():
    result = validate_config_value("hermes", "kanban_adapter")
    assert result == "hermes"


def test_validate_config_value_kanban_null():
    result = validate_config_value("null", "kanban_adapter")
    assert result == "null"


# ============================================================
# load_global_config tests
# ============================================================


def test_load_global_config_no_file_returns_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    config = load_global_config()
    assert config == Config.default()


def test_load_global_config_empty_file_returns_default(monkeypatch, tmp_path):
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    config = load_global_config()
    assert config == Config.default()


def test_load_global_config_null_yaml_returns_default(monkeypatch, tmp_path):
    cfg = tmp_path / "null.yaml"
    cfg.write_text("null")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    config = load_global_config()
    assert config == Config.default()


def test_load_global_config_valid_override(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("projects_dir: /opt/myprojects\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    config = load_global_config()
    assert config.projects_dir == Path("/opt/myprojects")


def test_load_global_config_multiple_overrides(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "projects_dir: /opt/projects\n"
        "default_timeout: 3600\n"
        "slack_channel: '#deployments'\n"
    )
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    config = load_global_config()
    assert config.projects_dir == Path("/opt/projects")
    assert config.default_timeout == 3600
    assert config.slack_channel == "#deployments"


def test_load_global_config_unknown_key_raises(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("projects_dir: /opt\nbad_key: value\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    with pytest.raises(ValueError, match="unknown config key 'bad_key'"):
        load_global_config()


def test_load_global_config_underscore_key_skipped(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("_internal: something\nprojects_dir: /opt\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    config = load_global_config()
    assert config.projects_dir == Path("/opt")


def test_load_global_config_symlink_refused(monkeypatch, tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text("projects_dir: /opt")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    monkeypatch.setenv("TPO_CONFIG_FILE", str(link))
    with pytest.raises(ValueError, match="symlink"):
        load_global_config()


def test_load_global_config_yaml_parse_error(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{invalid: [yaml: content")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    with pytest.raises(ValueError, match="YAML parse error"):
        load_global_config()


def test_load_global_config_invalid_int(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("default_timeout: not_a_number\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    with pytest.raises(ValueError, match="invalid value for 'default_timeout'"):
        load_global_config()


# ============================================================
# _coerce_config tests (via load_global_config)
# ============================================================


def test_coerce_config_no_valid_keys_returns_default(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("_only_comments: true\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(cfg))
    config = load_global_config()
    assert config == Config.default()


# ============================================================
# SKELETON tests
# ============================================================


def test_skeleton_has_header():
    assert "# tpo global configuration" in SKELETON


def test_skeleton_all_fields_commented():
    for field in dataclasses.fields(Config):
        assert field.name in SKELETON


import dataclasses


def test_skeleton_no_uncommented_fields():
    for line in SKELETON.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            pytest.fail(f"SKELETON should have no uncommented lines, got: {stripped}")


# ============================================================
# _format_value tests
# ============================================================


def test_format_bool_true():
    assert _format_value(True, "k") == "true"


def test_format_bool_false():
    assert _format_value(False, "k") == "false"


def test_format_simple_string():
    assert _format_value("hello", "k") == "hello"


def test_format_null_string_quoted():
    result = _format_value("null", "k")
    assert result == '"null"'


def test_format_empty_string_quoted():
    result = _format_value("", "k")
    assert '"' in result


def test_format_string_with_special_chars():
    result = _format_value("# comment", "k")
    assert '"' in result


def test_format_hash_channel_quoted():
    result = _format_value("#general", "k")
    assert '"' in result


def test_format_int():
    assert _format_value(42, "k") == "42"


def test_format_float():
    assert _format_value(3.14, "k") == "3.14"


def test_format_path():
    assert _format_value(Path("/opt/data"), "k") == "/opt/data"


# ============================================================
# Integration tests — end-to-end config workflow
# ============================================================

def test_integration_init_set_get_load(monkeypatch, tmp_path):
    """Full workflow: init creates file, set modifies, get reads, from_env loads."""
    from hermes_pipeline.cli import main
    from hermes_pipeline.config import Config
    from hermes_pipeline.config_loader import default_config_path

    monkeypatch.setenv("XDG_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    monkeypatch.delenv("PIPELINE_CLAUDE_CMD", raising=False)

    # Init
    assert main(["config", "init"]) == 0
    assert default_config_path().exists()

    # Set
    assert main(["config", "set", "claude_cmd", "claude-code"]) == 0

    # Get through Config.from_env()
    cfg = Config.from_env()
    assert cfg.claude_cmd == "claude-code"

    # Set path type
    assert main(["config", "set", "projects_dir", "/opt/projects"]) == 0
    cfg2 = Config.from_env()
    assert cfg2.projects_dir == Path("/opt/projects")

    # Set int type
    assert main(["config", "set", "default_timeout", "3600"]) == 0
    cfg3 = Config.from_env()
    assert cfg3.default_timeout == 3600


def test_integration_env_overrides_config_file(monkeypatch, tmp_path):
    """Env var layers on top of config file."""
    from hermes_pipeline.config import Config

    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    f = tmp_path / "config.yaml"
    f.write_text("claude_cmd: claude-code\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))

    # Without env override
    cfg = Config.from_env()
    assert cfg.claude_cmd == "claude-code"

    # With env override
    monkeypatch.setenv("PIPELINE_CLAUDE_CMD", "env-wins")
    cfg2 = Config.from_env()
    assert cfg2.claude_cmd == "env-wins"


def test_integration_config_set_preserves_skeleton(monkeypatch, tmp_path):
    """Config set preserves skeleton structure and comments."""
    from hermes_pipeline.cli import main

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_DIR", str(xdg))
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)

    main(["config", "init"])
    f = xdg / "tpo" / "config.yaml"

    # Set one value
    main(["config", "set", "claude_cmd", "claude-code"])

    content = f.read_text()
    # Original comments should be preserved
    assert "global configuration" in content
    # The set value should be active
    assert "claude_cmd: claude-code" in content


# ============================================================
# Regression tests — backward compatibility
# ============================================================

def test_regression_no_config_file_unchanged(monkeypatch):
    """Users without config file see same defaults as before."""
    monkeypatch.setenv("TPO_CONFIG_FILE", "/nonexistent")
    monkeypatch.delenv("PIPELINE_LOCK_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_STATE_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_CLAUDE_CMD", raising=False)
    monkeypatch.delenv("PIPELINE_KANBAN_ADAPTER", raising=False)
    monkeypatch.delenv("PIPELINE_SLACK_CHANNEL", raising=False)
    from hermes_pipeline.config import Config
    cfg = Config.from_env()
    default = Config.default()
    assert cfg.lock_dir == default.lock_dir
    assert cfg.projects_dir == default.projects_dir
    assert cfg.claude_cmd == default.claude_cmd
    assert cfg.kanban_adapter == default.kanban_adapter

def test_regression_env_vars_still_work(monkeypatch, tmp_path):
    """Remaining PIPELINE_* env vars still work without config file."""
    monkeypatch.setenv("TPO_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("PIPELINE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PIPELINE_CLAUDE_CMD", "custom-claude")
    from hermes_pipeline.config import Config
    cfg = Config.from_env()
    assert cfg.lock_dir == tmp_path / "locks"
    assert cfg.state_dir == tmp_path / "state"
    assert cfg.claude_cmd == "custom-claude"

def test_regression_frozen_config_unchanged():
    """Config dataclass is still frozen."""
    from hermes_pipeline.config import Config
    cfg = Config.default()
    with pytest.raises(Exception):  # FrozenInstanceError
        cfg.projects_dir = Path("/changed")
