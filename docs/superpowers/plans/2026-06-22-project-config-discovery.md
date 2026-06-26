# Multi-project Project Config & Discovery

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `project.toml` parsing (`enabled`, `slack_channel`) and `_discover_projects()` so the scan loop can find active projects in `projects_dir`.

**Architecture:** Create `hermes_pipeline/project_config.py` with two public functions: `_is_enabled(project_dir)` reads `project.toml` and returns whether the project is active, and `_resolve_slack_channel(project_dir, env_channel)` returns the per-project Slack channel using a 3-level fallback chain. A thin `_discover_projects(config)` helper scans `config.projects_dir` for valid project directories with `TODOS.md` that are enabled.

**Tech Stack:** Python 3.12+, `uv`, `tomllib` (stdlib), no new dependencies.

## Global Constraints

- One TODO in flight per project (existing constraint)
- Hermes-only for LLM queries (TODO-6)
- Filesystem-based config — no database or network calls before selection
- Python 3.12+, `uv`-managed
- Per-project config at `<project>/.hermes/project.toml`
- Default (no file) is active — opt-out for archived projects

---

### Task 1: Create project_config module with _is_enabled

**Files:**
- Create: `hermes_pipeline/project_config.py`
- Test: `tests/test_project_config.py`

**Interfaces:**
- Consumes: Nothing from other tasks
- Produces: `_is_enabled(project_dir) -> bool`, `_read_project_toml(project_dir) -> dict | None`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path

from hermes_pipeline.project_config import _is_enabled, _read_project_toml


def test_is_enabled_default_true_when_no_file(tmp_path: Path):
    """Project is enabled by default when project.toml doesn't exist."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    assert _is_enabled(project_dir) is True


def test_is_enabled_default_true_when_no_active_section(tmp_path: Path):
    """Project is enabled when project.toml exists but has no [active] section."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("# just a comment\n")
    assert _is_enabled(project_dir) is True


def test_is_enabled_false(tmp_path: Path):
    """Project can be disabled via [active] enabled = false."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[active]\nenabled = false\n")
    assert _is_enabled(project_dir) is False


def test_is_enabled_explicit_true(tmp_path: Path):
    """Project is enabled when [active] enabled = true."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[active]\nenabled = true\n")
    assert _is_enabled(project_dir) is True


def test_read_project_toml_returns_none_when_missing(tmp_path: Path):
    """_read_project_toml returns None when file doesn't exist."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    result = _read_project_toml(project_dir)
    assert result is None


def test_read_project_toml_parses_sections(tmp_path: Path):
    """_read_project_toml parses project.toml into a dict."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[active]\nenabled = true\n\n[notifications]\nslack_channel = \"project__test\"\n")
    result = _read_project_toml(project_dir)
    assert result is not None
    assert result["active"]["enabled"] is True
    assert result["notifications"]["slack_channel"] == "project__test"


def test_is_enabled_returns_true_on_parse_error(tmp_path: Path):
    """If project.toml is malformed, treat as enabled (default)."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("this is not valid toml {{{")
    assert _is_enabled(project_dir) is True  # Default on error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_project_config.py -v`
Expected: FAIL with "cannot import name '_is_enabled' from 'hermes_pipeline.project_config'"

- [ ] **Step 3: Write minimal implementation**

```python
"""Per-project configuration for multi-project scanning.

Reads <project>/.hermes/project.toml for project-specific settings.
"""
from __future__ import annotations

import logging
from pathlib import Path

import tomllib

log = logging.getLogger(__name__)

# Sentinel file location relative to project root
PROJECT_TOML_PATH = ".hermes/project.toml"

# Default Slack channel if no override is found
DEFAULT_SLACK_CHANNEL = "#alert"


def _read_project_toml(project_dir: Path) -> dict | None:
    """Read and parse <project>/.hermes/project.toml.

    Args:
        project_dir: Project root directory.

    Returns:
        Parsed TOML as a dict, or None if file doesn't exist.
    """
    toml_path = project_dir / PROJECT_TOML_PATH
    if not toml_path.is_file():
        return None
    try:
        data = toml_path.read_bytes()
        return tomllib.loads(data.decode("utf-8"))
    except Exception as e:
        log.warning("failed to parse %s: %s — using defaults", toml_path, e)
        return None


def _is_enabled(project_dir: Path) -> bool:
    """Check if a project is active (not archived).

    Reads <project>/.hermes/project.toml. If the file doesn't exist or
    is malformed, defaults to True (project is active).

    Args:
        project_dir: Project root directory.

    Returns:
        True if project is active, False if explicitly disabled.
    """
    toml_data = _read_project_toml(project_dir)
    if toml_data is None:
        return True
    active = toml_data.get("active", {})
    return active.get("enabled", True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_project_config.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/project_config.py tests/test_project_config.py
git commit -m "feat: add project.toml parsing for per-project config

Add _is_enabled() and _read_project_toml() for reading
<project>/.hermes/project.toml. Default is active."
```

### Task 2: Add _resolve_slack_channel

**Files:**
- Modify: `hermes_pipeline/project_config.py`
- Test: `tests/test_project_config.py`

**Interfaces:**
- Consumes: `_read_project_toml()` from Task 1
- Produces: `_resolve_slack_channel(project_dir, env_channel) -> str`

Channel resolution (priority):
1. `project.toml`'s `slack_channel`
2. `PIPELINE_SLACK_CHANNEL` env var
3. `#alert` (hardcoded fallback)

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path

from hermes_pipeline.project_config import _resolve_slack_channel


def test_resolve_channel_project_toml_priority(tmp_path: Path):
    """project.toml slack_channel takes priority over env var."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[notifications]\nslack_channel = \"project__test\"\n")
    result = _resolve_slack_channel(project_dir, env_channel="env_channel")
    assert result == "project__test"


def test_resolve_channel_env_fallback(tmp_path: Path):
    """PIPELINE_SLACK_CHANNEL env var is used when project.toml has none."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    # No project.toml
    result = _resolve_slack_channel(project_dir, env_channel="env_channel")
    assert result == "env_channel"


def test_resolve_channel_default_fallback(tmp_path: Path):
    """#alert is the final fallback when no config source provides channel."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    # No project.toml, no env_channel
    result = _resolve_slack_channel(project_dir, env_channel="")
    assert result == "#alert"


def test_resolve_channel_empty_project_toml_channel_uses_env(tmp_path: Path):
    """Empty slack_channel in project.toml falls through to env var."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    project_toml = project_dir / ".hermes" / "project.toml"
    project_toml.parent.mkdir()
    project_toml.write_text("[notifications]\nslack_channel = \"\"\n")
    result = _resolve_slack_channel(project_dir, env_channel="env_channel")
    assert result == "env_channel"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_project_config.py::test_resolve_channel_project_toml_priority -v`
Expected: FAIL with "cannot import name '_resolve_slack_channel'"

- [ ] **Step 3: Add _resolve_slack_channel**

Add to `hermes_pipeline/project_config.py`:

```python
def _resolve_slack_channel(
    project_dir: Path,
    env_channel: str,
) -> str:
    """Resolve the Slack channel for a project.

    Priority:
      1. project.toml's [notifications] slack_channel
      2. PIPELINE_SLACK_CHANNEL env var (env_channel parameter)
      3. #alert (hardcoded fallback)

    Args:
        project_dir: Project root directory.
        env_channel: Value from PIPELINE_SLACK_CHANNEL env var.

    Returns:
        Slack channel string (e.g., "project__my-slug" or "#alert").
    """
    # Level 1: project.toml
    toml_data = _read_project_toml(project_dir)
    if toml_data is not None:
        notifications = toml_data.get("notifications", {})
        channel = notifications.get("slack_channel", "")
        if channel:
            return channel

    # Level 2: env var
    if env_channel:
        return env_channel

    # Level 3: hardcoded default
    return DEFAULT_SLACK_CHANNEL
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_project_config.py -v`
Expected: PASS (all 11 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/project_config.py tests/test_project_config.py
git commit -m "feat: add per-project Slack channel resolution

Add _resolve_slack_channel() with 3-level fallback:
project.toml > PIPELINE_SLACK_CHANNEL > #alert."
```

### Task 3: Add _discover_projects

**Files:**
- Modify: `hermes_pipeline/project_config.py`
- Test: `tests/test_project_config.py`

**Interfaces:**
- Consumes: `_is_enabled()` from Task 1, `_validate_project_slug()` from `cli.py`
- Produces: `_discover_projects(config) -> list[Path]`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path

from hermes_pipeline.config import Config
from hermes_pipeline.project_config import _discover_projects


def test_discover_projects_finds_active_projects(tmp_path: Path):
    """Should find projects with TODOS.md and enabled=true."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Active project with TODOS.md
    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")

    # Active project with TODOS.md
    p2 = projects_dir / "project-b"
    p2.mkdir()
    (p2 / "TODOS.md").write_text("# TODOS\n")

    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)

    assert len(result) == 2
    assert projects_dir / "project-a" in result
    assert projects_dir / "project-b" in result


def test_discover_projects_skips_disabled(tmp_path: Path):
    """Projects with enabled=false should be skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")

    p2 = projects_dir / "project-b"
    p2.mkdir()
    (p2 / "TODOS.md").write_text("# TODOS\n")
    p2_hermes = p2 / ".hermes"
    p2_hermes.mkdir()
    (p2_hermes / "project.toml").write_text("[active]\nenabled = false\n")

    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)

    assert len(result) == 1
    assert projects_dir / "project-a" in result
    assert projects_dir / "project-b" not in result


def test_discover_projects_skips_no_todos(tmp_path: Path):
    """Directories without TODOS.md are skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    p1 = projects_dir / "project-a"
    p1.mkdir()
    # No TODOS.md

    p2 = projects_dir / "project-b"
    p2.mkdir()
    (p2 / "TODOS.md").write_text("# TODOS\n")

    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)

    assert len(result) == 1
    assert projects_dir / "project-b" in result


def test_discover_projects_skips_invalid_slugs(tmp_path: Path):
    """Directories with invalid project slugs are skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Valid project
    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")

    # Invalid slug (starts with dash)
    p2 = projects_dir / "-invalid"
    p2.mkdir()
    (p2 / "TODOS.md").write_text("# TODOS\n")

    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)

    assert len(result) == 1
    assert projects_dir / "project-a" in result


def test_discover_projects_skips_files(tmp_path: Path):
    """Non-directory entries in projects_dir are skipped."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    (projects_dir / "README.md").write_text("readme\n")

    p1 = projects_dir / "project-a"
    p1.mkdir()
    (p1 / "TODOS.md").write_text("# TODOS\n")

    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)

    assert len(result) == 1


def test_discover_projects_sorted(tmp_path: Path):
    """Projects are returned in sorted order by directory name."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    for name in ["zebra", "alpha", "beta"]:
        p = projects_dir / name
        p.mkdir()
        (p / "TODOS.md").write_text("# TODOS\n")

    config = Config(projects_dir=projects_dir)
    result = _discover_projects(config)

    names = [p.name for p in result]
    assert names == ["alpha", "beta", "zebra"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_project_config.py::test_discover_projects_finds_active_projects -v`
Expected: FAIL with "cannot import name '_discover_projects'"

- [ ] **Step 3: Add _discover_projects**

Add to `hermes_pipeline/project_config.py`:

```python
def _discover_projects(config) -> list[Path]:
    """Scan projects_dir for active projects with TODOS.md.

    The project slug is the directory name (d.name). Directories that fail
    _validate_project_slug are skipped with a warning. Projects with
    enabled=false in project.toml are skipped (archived).

    Args:
        config: Config with projects_dir set.

    Returns:
        Sorted list of project directory paths.
    """
    from .cli import _validate_project_slug

    projects = []
    for d in sorted(config.projects_dir.iterdir()):
        if not d.is_dir():
            continue
        slug = d.name
        if not _validate_project_slug(slug):
            log.warning("skipping invalid project slug: %r", slug)
            continue
        if not (d / "TODOS.md").exists():
            continue
        if not _is_enabled(d):
            continue
        projects.append(d)
    return projects
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_project_config.py -v`
Expected: PASS (all 17 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/project_config.py tests/test_project_config.py
git commit -m "feat: add project discovery for scan loop

Add _discover_projects() that scans config.projects_dir for active
projects with TODOS.md, skipping invalid slugs and disabled projects."
```

### Task 4: Verify full test suite passes

**Files:**
- No code changes — verification only.

- [ ] **Step 1: Run affected test suites**

Run: `uv run pytest tests/test_project_config.py tests/test_config.py -v`
Expected: All PASS

- [ ] **Step 2: Commit (if any fixes needed from Step 1)**

```bash
git commit -m "fix: resolve test failures from project config"
```

---
