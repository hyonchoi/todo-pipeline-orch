"""State management for pipeline execution: locks and checkpoints."""

from __future__ import annotations
import json
import os
import hashlib
import uuid as _uuid
from pathlib import Path
from typing import Optional


def _atomic_write_text(path: Path, payload: str) -> None:
    """Crash-safe write: tmp + rename. Same-directory tmp keeps rename atomic."""
    tmp = path.with_name(f"{path.name}.{_uuid.uuid4().hex}.tmp")
    tmp.write_text(payload)
    tmp.replace(path)


class State:
    """Manages pipeline state: locks, hashes, checkpoints, and ready-for-review records."""

    def __init__(
        self,
        project: str,
        lock_dir: Path | str,
        checkpoint_dir: Path | str,
        ready_dir: Path | str,
    ):
        """
        Initialize State for a given project.

        Args:
            project: Project name
            lock_dir: Directory for project.lock files
            checkpoint_dir: Directory for checkpoint JSON files
            ready_dir: Directory for ready-for-review records
        """
        self.project = project
        self.lock_dir = Path(lock_dir)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.ready_dir = Path(ready_dir)
        self.lock_path = self.lock_dir / f"{project}.lock"

    def is_locked(self) -> bool:
        """Check if this project is locked."""
        return self.lock_path.exists()

    def lock(self) -> None:
        """
        Acquire an exclusive lock for this project using O_EXCL.

        Raises:
            FileExistsError: If lock is already held.
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Use O_EXCL for atomic exclusive creation
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
        except FileExistsError:
            raise FileExistsError(f"Lock already held: {self.lock_path}")

    def unlock(self) -> None:
        """Release the lock for this project."""
        if self.lock_path.exists():
            self.lock_path.unlink()

    def get_saved_hash(self) -> Optional[str]:
        """
        Get the last saved TODOS.md hash for change detection.

        Returns:
            The hash string if saved, else None.
        """
        todos_hash_file = self.lock_dir / f"{self.project}.todos_hash"
        if todos_hash_file.exists():
            return todos_hash_file.read_text().strip()
        return None

    def save_hash(self, h: str) -> None:
        """
        Save TODOS.md hash for change detection.

        Args:
            h: Hash string to save.
        """
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        todos_hash_file = self.lock_dir / f"{self.project}.todos_hash"
        todos_hash_file.write_text(h)

    def last_completed_phase_index(self, todo_id: int) -> int:
        """
        Get the last completed phase index for a TODO.

        Returns:
            0-based phase index, or -1 if no checkpoint exists.
        """
        checkpoint_path = self.checkpoint_dir / f"todo-{todo_id}.json"
        if not checkpoint_path.exists():
            return -1
        try:
            data = json.loads(checkpoint_path.read_text())
            return data.get("last_completed_phase_index", -1)
        except (json.JSONDecodeError, KeyError):
            return -1

    def mark_phase_done(self, todo_id: int, phase_key: str, phase_index: int) -> None:
        """
        Atomically mark a phase as completed in the checkpoint JSON.

        Args:
            todo_id: TODO ID.
            phase_key: Phase key (e.g., "P1_research").
            phase_index: 0-based phase index.
        """
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = self.checkpoint_dir / f"todo-{todo_id}.json"

        # Read existing or start fresh
        if checkpoint_path.exists():
            data = json.loads(checkpoint_path.read_text())
        else:
            data = {}

        data["last_completed_phase_index"] = phase_index
        data["completed_phases"] = data.get("completed_phases", {})
        data["completed_phases"][phase_key] = True

        # Crash-safe write: tmp + rename so a mid-write crash never leaves a
        # truncated JSON that read_text + json.loads would swallow as "no
        # checkpoint" and re-drive the phase.
        _atomic_write_text(checkpoint_path, json.dumps(data, indent=2))

    def reset(self, todo_id: int) -> None:
        """
        Clear all checkpoints for a TODO.

        Args:
            todo_id: TODO ID.
        """
        checkpoint_path = self.checkpoint_dir / f"todo-{todo_id}.json"
        if checkpoint_path.exists():
            checkpoint_path.unlink()

