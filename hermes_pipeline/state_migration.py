"""Per-project state migration from global ~/.hermes/ to <project>/.hermes/.

One-time migration that copies state files (current_tick_id.txt, circuit.json,
outcomes/) from the global state directory into a project's own .hermes/
directory only when that per-project state directory does not already exist.
Copies (not moves) so the global state remains available for manual recovery.

Uses atomic temp+rename: a partially-written state directory must not be
visible to the next tick, which could interpret an incomplete circuit.json
or a missing current_tick_id.txt as "in-flight" and skip the project.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

_STATE_FILES: list[str] = ["current_tick_id.txt", "circuit.json"]
_OUTCOMES_DIR = "outcomes"


def _get_project_state_dir(project_dir: Path) -> Path:
    """Return the per-project .hermes directory for *project_dir*."""
    return project_dir / ".hermes"


def needs_migration(config: Config, dst: Path) -> bool:
    """Return True if any source file exists that the destination lacks."""
    if dst.exists():
        return False
    for filename in _STATE_FILES:
        src = config.state_dir / filename
        if src.is_file():
            return True
    outcomes_src = config.state_dir / _OUTCOMES_DIR
    if outcomes_src.is_dir():
        return True
    return False


def _migrate_global_state(project_dir: Path, config: Config) -> None:
    """Copy state files from the global state dir to the per-project dir.

    Files are only copied when the source exists in *config.state_dir* and the
    per-project state directory does not already exist.  Existing project state
    is authoritative and is never overwritten by legacy global state.

    Uses atomic temp+rename so a concurrent tick can't observe a partially
    written state directory and interpret it as "in-flight".
    """
    dst = _get_project_state_dir(project_dir)

    if not needs_migration(config, dst):
        return

    # Gather files into a temporary directory first, then atomically rename
    # it into place.  The rename is atomic on the same filesystem (which is
    # the case here because we create the temp dir inside dst.parent).
    dst.parent.mkdir(exist_ok=True)
    tmp_dir = tempfile.mkdtemp(dir=dst.parent, prefix=".hermes-migrate-")

    try:
        for filename in _STATE_FILES:
            src = config.state_dir / filename
            dest = Path(tmp_dir) / filename
            if src.is_file() and not dst.joinpath(filename).exists():
                log.info("Migrating %s -> %s", src, dest)
                shutil.copy2(src, dest)

        outcomes_src = config.state_dir / _OUTCOMES_DIR
        outcomes_dst = Path(tmp_dir) / _OUTCOMES_DIR
        if outcomes_src.is_dir() and not dst.joinpath(_OUTCOMES_DIR).exists():
            log.info("Migrating %s -> %s", outcomes_src, outcomes_dst)
            outcomes_dst.mkdir(parents=True, exist_ok=True)
            for _item in outcomes_src.iterdir():
                shutil.copy2(str(_item), str(outcomes_dst / _item.name))

        os.rename(tmp_dir, dst)

    except OSError:
        # Best-effort cleanup of the temp directory on failure.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
