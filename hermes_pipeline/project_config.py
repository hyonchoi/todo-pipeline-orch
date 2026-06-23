"""Per-project configuration for multi-project scanning.

Reads <project>/.hermes/project.toml for project-specific settings.
"""
from __future__ import annotations

import logging
from pathlib import Path

import tomllib

log = logging.getLogger(__name__)

PROJECT_TOML_PATH = ".hermes/project.toml"
DEFAULT_SLACK_CHANNEL = "#alert"


def _read_project_toml(project_dir: Path) -> dict | None:
    """Read and parse <project>/.hermes/project.toml."""
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
    """Check if a project is active (not archived). Default: True."""
    toml_data = _read_project_toml(project_dir)
    if toml_data is None:
        return True
    active = toml_data.get("active", {})
    return active.get("enabled", True)


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
    from .config import _validate_project_slug

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
