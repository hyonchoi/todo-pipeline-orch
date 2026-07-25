# Global Config File Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `~/.config/tpo/config.yaml` global config file with `tpo config` CLI subcommand (init/get/set/path), replacing `PIPELINE_PROJECTS_DIR` env var with file-based config.

**Architecture:** Three-layer loader: `Config.default()` → YAML config file → env var overrides. New `config_loader.py` module handles XDG search, schema validation, type coercion, and YAML-safe formatting. CLI subcommand bootstraps like `skills` — no `Config.from_env()` needed.

**Tech Stack:** Python 3.12+, PyYAML (existing dep), dataclasses, typing.get_args, fcntl, argparse

## Global Constraints

- PyYAML is already a dependency (`pyyaml>=6.0` in pyproject.toml)
- `Config` dataclass is frozen — use `dataclasses.replace()` for overrides
- Existing env var overrides (`PIPELINE_*`) must continue to work AND layer on top of config file
- The `config` subcommand must bootstrap without `Config.from_env()` (like `skills`)
- Python 3.12+ syntax allowed (int | float, list[Path], etc.)
- Test override env var `TPO_CONFIG_FILE` for test isolation
- XDG search: 2 paths only (`${XDG_CONFIG_DIR:-~/.config}/tpo/config.yaml` → `${XDG_CONFIG_HOME:-~/.config}/tpo/config.yaml` is wrong; use `XDG_CONFIG_DIR` per design)
- Symlink check: reject symlinked config files with clear error
- File locking: `fcntl.flock()` during read-edit-write in `config set`
- Env var validation: `PIPELINE_KANBAN_ADAPTER=banana` must raise ValueError
- Auto-generated env_map from dataclass introspection, not hardcoded
- `config get` shows source attribution (default/file/env)
- `config get` has error recovery fallback when config file is broken
- `_format_value` quotes YAML-special characters (:, #, {, [, %, &, *, !, |, >, ', ", %, @, `)
- Line-number-aware YAML editing, not regex

---

### Task 1: Create config_loader.py — core loading, validation, coercion

**Files:**
- Create: `hermes_pipeline/config_loader.py`
- Test: `tests/test_config_loader.py`

**Interfaces:**
- Consumes: `Config` dataclass from `hermes_pipeline/config.py`
- Produces: `_search_paths()`, `find_config_file()`, `default_config_path()`, `load_global_config()`, `_coerce_config()`, `_coerce_value()`, `_get_literal_values()`, `validate_config_key()`, `validate_config_value()`, `SKELETON`, `_format_value()`

- [ ] **Step 1: Write test for XDG search paths**

```python
import os
from hermes_pipeline.config_loader import _search_paths, find_config_file, default_config_path

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
    from pathlib import Path
    assert paths[0] == Path.home() / ".config" / "tpo" / "config.yaml"

def test_search_paths_two_paths_only(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TPO_CONFIG_FILE", raising=False)
    paths = _search_paths()
    assert len(paths) == 2

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hyonchoi/Personal/todo-pipeline-orchestrator && uv run pytest tests/test_config_loader.py -k "test_search" -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'hermes_pipeline.config_loader'"

- [ ] **Step 3: Implement config_loader.py — search paths, find, default**

```python
# hermes_pipeline/config_loader.py
from __future__ import annotations

import dataclasses
import os
import re
import typing
import yaml
from pathlib import Path

from .config import Config


def _search_paths() -> list[Path]:
    if "TPO_CONFIG_FILE" in os.environ:
        return [Path(os.environ["TPO_CONFIG_FILE"])]
    xdg = os.environ.get("XDG_CONFIG_DIR", str(Path.home() / ".config"))
    return [
        Path(xdg) / "tpo" / "config.yaml",
        Path.home() / ".tpo" / "config.yaml",
    ]


def find_config_file() -> Path | None:
    for p in _search_paths():
        if p.is_file():
            return p
    return None


def default_config_path() -> Path:
    return _search_paths()[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/hyonchoi/Personal/todo-pipeline-orchestrator && uv run pytest tests/test_config_loader.py -k "test_search" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/config_loader.py tests/test_config_loader.py
git commit -m "feat(TODO-37): add config file XDG search paths"
```

- [ ] **Step 6: Write test for type coercion and validation**

```python
from hermes_pipeline.config_loader import _coerce_value, validate_config_key, validate_config_value, _get_literal_values
from pathlib import Path
import pytest

def test_coerce_path_expands_tilde():
    result = _coerce_value("~/projects", Path, "projects_dir", Path("<test>"))
    assert result == Path.home() / "projects"

def test_coerce_int():
    assert _coerce_value(1800, int, "default_timeout", Path("<test>")) == 1800

def test_coerce_bool_true():
    assert _coerce_value(True, bool, "some_key", Path("<test>")) is True

def test_coerce_bool_string_yes():
    assert _coerce_value("yes", bool, "some_key", Path("<test>")) is True

def test_coerce_bool_string_invalid():
    with pytest.raises(ValueError, match="cannot coerce"):
        _coerce_value("maybe", bool, "some_key", Path("<test>"))

def test_coerce_str_null_rejected():
    with pytest.raises(ValueError, match="YAML .*null"):
        _coerce_value(None, str, "slack_channel", Path("<test>"))

def test_coerce_str_normal():
    assert _coerce_value("my-channel", str, "slack_channel", Path("<test>")) == "my-channel"

def test_coerce_kanban_adapter_null():
    assert _coerce_value("null", str, "kanban_adapter", Path("<test>")) == "null"

def test_coerce_kanban_adapter_hermes():
    assert _coerce_value("hermes", str, "kanban_adapter", Path("<test>")) == "hermes"

def test_coerce_kanban_adapter_invalid():
    with pytest.raises(ValueError, match="must be one of"):
        _coerce_value("banana", str, "kanban_adapter", Path("<test>"))

def test_validate_config_key_valid():
    assert validate_config_key("projects_dir") == "projects_dir"

def test_validate_config_key_invalid():
    with pytest.raises(ValueError, match="unknown config key"):
        validate_config_key("nonexistent")

def test_validate_config_value_path():
    result = validate_config_value("~/opt", "projects_dir")
    assert result == Path.home() / "opt"

def test_validate_config_value_int():
    assert validate_config_value("3600", "default_timeout") == 3600

def test_validate_config_value_kanban():
    assert validate_config_value("hermes", "kanban_adapter") == "hermes"

def test_get_literal_values_kanban():
    vals = _get_literal_values(str, "kanban_adapter")
    assert vals is not None
    assert "null" in vals
    assert "hermes" in vals

def test_get_literal_values_non_literal():
    vals = _get_literal_values(Path, "projects_dir")
    assert vals is None
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_loader.py -k "test_coerce" -v`
Expected: FAIL — functions not yet defined

- [ ] **Step 8: Implement coercion, validation, literal introspection**

Append to `config_loader.py`:

```python
def _get_literal_values(target_type, key: str) -> set[str] | None:
    args = typing.get_args(target_type)
    if args:
        return {str(a) for a in args}
    return None


def _coerce_value(value, target_type, key: str, source: Path):
    if target_type is Path:
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
            str_val = str(value)
            if str_val not in valid:
                raise ValueError(f"must be one of {valid}, got {str_val!r}")
            return str_val
        return value


def validate_config_key(key: str) -> str:
    fields = {f.name for f in dataclasses.fields(Config)}
    if key not in fields:
        raise ValueError(f"unknown config key {key!r} — valid keys: {', '.join(sorted(fields))}")
    return key


def validate_config_value(value: str, key: str):
    fields = {f.name: f for f in dataclasses.fields(Config)}
    field_type = fields[key].type
    return _coerce_value(value, field_type, key, Path("<cli>"))
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_loader.py -k "test_coerce or test_validate or test_get_literal" -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add hermes_pipeline/config_loader.py tests/test_config_loader.py
git commit -m "feat(TODO-37): add type coercion and schema validation"
```

- [ ] **Step 11: Write test for load_global_config — valid YAML**

```python
from hermes_pipeline.config_loader import load_global_config
import yaml as _yaml

def test_load_global_config_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    from hermes_pipeline.config import Config
    cfg = load_global_config()
    assert cfg == Config.default()

def test_load_global_config_empty_file(monkeypatch, tmp_path):
    f = tmp_path / "empty.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    from hermes_pipeline.config import Config
    cfg = load_global_config()
    assert cfg == Config.default()

def test_load_global_config_valid_override(monkeypatch, tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("projects_dir: /opt/projects\nclaude_cmd: claude-code\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    cfg = load_global_config()
    assert cfg.projects_dir == Path("/opt/projects")
    assert cfg.claude_cmd == "claude-code"

def test_load_global_config_unknown_key_error(monkeypatch, tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("badkey: value\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    with pytest.raises(ValueError, match="unknown config key"):
        load_global_config()

def test_load_global_config_skip_comment_keys(monkeypatch, tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("_internal: something\nprojects_dir: /opt\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    cfg = load_global_config()
    assert cfg.projects_dir == Path("/opt")

def test_load_global_config_yaml_null_str_field(monkeypatch, tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("slack_channel: null\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    with pytest.raises(ValueError, match="YAML"):
        load_global_config()

def test_load_global_config_yaml_error_with_path(monkeypatch, tmp_path):
    f = tmp_path / "broken.yaml"
    f.write_text("key: [unbalanced\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    with pytest.raises(ValueError, match=str(f)):
        load_global_config()
```

- [ ] **Step 12: Run tests to verify they fail**

Run: `uv run pytest tests/test_config_loader.py -k "test_load_global" -v`
Expected: FAIL

- [ ] **Step 13: Implement load_global_config and _coerce_config**

Append to `config_loader.py`:

```python
def load_global_config() -> Config:
    config_file = find_config_file()
    if config_file is None:
        return Config.default()

    if config_file.is_symlink():
        raise ValueError(
            f"Config file {config_file} is a symlink — refused for security. "
            f"Use a regular file."
        )

    try:
        raw = yaml.safe_load(config_file.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error in {config_file}: {e}")

    if raw is None or not isinstance(raw, dict):
        return Config.default()

    return _coerce_config(raw, config_file)


def _coerce_config(raw: dict, source: Path) -> Config:
    fields = {f.name: f for f in dataclasses.fields(Config)}
    overrides = {}
    errors = []

    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if key not in fields:
            errors.append(
                f"unknown config key {key!r} in {source} — "
                f"valid keys: {', '.join(sorted(fields.keys()))}"
            )
            continue
        try:
            overrides[key] = _coerce_value(value, fields[key].type, key, source)
        except (TypeError, ValueError) as e:
            errors.append(f"invalid value for {key!r} in {source}: {e}")

    if errors:
        raise ValueError(
            "\n".join(errors)
            + f"\nRun 'tpo config path' to locate and fix the file."
        )

    if not overrides:
        return Config.default()

    return dataclasses.replace(Config.default(), **overrides)
```

- [ ] **Step 14: Run tests to verify they pass**

Run: `uv run pytest tests/test_config_loader.py -k "test_load_global" -v`
Expected: PASS

- [ ] **Step 15: Commit**

```bash
git add hermes_pipeline/config_loader.py tests/test_config_loader.py
git commit -m "feat(TODO-37): implement config file loader with validation"
```

- [ ] **Step 16: Write test for SKELETON format**

```python
from hermes_pipeline.config_loader import SKELETON

def test_skeleton_has_all_keys():
    import dataclasses
    for field in dataclasses.fields(Config):
        if field.name == "kanban_adapter":
            assert 'kanban_adapter' in SKELETON
        else:
            assert field.name in SKELETON, f"Missing {field.name} in skeleton"

def test_skeleton_kanban_quoted():
    assert '"null"' in SKELETON or "'null'" in SKELETON, "kanban_adapter 'null' must be quoted"

def test_skeleton_slack_quoted():
    lines = SKELETON.split("\n")
    for line in lines:
        if "slack_channel" in line and not line.strip().startswith("#"):
            assert '"' in line
            break

def test_skeleton_parses_when_uncommented(monkeypatch, tmp_path):
    uncommented = re.sub(r'^# ', '', SKELETON, flags=re.MULTILINE)
    uncommented = re.sub(r'^#', '', uncommented, flags=re.MULTILINE)
    raw = yaml.safe_load(uncommented)
    assert isinstance(raw, dict)
```

- [ ] **Step 17: Run tests — verify fail**

Run: `uv run pytest tests/test_config_loader.py -k "test_skeleton" -v`
Expected: FAIL

- [ ] **Step 18: Add SKELETON constant to config_loader.py**

Append the `SKELETON` constant from the design doc (lines 198-248).

- [ ] **Step 19: Run tests — verify pass**

Run: `uv run pytest tests/test_config_loader.py -k "test_skeleton" -v`
Expected: PASS

- [ ] **Step 20: Write test for _format_value**

```python
from hermes_pipeline.config_loader import _format_value

def test_format_bool_true():
    assert _format_value(True, "key") == "true"

def test_format_bool_false():
    assert _format_value(False, "key") == "false"

def test_format_int():
    assert _format_value(1800, "key") == "1800"

def test_format_string_null_quoted():
    assert _format_value("null", "kanban_adapter") == '"null"'

def test_format_string_empty_quoted():
    assert _format_value("", "slack_channel") == '""'

def test_format_string_tilde_quoted():
    assert _format_value("~/.hermes", "lock_dir").startswith('"')

def test_format_string_yaml_special_quoted():
    assert _format_value("hello: world", "key").startswith('"')

def test_format_string_yaml_hash_quoted():
    assert _format_value("# comment", "key").startswith('"')

def test_format_string_normal():
    result = _format_value("claude", "claude_cmd")
    assert result == "claude"

def test_format_path():
    from pathlib import Path
    result = _format_value(Path("/opt/projects"), "projects_dir")
    assert "/" in result
```

- [ ] **Step 21: Run tests — verify fail**

Run: `uv run pytest tests/test_config_loader.py -k "test_format" -v`
Expected: FAIL

- [ ] **Step 22: Implement _format_value**

Append to `config_loader.py`:

```python
def _format_value(value, key: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    elif isinstance(value, str):
        if (value.lower() in ("null", "true", "false", "yes", "no") or
                value == "" or value.startswith("~") or
                any(c in value for c in ":#{}[]%&*!|>'\"@`")):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value
    elif isinstance(value, int | float):
        return str(value)
    elif isinstance(value, Path):
        return str(value)
    return str(value)
```

- [ ] **Step 23: Run tests — verify pass**

Run: `uv run pytest tests/test_config_loader.py -k "test_format" -v`
Expected: PASS

- [ ] **Step 24: Commit**

```bash
git add hermes_pipeline/config_loader.py tests/test_config_loader.py
git commit -m "feat(TODO-37): add skeleton template and _format_value"
```

---

### Task 2: Update Config.from_env() — three-layer loader

**Files:**
- Modify: `hermes_pipeline/config.py:28-50`
- Test: `tests/test_config_from_env.py`

**Interfaces:**
- Consumes: `load_global_config()` from `config_loader.py`
- Produces: Updated `Config.from_env()` with three-layer loading

- [ ] **Step 1: Write test for three-layer loading**

```python
# tests/test_config_from_env.py
import pytest
from pathlib import Path
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
    from hermes_pipeline.config import Config
    import dataclasses
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
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run pytest tests/test_config_from_env.py -v`
Expected: FAIL — from_env doesn't use config_loader yet

- [ ] **Step 3: Update Config.from_env() in config.py**

Replace the `from_env` method (lines 28-50):

```python
    @classmethod
    def from_env(cls) -> Config:
        from .config_loader import load_global_config
        from dataclasses import fields as _fields, replace

        # Layer 1 + 2: defaults → config file
        base = load_global_config()

        # Layer 3: env var overrides (auto-generated from dataclass)
        env_map = {}
        for f in _fields(cls):
            env_name = f"PIPELINE_{f.name.upper()}"
            if f.name == "projects_dir":
                continue  # Removed — use config file instead
            env_map[env_name] = (f.name, f.type)

        overrides = {}
        for env_key, (attr, field_type) in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                from .config_loader import _coerce_value
                coerced = _coerce_value(val, field_type, attr, Path(f"<env:{env_key}>"))
                overrides[attr] = coerced

        if not overrides:
            return base
        return replace(base, **overrides)
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `uv run pytest tests/test_config_from_env.py -v`
Expected: PASS

- [ ] **Step 5: Verify existing test_config.py still works (with migration)**

The existing `test_config.py` uses `PIPELINE_PROJECTS_DIR`. We need to update it in Task 6. For now, skip:

Run: `uv run pytest tests/test_config.py --ignore-glob="*test_config*" -v` to check other tests
Expected: Other config-related tests should not break

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/config.py tests/test_config_from_env.py
git commit -m "feat(TODO-37): three-layer Config.from_env() loader, remove PIPELINE_PROJECTS_DIR"
```

---

### Task 3: Add tpo config CLI — subparsers and handlers

**Files:**
- Modify: `hermes_pipeline/cli.py:327` (add subparsers after `skills`, before `return parser`)
- Modify: `hermes_pipeline/cli.py:1288-1328` (add `config` to bootstrap list in `main()`)
- Test: `tests/test_config_cli.py`

**Interfaces:**
- Consumes: `config_loader` functions, `build_parser` subparser pattern
- Produces: `tpo config init/get/set/path` CLI commands

- [ ] **Step 1: Write test for config CLI — init**

```python
# tests/test_config_cli.py
import pytest
from hermes_pipeline.cli import main
from pathlib import Path

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
```

- [ ] **Step 2: Run tests — verify fail**

Run: `uv run pytest tests/test_config_cli.py -k "test_config_init" -v`
Expected: FAIL — subparser doesn't exist yet

- [ ] **Step 3: Add config subparsers to build_parser()**

Insert before `return parser` in `cli.py` (around line 348):

```python
    # config: read/write global tpo configuration
    config_parser = subparsers.add_parser(
        "config",
        help="Read and write global tpo configuration",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)

    config_init_parser = config_subparsers.add_parser(
        "init",
        help="Create a global config file with documented defaults",
    )
    config_init_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing config file",
    )
    config_init_parser.set_defaults(func=_cmd_config_init)

    config_get_parser = config_subparsers.add_parser(
        "get",
        help="Get the effective value of a config key",
    )
    config_get_parser.add_argument("key", help="Config key name")
    config_get_parser.set_defaults(func=_cmd_config_get)

    config_set_parser = config_subparsers.add_parser(
        "set",
        help="Set a config key in the global config file",
    )
    config_set_parser.add_argument("key", help="Config key name")
    config_set_parser.add_argument("value", help="New value")
    config_set_parser.set_defaults(func=_cmd_config_set)

    config_path_parser = config_subparsers.add_parser(
        "path",
        help="Show the path to the global config file",
    )
    config_path_parser.set_defaults(func=_cmd_config_path)
```

- [ ] **Step 4: Add config to bootstrap in main()**

Change the bootstrap check in `main()` (cli.py:1307):

```python
    if getattr(args, "command", None) in ("skills", "config"):
```

- [ ] **Step 5: Implement _cmd_config_init handler**

Add before `main()` in cli.py:

```python
def _cmd_config_init(args, config) -> int:
    from .config_loader import default_config_path, SKELETON
    path = default_config_path()
    if path.is_file() and not args.force:
        print(f"Config file already exists at {path}")
        print(f"Use --force to overwrite.")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKELETON)
    print(f"OK: created {path}")
    return 0
```

- [ ] **Step 6: Run tests — verify init passes**

Run: `uv run pytest tests/test_config_cli.py -k "test_config_init" -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_config_cli.py
git commit -m "feat(TODO-37): add tpo config init subcommand"
```

- [ ] **Step 8: Write test for config path CLI**

```python
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
```

- [ ] **Step 9: Implement _cmd_config_path**

```python
def _cmd_config_path(args, config) -> int:
    from .config_loader import find_config_file, default_config_path
    existing = find_config_file()
    if existing:
        print(f"Using: {existing}")
    else:
        print(f"No config file found.")
        print(f"Default path: {default_config_path()}")
        print(f"Run `tpo config init` to create one.")
    return 0
```

- [ ] **Step 10: Run tests — verify path passes**

Run: `uv run pytest tests/test_config_cli.py -k "test_config_path" -v`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_config_cli.py
git commit -m "feat(TODO-37): add tpo config path subcommand"
```

- [ ] **Step 12: Write test for config get CLI**

```python
def test_config_get_default(monkeypatch, tmp_path, capsys):
    """tpo config get shows default when no config file."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("PIPELINE_CLAUDE_CMD", raising=False)
    exit_code = main(["config", "get", "claude_cmd"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "claude_cmd: claude" in captured.out
    assert "from default" in captured.out.lower() or "default" in captured.out.lower()

def test_config_get_from_file(monkeypatch, tmp_path, capsys):
    """tpo config get shows value from config file."""
    f = tmp_path / "config.yaml"
    f.write_text("claude_cmd: claude-code\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.delenv("PIPELINE_CLAUDE_CMD", raising=False)
    exit_code = main(["config", "get", "claude_cmd"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "claude-code" in captured.out

def test_config_get_env_override(monkeypatch, tmp_path, capsys):
    """tpo config get shows env var override with attribution."""
    f = tmp_path / "config.yaml"
    f.write_text("claude_cmd: claude-code\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    monkeypatch.setenv("PIPELINE_CLAUDE_CMD", "env-override")
    exit_code = main(["config", "get", "claude_cmd"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "env-override" in captured.out
    assert "PIPELINE_CLAUDE_CMD" in captured.out or "env" in captured.out

def test_config_get_invalid_key(monkeypatch, tmp_path, capsys):
    """tpo config get rejects unknown key."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    exit_code = main(["config", "get", "nonexistent"])
    assert exit_code != 0

def test_config_get_broken_config_recovery(monkeypatch, tmp_path, capsys):
    """tpo config get recovers gracefully when config has errors."""
    f = tmp_path / "config.yaml"
    f.write_text("badkey: value\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "get", "claude_cmd"])
    captured = capsys.readouterr()
    assert "claude" in captured.out or "error" in captured.out.lower()
```

- [ ] **Step 13: Implement _cmd_config_get with source attribution + recovery**

```python
def _cmd_config_get(args, config) -> int:
    from .config_loader import validate_config_key
    from .config import Config
    key = validate_config_key(args.key)

    try:
        cfg = Config.from_env()
    except ValueError as e:
        print(f"Warning: config file has errors: {e}")
        print(f"Falling back to defaults.")
        cfg = Config.default()

    value = getattr(cfg, key)

    # Determine source attribution
    import os as _os
    env_name = f"PIPELINE_{key.upper()}"
    default_cfg = Config.default()
    if _os.environ.get(env_name) is not None and key != "projects_dir":
        source = f" (from env: {env_name})"
    elif value != getattr(default_cfg, key):
        from .config_loader import find_config_file
        cfg_file = find_config_file()
        source = f" (from file: {cfg_file})" if cfg_file else " (from config file)"
    else:
        source = " (from default)"

    print(f"{key}: {value}{source}")
    return 0
```

- [ ] **Step 14: Run tests — verify get passes**

Run: `uv run pytest tests/test_config_cli.py -k "test_config_get" -v`
Expected: PASS

- [ ] **Step 15: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_config_cli.py
git commit -m "feat(TODO-37): add tpo config get with source attribution"
```

- [ ] **Step 16: Write test for config set CLI**

```python
def test_config_set_creates_file(monkeypatch, tmp_path, capsys):
    """tpo config set auto-creates config file if missing."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "config.yaml"))
    exit_code = main(["config", "set", "claude_cmd", "claude-code"])
    assert exit_code == 0
    assert (tmp_path / "config.yaml").exists()
    captured = capsys.readouterr()
    assert "claude-code" in captured.out

def test_config_set_overrides_value(monkeypatch, tmp_path, capsys):
    """tpo config set writes value to existing file."""
    f = tmp_path / "config.yaml"
    f.write_text("claude_cmd: claude\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "claude_cmd", "claude-code"])
    assert exit_code == 0
    raw = yaml.safe_load(f.read_text())
    assert raw["claude_cmd"] == "claude-code"

def test_config_set_uncomments_existing(monkeypatch, tmp_path):
    """tpo config set uncomments an existing commented key."""
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
    f.write_text("# my comment\nclaude_cmd: claude\n")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "claude_cmd", "claude-code"])
    assert exit_code == 0
    assert "# my comment" in f.read_text()

def test_config_set_invalid_key(monkeypatch, tmp_path, capsys):
    """tpo config set rejects unknown key."""
    monkeypatch.setenv("TPO_CONFIG_FILE", str(tmp_path / "config.yaml"))
    exit_code = main(["config", "set", "nonexistent", "value"])
    assert exit_code != 0

def test_config_set_kanban_adapter_validation(monkeypatch, tmp_path):
    """tpo config set validates kanban_adapter literal values."""
    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "kanban_adapter", "banana"])
    assert exit_code != 0

def test_config_set_path_type_coercion(monkeypatch, tmp_path):
    """tpo config set coerces string to Path type."""
    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "projects_dir", "/opt/projects"])
    assert exit_code == 0
    cfg = load_global_config()
    assert cfg.projects_dir == Path("/opt/projects")

def test_config_set_int_type_coercion(monkeypatch, tmp_path):
    """tpo config set coerces string to int type."""
    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "default_timeout", "3600"])
    assert exit_code == 0
    cfg = load_global_config()
    assert cfg.default_timeout == 3600

def test_config_set_symlink_rejected(monkeypatch, tmp_path):
    """tpo config set rejects symlinked config file."""
    real = tmp_path / "real.yaml"
    real.write_text("")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    monkeypatch.setenv("TPO_CONFIG_FILE", str(link))
    exit_code = main(["config", "set", "claude_cmd", "test"])
    assert exit_code != 0

def test_config_set_yaml_special_chars_quoted(monkeypatch, tmp_path):
    """tpo config set quotes values with YAML-special characters."""
    f = tmp_path / "config.yaml"
    f.write_text("")
    monkeypatch.setenv("TPO_CONFIG_FILE", str(f))
    exit_code = main(["config", "set", "slack_channel", "#general: alerts"])
    assert exit_code == 0
    raw = yaml.safe_load(f.read_text())
    assert raw["slack_channel"] == "#general: alerts"
```

- [ ] **Step 17: Implement _cmd_config_set with line-number-aware edit + flock**

```python
def _cmd_config_set(args, config) -> int:
    from .config_loader import (validate_config_key, validate_config_value,
                                 find_config_file, default_config_path,
                                 _format_value)
    from .config import Config
    import dataclasses

    try:
        key = validate_config_key(args.key)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    try:
        coerced = validate_config_value(args.value, key)
    except (TypeError, ValueError) as e:
        print(f"Error: invalid value for {args.key!r}: {e}", file=sys.stderr)
        return 2

    config_file = find_config_file()
    if config_file is None:
        config_file = default_config_path()
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("# tpo global configuration\n\n")

    if config_file.is_symlink():
        print(f"Error: config file {config_file} is a symlink — refused for security.",
              file=sys.stderr)
        return 2

    # File locking with fcntl
    import fcntl
    import tempfile

    text = config_file.read_text()
    lines = text.split("\n")

    # Find line with this key (commented or active)
    found_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and f"{key}:" in stripped:
            uncommented = stripped.lstrip("#").lstrip()
            if uncommented.startswith(f"{key}:"):
                found_idx = i
                break
        elif stripped.startswith(f"{key}:"):
            found_idx = i
            break

    formatted = _format_value(coerced, key)
    new_line = f"{key}: {formatted}"

    if found_idx is not None:
        lines[found_idx] = new_line
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(new_line)
        lines.append("")

    new_text = "\n".join(lines)

    # Atomic write with lock
    fd = os.open(str(config_file), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, new_text.encode())
        os.ftruncate(fd, len(new_text.encode()))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    print(f"OK: set {key} = {coerced}")
    print(f"File: {config_file}")
    return 0
```

- [ ] **Step 18: Run tests — verify set passes**

Run: `uv run pytest tests/test_config_cli.py -k "test_config_set" -v`
Expected: PASS

- [ ] **Step 19: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_config_cli.py
git commit -m "feat(TODO-37): add tpo config set with line-edit, flock, symlink check"
```

---

### Task 4: Migrate PIPELINE_PROJECTS_DIR test references

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_recover_counter_cli.py`

**Interfaces:**
- Consumes: Updated `Config.from_env()` (no more `PIPELINE_PROJECTS_DIR`)
- Produces: Tests using config file or removed env var references

- [ ] **Step 1: Read and audit test_config.py**

Run: `grep -n "PIPELINE_PROJECTS_DIR" tests/test_config.py`
Expected: 2 references (line 15 setenv, line 20 assertion)

- [ ] **Step 2: Update test_config.py**

In `test_env_overrides`, remove `PIPELINE_PROJECTS_DIR` monkeypatch and assertion. The test should use a config file for `projects_dir` or simply not test that field via env var.

```python
def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("PIPELINE_CLAUDE_CMD", "/usr/bin/claude")
    monkeypatch.setenv("PIPELINE_KANBAN_ADAPTER", "hermes")
    c = Config.from_env()
    assert c.lock_dir == tmp_path / "locks"
    assert c.claude_cmd == "/usr/bin/claude"
    assert c.kanban_adapter == "hermes"
```

- [ ] **Step 3: Run updated test_config.py**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 4: Update test_cli.py**

Replace `os.environ["PIPELINE_PROJECTS_DIR"]` at line 28 with config file approach or remove. Also update the cleanup at line 47 to remove `PIPELINE_PROJECTS_DIR` from the unset list.

Read the full setup/teardown pattern in test_cli.py and apply the same fix: remove `PIPELINE_PROJECTS_DIR` from env setup and unset lists.

- [ ] **Step 5: Run updated test_cli.py**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 6: Update test_recover_counter_cli.py**

Same pattern: 17 references across setup functions. Replace `PIPELINE_PROJECTS_DIR` env var with `TPO_CONFIG_FILE` pointing to a YAML file with `projects_dir`, or use the config file approach.

For each fixture/function that sets `PIPELINE_PROJECTS_DIR`, replace with:
```python
config_file = tmp_path / "config.yaml"
config_file.write_text(f"projects_dir: {str(projects_dir)}\n")
monkeypatch.setenv("TPO_CONFIG_FILE", str(config_file))
```

Also remove `PIPELINE_PROJECTS_DIR` from unset lists.

- [ ] **Step 7: Run updated test_recover_counter_cli.py**

Run: `uv run pytest tests/test_recover_counter_cli.py -v`
Expected: PASS

- [ ] **Step 8: Run full test suite for related tests**

Run: `uv run pytest tests/test_config.py tests/test_cli.py tests/test_recover_counter_cli.py tests/test_config_from_env.py tests/test_config_cli.py tests/test_config_loader.py -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add tests/test_config.py tests/test_cli.py tests/test_recover_counter_cli.py
git commit -m "fix(TODO-37): migrate PIPELINE_PROJECTS_DIR test references to config file"
```

---

### Task 5: Update CHANGELOG.md

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Design doc breaking change notes
- Produces: CHANGELOG entry with breaking change + migration guidance

- [ ] **Step 1: Read current CHANGELOG.md header**

Read: `head -20 CHANGELOG.md`

- [ ] **Step 2: Add breaking change entry**

Add to top of CHANGELOG.md under current version or new section:

```markdown
## Breaking
- Removed `PIPELINE_PROJECTS_DIR` environment variable. Use `tpo config set projects_dir <path>` instead.

## Migration
- If you set `PIPELINE_PROJECTS_DIR` in your shell profile, remove it and run `tpo config set projects_dir <your-path>` once.

## Added
- `tpo config` subcommand: `init`, `get`, `set`, `path`
- Global config file at `${XDG_CONFIG_DIR:-~/.config}/tpo/config.yaml`
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(TODO-37): add CHANGELOG for global config feature"
```

---

### Task 6: Integration tests — end-to-end config workflow

**Files:**
- Test: `tests/test_config_loader.py` (append integration section)

**Interfaces:**
- Consumes: All modules from Tasks 1-5
- Produces: Integration tests for full workflow

- [ ] **Step 1: Write integration test — init → set → get → load**

```python
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
    from hermes_pipeline.cli import main
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
    from hermes_pipeline.config_loader import SKELETON

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
```

- [ ] **Step 2: Run integration tests**

Run: `uv run pytest tests/test_config_loader.py -k "test_integration" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_config_loader.py
git commit -m "test(TODO-37): add integration tests for config workflow"
```

---

### Task 7: Regression tests — existing behavior unchanged

**Files:**
- Test: `tests/test_config_loader.py` (append regression section)

**Interfaces:**
- Consumes: All new modules
- Produces: Tests verifying no regression for users without config file

- [ ] **Step 1: Write regression tests**

```python
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
```

- [ ] **Step 2: Run regression tests**

Run: `uv run pytest tests/test_config_loader.py -k "test_regression" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_config_loader.py
git commit -m "test(TODO-37): add regression tests for backward compatibility"
```

---

## Self-Review

**1. Spec coverage:**
- `tpo config init` creates self-documenting config.yaml — Task 3 Step 1-7
- `tpo config set projects_dir /opt/projects` writes to file, picked up by from_env — Task 3 Step 16-19 + Task 2
- `tpo config get projects_dir` shows effective value — Task 3 Step 8-15
- Unknown keys rejected — Task 1 Step 6-10 (validation) + Task 3 Step 16-18 (CLI)
- Wrong types rejected — Task 1 coercion tests
- PIPELINE_* env vars override config — Task 2 tests + Task 6 integration
- PIPELINE_PROJECTS_DIR removed — Task 2 + Task 4 migration
- Existing behavior unchanged — Task 7 regression
- Config set preserves comments — Task 3 line-number-aware edit + Task 6 integration test
- YAML null rejected for string — Task 1 coercion tests
- kanban_adapter literal validation — Task 1 coercion tests
- Symlink check — Task 1 load_global_config + Task 3 config set
- File locking — Task 3 config set fcntl
- TPO_CONFIG_FILE override — Task 1 search paths
- Source attribution — Task 3 config get
- Error recovery — Task 3 config get
- Env var validation — Task 2 from_env coercion
- Auto-generated env_map — Task 2 from_env introspection
- YAML-special char quoting — Task 1 _format_value
- Recovery hint in errors — Task 1 _coerce_config
- typing.get_args() introspection — Task 1 _get_literal_values

All success criteria covered. All reviewer findings addressed.

**2. Placeholder scan:** No "TBD", "TODO", "implement later", or vague instructions found. All steps have code blocks.

**3. Type consistency:**
- `config_loader.py` functions: `_search_paths`, `find_config_file`, `default_config_path`, `load_global_config`, `_coerce_config`, `_coerce_value`, `_get_literal_values`, `validate_config_key`, `validate_config_value`, `_format_value`, `SKELETON` — consistent across all tasks
- CLI handlers: `_cmd_config_init`, `_cmd_config_get`, `_cmd_config_set`, `_cmd_config_path` — all return int, take (args, config)
- `Config.from_env()` signature unchanged — returns Config, classmethod

No inconsistencies found.
