"""Resolve profile execution authority without trusting worker environment input."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

from .agent_execution import ExecutionError, _no_symlinks, _safe_read


def account_home() -> Path:
    try:
        import pwd

        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError) as exc:
        raise ExecutionError("profile_authority_unconfirmed") from exc
    if not home.is_absolute():
        raise ExecutionError("profile_authority_unconfirmed")
    return home


def profile_root(project_dir: Path) -> Path:
    """Honor conventional account configuration, never HOME or config overrides.

    An environment-only custom root must be moved into conventional trusted
    configuration before dispatch. The caller may not silently choose a new root.
    """
    home = account_home()
    state = home / ".hermes"
    for source in (home / ".config/tpo/config.yaml", home / ".tpo/config.yaml", home / ".hermes/tpo.yaml"):
        if not source.exists() and not source.is_symlink():
            continue
        try:
            raw = yaml.safe_load(_safe_read(source))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise ExecutionError("profile_authority_config_invalid") from exc
        if raw is None:
            break
        if not isinstance(raw, dict):
            raise ExecutionError("profile_authority_config_invalid")
        value = raw.get("state_dir")
        if value is not None:
            if not isinstance(value, str) or not value:
                raise ExecutionError("profile_authority_config_invalid")
            state = home / value[2:] if value.startswith("~/") else Path(value)
            if not state.is_absolute():
                raise ExecutionError("profile_authority_config_invalid")
        elif "state_dir" in raw:
            raise ExecutionError("profile_authority_config_invalid")
        break
    root = state / "agent-executions" / hashlib.sha256(os.fsencode(project_dir.resolve())).hexdigest()
    _no_symlinks(root)
    return root
