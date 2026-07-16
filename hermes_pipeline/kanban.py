"""Kanban integration for pipeline orchestration.

Provides a Protocol-based KanbanClient interface with implementations for null (no-op)
and hermes (CLI-based) adapters. Includes atomic store management and a create-preserving
outbox for resilient sync with cap and drop-oldest-first on overflow.
"""

"""
NOTE: HermesKanbanAdapter, KanbanOutbox, and ActiveTasksStore are retained for
backward compatibility (--kanban null path, merge orchestration). The harness
--kanban hermes path has moved to kanban-as-scheduler via kanban_tasks.py.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

log = logging.getLogger(__name__)

# Phase statuses and outcomes
PhaseStatus = Literal["running", "done", "failed", "ready_for_review"]
KanbanOutcome = Literal["merged", "rejected", "abandoned"]


@dataclass(frozen=True)
class SyncResult:
    """Result of a kanban sync operation."""
    ok: bool
    task_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class OutboxEntry:
    """A queued kanban operation waiting for retry."""
    project: str
    operation: str  # "set_active_task", "update_phase", or "clear_active_task"
    has_task_id: bool  # True if the operation has an associated task_id
    params: dict[str, str | int]  # JSON-serializable parameters


class KanbanClient(Protocol):
    """Protocol for kanban sync operations. Implementations are non-blocking and return SyncResult."""

    def set_active_task(
        self,
        project: str,
        *,
        todo_id: int,
        title: str,
        phase: str,
        metadata: dict[str, str] | None = None,
    ) -> SyncResult:
        """Set the active task for a project. Called when a TODO moves into Phase 1 (Development).

        metadata, when provided, is additional key/value context (e.g. tick_id, fixture_name,
        state_dir) recorded in the card body for debug-trail purposes. Implementations that
        don\'t render a body (e.g. NullKanbanAdapter) accept and ignore it.
        """
        ...

    def update_phase(
        self,
        project: str,
        *,
        phase: str,
        status: PhaseStatus,
    ) -> SyncResult:
        """Update the phase status for the active task. Called as pipeline progresses."""
        ...

    def clear_active_task(
        self,
        project: str,
        *,
        outcome: KanbanOutcome,
    ) -> SyncResult:
        """Clear the active task (merge, reject, or abandon). Called after Phase 8/9."""
        ...


class NullKanbanAdapter:
    """No-op kanban adapter. All operations succeed silently."""

    def set_active_task(
        self,
        project: str,
        *,
        todo_id: int,
        title: str,
        phase: str,
        metadata: dict[str, str] | None = None,
    ) -> SyncResult:
        return SyncResult(ok=True)

    def update_phase(
        self,
        project: str,
        *,
        phase: str,
        status: PhaseStatus,
    ) -> SyncResult:
        return SyncResult(ok=True)

    def clear_active_task(
        self,
        project: str,
        *,
        outcome: KanbanOutcome,
    ) -> SyncResult:
        return SyncResult(ok=True)


class ActiveTasksStore:
    """Atomic JSON store mapping project → task_id.

    Uses tmp+rename pattern for atomic writes.
    """

    def __init__(self, store_path: Path):
        self.store_path = Path(store_path)

    def get(self, project: str) -> str | None:
        """Retrieve task_id for project, or None if not set."""
        if not self.store_path.exists():
            return None
        try:
            data = json.loads(self.store_path.read_text())
            return data.get(project)
        except Exception as e:
            log.warning("failed to read active tasks store: %s", e)
            return None

    def set(self, project: str, task_id: str) -> None:
        """Set task_id for project, creating or updating atomically."""
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text())
            else:
                data = {}
            data[project] = task_id
            # Atomic write via tmp+rename
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.store_path.parent,
                delete=False,
            ) as tmp:
                json.dump(data, tmp)
                tmp_path = Path(tmp.name)
            tmp_path.replace(self.store_path)
        except Exception as e:
            log.error("failed to write active tasks store: %s", e)

    def drop(self, project: str) -> None:
        """Remove project from store."""
        try:
            if not self.store_path.exists():
                return
            data = json.loads(self.store_path.read_text())
            data.pop(project, None)
            # Atomic write via tmp+rename
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.store_path.parent,
                delete=False,
            ) as tmp:
                json.dump(data, tmp)
                tmp_path = Path(tmp.name)
            tmp_path.replace(self.store_path)
        except Exception as e:
            log.error("failed to update active tasks store: %s", e)


class KanbanOutbox:
    """Outbox for queued kanban operations with create-preserving collapse (T1).

    Rules:
    - If a non-create op is enqueued while a pending create (no task_id) exists for the same project,
      fold the new op's phase/status into the create and keep only the create.
    - Otherwise, replace the existing entry for that project.
    - Cap total entries at 500; drop oldest first on overflow.
    """

    def __init__(self, outbox_path: Path, cap: int = 500):
        self.outbox_path = Path(outbox_path)
        self.cap = cap
        self._entries: list[OutboxEntry] = []
        self._load()

    def _load(self) -> None:
        """Load entries from disk."""
        if not self.outbox_path.exists():
            self._entries = []
            return
        try:
            lines = self.outbox_path.read_text().splitlines()
            self._entries = [
                OutboxEntry(**json.loads(line))
                for line in lines
                if line.strip()
            ]
        except Exception as e:
            log.warning("failed to load outbox: %s", e)
            self._entries = []

    def _save(self) -> None:
        """Save entries to disk."""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.outbox_path.parent,
                delete=False,
            ) as tmp:
                for entry in self._entries:
                    tmp.write(json.dumps(entry.__dict__) + "\n")
                tmp_path = Path(tmp.name)
            tmp_path.replace(self.outbox_path)
        except Exception as e:
            log.error("failed to save outbox: %s", e)

    def all(self) -> list[OutboxEntry]:
        """Return all queued entries."""
        return list(self._entries)

    def entries_for(self, project: str) -> list[OutboxEntry]:
        """Return all entries for a specific project."""
        return [e for e in self._entries if e.project == project]

    def enqueue(self, entry: OutboxEntry, has_task_id: bool) -> None:
        """Enqueue an operation, applying collapse rules (T1).

        If has_task_id=False (create operation) and a create already exists for the project,
        keep the existing create and do nothing.

        If has_task_id=False (create operation) and only non-create ops exist for the project,
        replace all of them with this create.

        If has_task_id=True (non-create operation) and a create exists for the project,
        fold this operation's params into the create and keep the create.

        Otherwise, replace the existing entry for the project.
        """
        # Find existing entries for this project
        existing = [e for e in self._entries if e.project == entry.project]

        if not has_task_id:
            # This is a create operation
            # Check if a create already exists
            existing_creates = [e for e in existing if not e.has_task_id]
            if existing_creates:
                # Keep the existing create, don't add the new one
                return
            # No create exists; remove all other entries for this project and add the create
            self._entries = [e for e in self._entries if e.project != entry.project]
            self._entries.append(entry)
        else:
            # This is a non-create operation
            # Check if a create exists for this project
            existing_creates = [e for e in existing if not e.has_task_id]
            if existing_creates:
                # Fold this operation's params into the create
                create_entry = existing_creates[0]
                # Merge params (new params override old ones)
                updated_params = {**create_entry.params, **entry.params}
                updated_create = OutboxEntry(
                    project=create_entry.project,
                    operation=create_entry.operation,
                    has_task_id=create_entry.has_task_id,
                    params=updated_params,
                )
                # Replace the create with the updated version
                self._entries = [
                    e if e != create_entry else updated_create
                    for e in self._entries
                ]
            else:
                # No create exists; replace existing entry for this project
                self._entries = [e for e in self._entries if e.project != entry.project]
                self._entries.append(entry)

        # Enforce cap
        if len(self._entries) > self.cap:
            # Drop oldest entries until we're under cap
            self._entries = self._entries[-(self.cap):]

        self._save()

    def dequeue_for(self, project: str) -> None:
        """Remove all entries for a project."""
        self._entries = [e for e in self._entries if e.project != project]
        self._save()


class HermesKanbanAdapter:
    """Kanban adapter using `hermes kanban` CLI commands.

    Uses the following commands:
    - hermes kanban create --tenant <tenant> <title> --body <body> --json → returns task json
    - hermes kanban comment <task_id> "<message>"
    - hermes kanban complete <task_id>
    - hermes kanban archive <task_id>

    On failure, operations are queued to the outbox for retry.
    """

    def __init__(self, outbox: KanbanOutbox, active_tasks: ActiveTasksStore):
        self.outbox = outbox
        self.active_tasks = active_tasks

    def _run_cmd(
        self,
        cmd: list[str],
        timeout: int = 10,
    ) -> tuple[bool, str]:
        """Run a hermes kanban command and return (ok, output_or_error)."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            else:
                return False, result.stderr.strip() or result.stdout.strip()
        except subprocess.TimeoutExpired:
            return False, f"Command timed out after {timeout}s"
        except Exception as e:
            return False, str(e)

    def set_active_task(
        self,
        project: str,
        *,
        todo_id: int,
        title: str,
        phase: str,
        metadata: dict[str, str] | None = None,
    ) -> SyncResult:
        """Set active task. Creates a task card in the project tenant."""
        # Create the task using --tenant for namespacing
        body = f"Phase: {phase}\nTODO ID: {todo_id}"
        if metadata:
            for key, value in metadata.items():
                body += f"\n{key}: {value}"
        ok, output = self._run_cmd(
            [
                "hermes", "kanban", "create",
                "--tenant", project,
                title,
                "--body", body,
                "--json",
            ]
        )

        if not ok:
            # Queue for retry
            params: dict[str, str | int] = {
                "todo_id": todo_id,
                "title": title,
                "phase": phase,
            }
            if metadata:
                params["metadata"] = json.dumps(metadata)
            entry = OutboxEntry(
                project=project,
                operation="set_active_task",
                has_task_id=False,
                params=params,
            )
            self.outbox.enqueue(entry, has_task_id=False)
            return SyncResult(ok=False, error=output)

        # Parse task_id from JSON output
        log.debug("kanban registration payload (raw JSON, truncated): %s", output[:500])
        try:
            task_data = json.loads(output)
            task_id = task_data["id"]
        except (json.JSONDecodeError, KeyError) as e:
            return SyncResult(ok=False, error=f"Failed to parse task ID: {e}")

        self.active_tasks.set(project, task_id)
        return SyncResult(ok=True, task_id=task_id)

    def update_phase(
        self,
        project: str,
        *,
        phase: str,
        status: PhaseStatus,
    ) -> SyncResult:
        """Update phase status by posting a comment on the task."""
        task_id = self.active_tasks.get(project)
        if not task_id:
            # No active task yet; queue for retry
            entry = OutboxEntry(
                project=project,
                operation="update_phase",
                has_task_id=False,
                params={
                    "phase": phase,
                    "status": status,
                },
            )
            self.outbox.enqueue(entry, has_task_id=False)
            return SyncResult(
                ok=False,
                error="No active task found for project",
            )

        message = f"{phase} — {status}"
        ok, output = self._run_cmd(
            ["hermes", "kanban", "comment", task_id, message]
        )

        if not ok:
            # Queue for retry
            entry = OutboxEntry(
                project=project,
                operation="update_phase",
                has_task_id=True,
                params={
                    "task_id": task_id,
                    "phase": phase,
                    "status": status,
                },
            )
            self.outbox.enqueue(entry, has_task_id=True)
            return SyncResult(ok=False, error=output)

        return SyncResult(ok=True, task_id=task_id)

    def clear_active_task(
        self,
        project: str,
        *,
        outcome: KanbanOutcome,
    ) -> SyncResult:
        """Clear active task. For merged, complete; otherwise archive."""
        task_id = self.active_tasks.get(project)
        if not task_id:
            # No active task; just succeed
            return SyncResult(ok=True)

        if outcome == "merged":
            cmd = ["hermes", "kanban", "complete", task_id]
        else:
            cmd = ["hermes", "kanban", "archive", task_id]

        ok, output = self._run_cmd(cmd)

        if not ok:
            # Queue for retry
            entry = OutboxEntry(
                project=project,
                operation="clear_active_task",
                has_task_id=True,
                params={
                    "task_id": task_id,
                    "outcome": outcome,
                },
            )
            self.outbox.enqueue(entry, has_task_id=True)
            return SyncResult(ok=False, error=output)

        # Clear from store
        self.active_tasks.drop(project)
        return SyncResult(ok=True)


def drain_outbox(adapter: KanbanClient, outbox: KanbanOutbox) -> None:
    """Retry all queued outbox operations. Dequeues on success, leaves on failure."""
    for entry in outbox.all():
        if entry.operation == "set_active_task":
            raw_metadata = entry.params.get("metadata")
            try:
                metadata = json.loads(raw_metadata) if raw_metadata else None
            except (json.JSONDecodeError, TypeError):
                log.warning("failed to parse metadata for outbox entry %s: skipping metadata", entry.project)
                metadata = None
            r = adapter.set_active_task(
                entry.project,
                todo_id=entry.params["todo_id"],
                title=entry.params["title"],
                phase=entry.params["phase"],
                metadata=metadata,
            )
        elif entry.operation == "update_phase":
            r = adapter.update_phase(
                entry.project,
                phase=entry.params["phase"],
                status=entry.params["status"],
            )
        elif entry.operation == "clear_active_task":
            r = adapter.clear_active_task(
                entry.project,
                outcome=entry.params["outcome"],
            )
        else:
            log.error("unknown outbox operation: %s", entry.operation)
            continue

        if r.ok:
            outbox.dequeue_for(entry.project)
