"""State management for pipeline execution: locks, checkpoints, and ready-for-review records."""

from __future__ import annotations
import json
import os
import hashlib
import uuid as _uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Literal

from hermes_pipeline.decision import store as _decision_store


def _atomic_write_text(path: Path, payload: str) -> None:
    """Crash-safe write: tmp + rename. Same-directory tmp keeps rename atomic."""
    tmp = path.with_name(f"{path.name}.{_uuid.uuid4().hex}.tmp")
    tmp.write_text(payload)
    tmp.replace(path)

MergeStatus = Literal["pending", "merged", "rejected", "abandoned", "failed"]

# -- Outcome mapping ---------------------------------------------------------

_STATUS_TO_OUTCOME = {
    "merged": "merged",
    "rejected": "discarded",
    "abandoned": "discarded",
    # "failed" is computed dynamically
    # "pending" is no-op (not terminal)
}

def _failed_outcome(rec: ReadyForReview) -> str:
    """Derive failed_at_phase_<key> from the last phase summary key."""
    keys = list(rec.phase_summaries.keys())
    if not keys:
        return "failed_at_phase_unknown"
    return f"failed_at_phase_{keys[-1]}"


@dataclass
class ReadyForReview:
    """A TODO that passed all phases and is ready for merge via Phase 9."""
    project: str
    todo_id: int
    branch: str
    pr_url: str
    phase_summaries: dict[str, str]
    kanban_task_id: str | None
    merge_status: MergeStatus = "pending"
    error: str | None = None
    created_at: str = ""
    tick_id: str = ""  # Empty when written outside the agent path.

    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> ReadyForReview:
        """Deserialize from JSON."""
        parsed = json.loads(data)
        return cls(**parsed)


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

    def write_ready_for_review(self, rec: ReadyForReview) -> None:
        """
        Write a ready-for-review record.

        Args:
            rec: ReadyForReview record.
        """
        self.ready_dir.mkdir(parents=True, exist_ok=True)
        path = self.ready_dir / f"todo-{rec.todo_id}.json"
        _atomic_write_text(path, rec.to_json())

    def write_ready_for_review_min(
        self,
        todo_id: int,
        branch: str,
        pr_url: str,
        kanban_task_id: str | None,
    ) -> None:
        """
        Write a minimal ready-for-review record (useful for Phase 8).

        Args:
            todo_id: TODO ID.
            branch: Feature branch name.
            pr_url: PR URL.
            kanban_task_id: Optional kanban task ID.
        """
        rec = ReadyForReview(
            project=self.project,
            todo_id=todo_id,
            branch=branch,
            pr_url=pr_url,
            phase_summaries={},
            kanban_task_id=kanban_task_id,
        )
        self.write_ready_for_review(rec)

    def read_ready_for_review(self, todo_id: int) -> Optional[ReadyForReview]:
        """
        Read a ready-for-review record.

        Returns:
            ReadyForReview record if found, else None.
        """
        path = self.ready_dir / f"todo-{todo_id}.json"
        if not path.exists():
            return None
        try:
            return ReadyForReview.from_json(path.read_text())
        except (json.JSONDecodeError, KeyError):
            return None

    def _state_dir(self) -> Path:
        """Derive the .hermes state root from ready_dir."""
        return self.ready_dir.parent

    def set_merge_status(
        self,
        todo_id: int,
        status: MergeStatus,
        error: Optional[str] = None,
    ) -> None:
        """
        Update the merge_status of a ready-for-review record.

        On terminal transitions (merged, rejected, abandoned, failed) also
        writes an outcome sidecar at `.hermes/outcomes/<tick_id>.json` if
        the record carries a `tick_id`.

        Args:
            todo_id: TODO ID.
            status: New merge status.
            error: Optional error message if status is "failed".

        Raises:
            FileExistsError: If an outcome sidecar was already written for
                this tick_id (write-once invariant).
        """
        rec = self.read_ready_for_review(todo_id)
        if rec is None:
            raise ValueError(f"No ready-for-review record found for TODO {todo_id}")
        rec.merge_status = status
        rec.error = error
        self.write_ready_for_review(rec)

        # Write outcome sidecar on terminal transitions (if we have a tick_id).
        # Best-effort: a sidecar collision (decision was already terminal —
        # e.g. operator-killed-then-resurrected-then-merged) must not unwind
        # the merge_status write above, which has already persisted to disk
        # and is visible to the operator.
        if not rec.tick_id:
            return
        outcome = _STATUS_TO_OUTCOME.get(status)
        if outcome is None and status == "failed":
            outcome = _failed_outcome(rec)
        if outcome is None:
            return  # pending — not terminal, skip sidecar
        try:
            _decision_store.append_outcome(
                self._state_dir(),
                rec.tick_id,
                outcome=outcome,
                detail={"todo_id": todo_id, "error": error},
            )
        except FileExistsError:
            pass

    def list_ready_for_review_pending(self) -> list[ReadyForReview]:
        """
        List all ready-for-review records with status in ("pending", "failed").

        Returns:
            List of ReadyForReview records.
        """
        result = []
        if not self.ready_dir.exists():
            return result
        for path in self.ready_dir.glob("todo-*.json"):
            try:
                rec = ReadyForReview.from_json(path.read_text())
                if rec.merge_status in ("pending", "failed"):
                    result.append(rec)
            except (json.JSONDecodeError, KeyError):
                pass
        return result
