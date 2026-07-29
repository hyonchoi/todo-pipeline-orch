# hermes_pipeline/config_loader.py
from __future__ import annotations

import dataclasses
import os
import typing
from pathlib import Path

import yaml

from .config import Config

# Resolve stringified annotations from `from __future__ import annotations`
_config_field_hints = typing.get_type_hints(Config)
_LEGACY_CONFIG_KEYS = {
    "lock_dir",
    "claude_cmd",
    "checkpoint_subdir",
    "ready_for_review_subdir",
    "counter_file_subpath",
    "default_timeout",
    "kanban_adapter",
    "kanban_outbox_cap",
}


# -- XDG search paths --


def _search_paths() -> list[Path]:
    if "TPO_CONFIG_FILE" in os.environ:
        return [Path(os.environ["TPO_CONFIG_FILE"])]
    xdg = os.environ.get("XDG_CONFIG_DIR", str(Path.home() / ".config"))
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    return [
        Path(xdg) / "tpo" / "config.yaml",
        Path.home() / ".tpo" / "config.yaml",
        Path(hermes_home) / "tpo.yaml",
    ]


def find_config_file() -> Path | None:
    for p in _search_paths():
        if p.is_file():
            return p
    return None


def default_config_path() -> Path:
    return _search_paths()[0]


# -- Type coercion and validation --


def _get_literal_values(target_type, key: str) -> set[str] | None:
    args = typing.get_args(target_type)
    if args:
        return {str(a) for a in args}
    return None


def _coerce_value(value, target_type, key: str, source: Path):
    if target_type is Path:
        if value is None:
            raise ValueError("YAML `null` is not a valid path value")
        return Path(str(value)).expanduser()
    elif target_type is int:
        return int(value)
    elif target_type is float:
        return float(value)
    elif target_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.lower() in ("true", "1", "yes"):
                return True
            if value.lower() in ("false", "0", "no"):
                return False
            raise ValueError(f"cannot coerce {value!r} to bool")
        return bool(value)
    elif target_type is str:
        if value is None:
            raise ValueError("YAML `null` is not a valid string value — use quoted string (e.g. \"null\")")
        return str(value)
    else:
        valid = _get_literal_values(target_type, key)
        if valid is not None:
            if value is None:
                raise ValueError(
                    f"YAML `null` is not valid; must be one of {sorted(valid)}"
                )
            str_val = str(value)
            if str_val not in valid:
                raise ValueError(
                    f"must be one of {sorted(valid)}, got {str_val!r}"
                )
            return str_val
        return value


def validate_config_key(key: str) -> str:
    fields = _config_field_hints
    if key not in fields:
        raise ValueError(f"unknown config key {key!r} — valid keys: {', '.join(sorted(fields.keys()))}")
    return key


def validate_config_value(value: str, key: str):
    field_type = _config_field_hints[key]
    return _coerce_value(value, field_type, key, Path("<cli>"))


# -- Config file loader --


def load_global_config() -> Config:
    config, _active_keys = load_global_config_with_active_keys()
    return config


def load_global_config_with_active_keys() -> tuple[Config, set[str]]:
    config_file = find_config_file()
    if config_file is None:
        return Config.default(), set()

    if config_file.is_symlink():
        raise ValueError(
            f"Config file {config_file} is a symlink — refused for security. "
            f"Use a regular file."
        )

    try:
        raw = yaml.safe_load(config_file.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error in {config_file}: {e}")

    if raw is None:
        return Config.default(), set()
    if not isinstance(raw, dict):
        raise ValueError(
            f"Config file {config_file} must contain a YAML mapping. "
            f"Run 'tpo config path' to locate and fix the file."
        )

    return _coerce_config(raw, config_file)


def _coerce_config(raw: dict, source: Path) -> tuple[Config, set[str]]:
    field_names = set(_config_field_hints.keys())
    overrides = {}
    errors = []

    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if key not in field_names:
            if key in _LEGACY_CONFIG_KEYS:
                continue
            errors.append(
                f"unknown config key {key!r} in {source} — "
                f"valid keys: {', '.join(sorted(field_names))}"
            )
            continue
        try:
            overrides[key] = _coerce_value(value, _config_field_hints[key], key, source)
        except (TypeError, ValueError) as e:
            errors.append(f"invalid value for {key!r} in {source}: {e}")

    if errors:
        raise ValueError(
            "\n".join(errors)
            + "\nRun 'tpo config path' to locate and fix the file."
        )

    if not overrides:
        return Config.default(), set()

    return dataclasses.replace(Config.default(), **overrides), set(overrides)


# -- YAML formatting --


def _format_value(value, key: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, str | Path):
        text = str(value)
        if (text.lower() in ("null", "true", "false", "yes", "no") or
                text == "" or text.startswith("~") or
                any(c in text for c in ":#{}[]%&*!|>'\"@`")):
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return text
    elif isinstance(value, int | float):
        return str(value)
    return str(value)


# -- Config template --

SKELETON = """\
# tpo global configuration
# Created by `tpo config init`
#
# Edit any field to override the built-in default.
# Run `tpo config get <key>` to see the effective value with source attribution.

projects_dir: ~/projects
state_dir: ~/.hermes
log_file_subpath: pipeline.log
log_retention_days: 7
slack_channel: ""
prompt_client: claude
"""
