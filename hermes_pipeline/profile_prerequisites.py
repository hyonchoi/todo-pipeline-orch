"""Shared validation helpers for phase-profile prerequisites."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from .config import PromptClient
from .phases import ProfilePrerequisites

HERMES_SKILL_REGISTRY_ROOT = "Hermes skill registry"


def unverified_prerequisite_ids(
    prerequisites: ProfilePrerequisites, prompt_client: PromptClient
) -> list[str]:
    """Return prerequisite IDs unsupported for the selected client."""
    unverified: list[str] = []
    for prerequisite in prerequisites.skills:
        if prerequisite.support == "Unverified":
            prerequisite.clients[prompt_client]
            unverified.append(prerequisite.skill_id)
    return unverified


def verify_hermes_skill_registry_prerequisite(
    *,
    assignee: str,
    skill_id: str,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[bool, str]:
    """Verify one enabled Hermes-registry skill with a bounded read-only call."""
    cmd = ["hermes"]
    if assignee != "default":
        cmd.extend(["-p", assignee])
    cmd.extend(["skills", "list", "--enabled-only"])
    try:
        result = runner(
            cmd,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return False, "Hermes is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return False, f"`{' '.join(cmd)}` timed out."

    if result.returncode != 0:
        return False, f"`{' '.join(cmd)}` failed (rc={result.returncode})."
    enabled_skill_names: set[str] = set()
    for line in (result.stdout or "").splitlines():
        columns = line.split()
        if len(columns) == 1 or (columns and columns[-1] == "enabled"):
            enabled_skill_names.add(columns[0])
    if skill_id not in enabled_skill_names:
        return (
            False,
            f"skill '{skill_id}' is not enabled in Hermes profile '{assignee}'.",
        )
    return True, ""
