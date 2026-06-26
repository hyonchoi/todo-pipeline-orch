"""Per-project configuration for multi-project scanning.

Reads <project>/.hermes/project.toml for project-specific settings.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import tomllib

log = logging.getLogger(__name__)

PROJECT_TOML_PATH = ".hermes/project.toml"
DEFAULT_SLACK_CHANNEL = "#alert"

# A valid Slack channel: optional #/@ sigil, then an alphanumeric first
# character (no leading dash — that could inject a CLI flag into the
# `hermes chan message <channel> ...` argv), then alnum/dot/dash/underscore.
# \Z (not $) so a trailing newline can't sneak through.
_SLACK_CHANNEL_RE = re.compile(r'^[#@]?[A-Za-z0-9][A-Za-z0-9._-]*\Z')


def _is_valid_slack_channel(channel: str) -> bool:
    """Return True if *channel* is safe to pass as a CLI argument."""
    return bool(channel) and bool(_SLACK_CHANNEL_RE.match(channel))


def _read_project_toml(project_dir: Path) -> dict | None:
    """Read and parse <project>/.hermes/project.toml."""
    toml_path = project_dir / PROJECT_TOML_PATH
    if not toml_path.is_file():
        return None
    try:
        data = toml_path.read_bytes()
        return tomllib.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        log.error("failed to parse %s: %s — using defaults (project will be treated as enabled)", toml_path, e)
        return None


def _is_enabled(project_dir: Path, *, toml_data: dict | None = None) -> bool:
    """Check if a project is active (not archived). Default: True.

    Args:
        project_dir: Project root directory.
        toml_data: Pre-parsed project.toml data (optional — read from disk if not provided).
    """
    if toml_data is None:
        toml_data = _read_project_toml(project_dir)
    if toml_data is None:
        return True
    active = toml_data.get("active", {})
    return active.get("enabled", True)


def _resolve_slack_channel(
    project_dir: Path,
    env_channel: str,
    *,
    toml_data: dict | None = None,
) -> str:
    """Resolve the Slack channel for a project.

    Priority:
      1. project.toml's [notifications] slack_channel
      2. PIPELINE_SLACK_CHANNEL env var (env_channel parameter)
      3. #alert (hardcoded fallback)

    Args:
        project_dir: Project root directory.
        env_channel: Value from PIPELINE_SLACK_CHANNEL env var.
        toml_data: Pre-parsed project.toml data (optional — read from disk if not provided).

    Returns:
        Slack channel string (e.g., "project__my-slug" or "#alert").
    """
    # Level 1: project.toml
    if toml_data is None:
        toml_data = _read_project_toml(project_dir)
    if toml_data is not None:
        notifications = toml_data.get("notifications", {})
        channel = notifications.get("slack_channel", "")
        if channel:
            if _is_valid_slack_channel(channel):
                return channel
            log.warning(
                "ignoring invalid slack_channel %r in %s/project.toml — "
                "falling back to env/default",
                channel, project_dir.name,
            )

    # Level 2: env var
    if env_channel:
        if _is_valid_slack_channel(env_channel):
            return env_channel
        log.warning(
            "ignoring invalid PIPELINE_SLACK_CHANNEL %r — using default",
            env_channel,
        )

    # Level 3: hardcoded default
    return DEFAULT_SLACK_CHANNEL


def _discover_projects(config) -> list[tuple[Path, dict | None]]:
    """Scan projects_dir for active projects with TODOS.md.

    The project slug is the directory name (d.name). Directories that fail
    _validate_project_slug are skipped with a warning. Projects with
    enabled=false in project.toml are skipped (archived).

    Args:
        config: Config with projects_dir set.

    Returns:
        Sorted list of (project_dir, parsed_project_toml) tuples.
        The toml_data is None when the file is missing or unparseable.
    """
    from .config import _validate_project_slug

    if not config.projects_dir.is_dir():
        log.warning("projects_dir %s does not exist or is not a directory",
                     config.projects_dir)
        return []

    projects = []
    for d in sorted(config.projects_dir.iterdir()):
        # Skip symlinks before is_dir() (which follows them): a symlinked entry
        # could point outside projects_dir and let a tick operate on an
        # arbitrary path. Discovery only services real subdirectories.
        if d.is_symlink():
            log.warning("skipping symlinked project entry: %s", d.name)
            continue
        if not d.is_dir():
            continue
        slug = d.name
        if not _validate_project_slug(slug):
            log.warning("skipping invalid project slug: %r", slug)
            continue
        if not (d / "TODOS.md").exists():
            continue
        toml_data = _read_project_toml(d)
        if not _is_enabled(d, toml_data=toml_data):
            continue
        projects.append((d, toml_data))
    return projects
