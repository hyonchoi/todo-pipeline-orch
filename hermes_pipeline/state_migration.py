"""Per-project state migration from global ~/.hermes/ to <project>/.hermes/.

One-time migration that moves state files (current_tick_id.txt, circuit.json,
outcomes/) from the global state directory into each project's own .hermes/
directory. No-op if files are already absent or already migrated.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

_STATE_FILES: list[str] = ["current_tick_id.txt", "circuit.json"]
_OUTCOMES_DIR = "outcomes"


def _get_project_state_dir(project_dir: Path) -> Path:
    """Return the per-project .hermes directory for *project_dir*."""
    return project_dir / ".hermes"


def _migrate_global_state(project_dir: Path, config: Config) -> None:
    """Move state files from the global state dir to the per-project dir.

    Files are only moved when the source exists in *config.state_dir* and the
    destination does not already exist in the per-project directory.
    """
    dst = _get_project_state_dir(project_dir)
    needs_create = False

    for filename in _STATE_FILES:
        src = config.state_dir / filename
        dest = dst / filename
        if src.is_file() and not dest.exists():
            needs_create = True
            break

    outcomes_src = config.state_dir / _OUTCOMES_DIR
    outcomes_dst = dst / _OUTCOMES_DIR
    if outcomes_src.is_dir() and not outcomes_dst.exists():
        needs_create = True

    if not needs_create:
        return

    dst.mkdir(exist_ok=True)

    for filename in _STATE_FILES:
        src = config.state_dir / filename
        dest = dst / filename
        if src.is_file() and not dest.exists():
            log.info("Migrating %s -> %s", src, dest)
            shutil.move(str(src), str(dest))

    outcomes_src = config.state_dir / _OUTCOMES_DIR
    outcomes_dst = dst / _OUTCOMES_DIR
    if outcomes_src.is_dir() and not outcomes_dst.exists():
        log.info("Migrating %s -> %s", outcomes_src, outcomes_dst)
        shutil.move(str(outcomes_src), str(outcomes_dst))
