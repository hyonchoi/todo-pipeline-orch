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
