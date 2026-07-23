"""Tests for kanban integration module."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from hermes_pipeline.kanban import (
    KanbanClient,
    ActiveTasksStore,
    KanbanOutbox,
    OutboxEntry,
    HermesKanbanAdapter,
    SyncResult,
    drain_outbox,
    PhaseStatus,
    KanbanOutcome,
)


# ============================================================================
# TC.1: KanbanClient Protocol + NullKanbanAdapter
# ============================================================================

class TestKanbanClientProtocol:
    """Test that KanbanClient Protocol is properly defined."""

    def test_protocol_has_set_active_task(self):
        """Protocol should have set_active_task method."""
        assert hasattr(KanbanClient, "set_active_task")

    def test_protocol_has_update_phase(self):
        """Protocol should have update_phase method."""
        assert hasattr(KanbanClient, "update_phase")

    def test_protocol_has_clear_active_task(self):
        """Protocol should have clear_active_task method."""
        assert hasattr(KanbanClient, "clear_active_task")




class TestSyncResult:

    """Test SyncResult dataclass."""

    def test_sync_result_success(self):
        """SyncResult with ok=True."""
        result = SyncResult(ok=True, task_id="task-123")
        assert result.ok is True
        assert result.task_id == "task-123"
        assert result.error is None

    def test_sync_result_failure(self):
        """SyncResult with ok=False and error."""
        result = SyncResult(ok=False, error="Network timeout")
        assert result.ok is False
        assert result.error == "Network timeout"
        assert result.task_id is None


# ============================================================================
# TC.2: ActiveTasksStore
# ============================================================================

class TestActiveTasksStore:
    """Test atomic JSON store for active tasks."""

    def test_get_nonexistent_project(self, tmp_path):
        """get() should return None for nonexistent project."""
        store_path = tmp_path / "active_tasks.json"
        store = ActiveTasksStore(store_path)
        assert store.get("nonexistent") is None

    def test_set_and_get(self, tmp_path):
        """set() and get() should work atomically."""
        store_path = tmp_path / "active_tasks.json"
        store = ActiveTasksStore(store_path)

        store.set("project_a", "task-123")
        assert store.get("project_a") == "task-123"
        assert store.get("project_b") is None

    def test_set_overwrites(self, tmp_path):
        """set() should overwrite previous value."""
        store_path = tmp_path / "active_tasks.json"
        store = ActiveTasksStore(store_path)

        store.set("project_a", "task-123")
        store.set("project_a", "task-456")
        assert store.get("project_a") == "task-456"

    def test_set_multiple_projects(self, tmp_path):
        """store should handle multiple projects."""
        store_path = tmp_path / "active_tasks.json"
        store = ActiveTasksStore(store_path)

        store.set("project_a", "task-1")
        store.set("project_b", "task-2")
        store.set("project_c", "task-3")

        assert store.get("project_a") == "task-1"
        assert store.get("project_b") == "task-2"
        assert store.get("project_c") == "task-3"

    def test_drop(self, tmp_path):
        """drop() should remove project from store."""
        store_path = tmp_path / "active_tasks.json"
        store = ActiveTasksStore(store_path)

        store.set("project_a", "task-1")
        store.set("project_b", "task-2")
        store.drop("project_a")

        assert store.get("project_a") is None
        assert store.get("project_b") == "task-2"

    def test_drop_nonexistent(self, tmp_path):
        """drop() on nonexistent project should not raise."""
        store_path = tmp_path / "active_tasks.json"
        store = ActiveTasksStore(store_path)

        # Should not raise
        store.drop("nonexistent")
        assert store.get("nonexistent") is None

    def test_drop_from_empty_store(self, tmp_path):
        """drop() on empty store should not raise."""
        store_path = tmp_path / "active_tasks.json"
        store = ActiveTasksStore(store_path)

        # Should not raise
        store.drop("nonexistent")

    def test_persistence(self, tmp_path):
        """store should persist across instances."""
        store_path = tmp_path / "active_tasks.json"
        store1 = ActiveTasksStore(store_path)
        store1.set("project_a", "task-123")

        # Create new instance and verify
        store2 = ActiveTasksStore(store_path)
        assert store2.get("project_a") == "task-123"

    def test_atomic_write_tmp_rename(self, tmp_path):
        """set() should use atomic tmp+rename pattern."""
        store_path = tmp_path / "active_tasks.json"
        store = ActiveTasksStore(store_path)

        store.set("project_a", "task-1")
        # Verify file exists and is valid JSON
        assert store_path.exists()
        data = json.loads(store_path.read_text())
        assert data == {"project_a": "task-1"}


# ============================================================================
# TC.3: KanbanOutbox
# ============================================================================

class TestKanbanOutbox:
    """Test outbox with T1 create-preserving collapse."""

    def test_all_empty(self, tmp_path):
        """all() should return empty list when no entries."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)
        assert outbox.all() == []

    def test_entries_for_empty(self, tmp_path):
        """entries_for() should return empty list when no entries."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)
        assert outbox.entries_for("project_a") == []

    def test_enqueue_and_all(self, tmp_path):
        """enqueue() and all() should work together."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        entry = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test", "phase": "Phase 1"},
        )
        outbox.enqueue(entry, has_task_id=False)

        all_entries = outbox.all()
        assert len(all_entries) == 1
        assert all_entries[0].project == "project_a"

    def test_entries_for_specific_project(self, tmp_path):
        """entries_for() should return only entries for a project."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        entry_a = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test", "phase": "Phase 1"},
        )
        entry_b = OutboxEntry(
            project="project_b",
            operation="update_phase",
            has_task_id=True,
            params={"task_id": "task-1", "phase": "Phase 2", "status": "running"},
        )
        outbox.enqueue(entry_a, has_task_id=False)
        outbox.enqueue(entry_b, has_task_id=True)

        assert len(outbox.entries_for("project_a")) == 1
        assert len(outbox.entries_for("project_b")) == 1
        assert len(outbox.entries_for("project_c")) == 0

    def test_dequeue_for_project(self, tmp_path):
        """dequeue_for() should remove all entries for a project."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        entry_a = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test", "phase": "Phase 1"},
        )
        entry_b = OutboxEntry(
            project="project_b",
            operation="update_phase",
            has_task_id=True,
            params={"task_id": "task-1", "phase": "Phase 2", "status": "running"},
        )
        outbox.enqueue(entry_a, has_task_id=False)
        outbox.enqueue(entry_b, has_task_id=True)

        outbox.dequeue_for("project_a")
        assert len(outbox.all()) == 1
        assert outbox.all()[0].project == "project_b"

    # T1 Collapse Rules
    def test_t1_create_already_exists_skip(self, tmp_path):
        """T1: If create exists and new create is enqueued, skip new create."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        create1 = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "First", "phase": "Phase 1"},
        )
        create2 = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 2, "title": "Second", "phase": "Phase 1"},
        )
        outbox.enqueue(create1, has_task_id=False)
        outbox.enqueue(create2, has_task_id=False)

        # Should have only the first create
        entries = outbox.all()
        assert len(entries) == 1
        assert entries[0].params["title"] == "First"

    def test_t1_create_replaces_non_create(self, tmp_path):
        """T1: If only non-create ops exist and create is enqueued, replace with create."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        update = OutboxEntry(
            project="project_a",
            operation="update_phase",
            has_task_id=True,
            params={"task_id": "task-1", "phase": "Phase 2", "status": "running"},
        )
        create = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test", "phase": "Phase 1"},
        )
        outbox.enqueue(update, has_task_id=True)
        outbox.enqueue(create, has_task_id=False)

        # Should have only the create
        entries = outbox.all()
        assert len(entries) == 1
        assert entries[0].operation == "set_active_task"
        assert entries[0].has_task_id is False

    def test_t1_non_create_folds_into_create(self, tmp_path):
        """T1: If create exists and non-create is enqueued, fold params into create."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        create = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test", "phase": "Phase 1"},
        )
        update = OutboxEntry(
            project="project_a",
            operation="update_phase",
            has_task_id=True,
            params={"task_id": "task-1", "phase": "Phase 2", "status": "running"},
        )
        outbox.enqueue(create, has_task_id=False)
        outbox.enqueue(update, has_task_id=True)

        # Should have only the create with merged params
        entries = outbox.all()
        assert len(entries) == 1
        assert entries[0].operation == "set_active_task"
        # Params should contain both original and folded params
        assert "todo_id" in entries[0].params
        assert "phase" in entries[0].params

    def test_t1_non_create_replaces_non_create(self, tmp_path):
        """T1: If only non-create ops exist and non-create is enqueued, replace."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        update1 = OutboxEntry(
            project="project_a",
            operation="update_phase",
            has_task_id=True,
            params={"task_id": "task-1", "phase": "Phase 2", "status": "running"},
        )
        update2 = OutboxEntry(
            project="project_a",
            operation="update_phase",
            has_task_id=True,
            params={"task_id": "task-1", "phase": "Phase 3", "status": "done"},
        )
        outbox.enqueue(update1, has_task_id=True)
        outbox.enqueue(update2, has_task_id=True)

        # Should have only the second update
        entries = outbox.all()
        assert len(entries) == 1
        assert entries[0].params["status"] == "done"

    def test_cap_500_entries(self, tmp_path):
        """Outbox should cap entries at 500 (default)."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path, cap=500)

        # Add 501 entries
        for i in range(501):
            entry = OutboxEntry(
                project=f"project_{i}",
                operation="set_active_task",
                has_task_id=False,
                params={"todo_id": i, "title": f"Test {i}", "phase": "Phase 1"},
            )
            outbox.enqueue(entry, has_task_id=False)

        # Should be capped at 500
        assert len(outbox.all()) == 500
        # Oldest entries (0-0) should be dropped
        assert outbox.all()[0].project == "project_1"

    def test_persistence(self, tmp_path):
        """Outbox should persist across instances."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox1 = KanbanOutbox(outbox_path)

        entry = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test", "phase": "Phase 1"},
        )
        outbox1.enqueue(entry, has_task_id=False)

        # Create new instance and verify
        outbox2 = KanbanOutbox(outbox_path)
        assert len(outbox2.all()) == 1
        assert outbox2.all()[0].project == "project_a"


# ============================================================================
# TC.4: HermesKanbanAdapter
# ============================================================================

class TestHermesKanbanAdapter:
    """Test HermesKanbanAdapter with subprocess mocking."""

    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_set_active_task_success(self, mock_run, tmp_path):
        """set_active_task should create task with --tenant and --json."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        # Mock successful task creation (JSON output from --json)
        task_result = MagicMock()
        task_result.returncode = 0
        task_result.stdout = '{"id": "task-456"}'
        task_result.stderr = ""

        mock_run.side_effect = [task_result]

        result = adapter.set_active_task(
            "project_a",
            todo_id=1,
            title="Test TODO",
            phase="Phase 1",
        )

        assert result.ok is True
        assert result.task_id == "task-456"
        assert store.get("project_a") == "task-456"
        assert len(outbox.all()) == 0  # No failures, so no outbox entries

    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_set_active_task_failure_queues_to_outbox(self, mock_run, tmp_path):
        """set_active_task failure should queue to outbox."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        # Mock failed board creation
        board_result = MagicMock()
        board_result.returncode = 1
        board_result.stdout = ""
        board_result.stderr = "Board creation failed"

        mock_run.return_value = board_result

        result = adapter.set_active_task(
            "project_a",
            todo_id=1,
            title="Test TODO",
            phase="Phase 1",
        )

        assert result.ok is False
        assert len(outbox.all()) == 1
        assert outbox.all()[0].project == "project_a"

    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_set_active_task_includes_metadata_in_body(self, mock_run, tmp_path):
        """metadata dict entries should appear in the --body argv, not in --tenant."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        task_result = MagicMock()
        task_result.returncode = 0
        task_result.stdout = '{"id": "task-789"}'
        task_result.stderr = ""
        mock_run.side_effect = [task_result]

        result = adapter.set_active_task(
            "mock-project",
            todo_id=1,
            title="Test TODO",
            phase="Phase 1",
            metadata={"tick_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "fixture_name": "happy-path"},
        )

        assert result.ok is True
        call_args = mock_run.call_args[0][0]  # first positional arg: the cmd list
        assert call_args[0:4] == ["hermes", "kanban", "create", "--tenant"]
        assert call_args[4] == "mock-project"
        body_index = call_args.index("--body") + 1
        body = call_args[body_index]
        assert "tick_id: 01ARZ3NDEKTSV4RRFFQ69G5FAV" in body
        assert "fixture_name: happy-path" in body


    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_update_phase_success(self, mock_run, tmp_path):
        """update_phase should post comment."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        # Set active task first
        store.set("project_a", "task-456")

        # Mock successful comment
        comment_result = MagicMock()
        comment_result.returncode = 0
        comment_result.stdout = ""
        comment_result.stderr = ""

        mock_run.return_value = comment_result

        result = adapter.update_phase(
            "project_a",
            phase="Phase 2: Autoplan",
            status="running",
        )

        assert result.ok is True
        assert len(outbox.all()) == 0

    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_update_phase_no_active_task_queues(self, mock_run, tmp_path):
        """update_phase without active task should queue."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        result = adapter.update_phase(
            "project_a",
            phase="Phase 2",
            status="running",
        )

        assert result.ok is False
        assert len(outbox.all()) == 1
        # Should queue as non-create (no task_id yet)
        assert outbox.all()[0].has_task_id is False

    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_clear_active_task_merged(self, mock_run, tmp_path):
        """clear_active_task with merged outcome should complete."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        store.set("project_a", "task-456")

        # Mock successful complete
        complete_result = MagicMock()
        complete_result.returncode = 0
        complete_result.stdout = ""
        complete_result.stderr = ""

        mock_run.return_value = complete_result

        result = adapter.clear_active_task(
            "project_a",
            outcome="merged",
        )

        assert result.ok is True
        assert store.get("project_a") is None

    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_clear_active_task_rejected(self, mock_run, tmp_path):
        """clear_active_task with rejected outcome should archive."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        store.set("project_a", "task-456")

        # Mock successful archive
        archive_result = MagicMock()
        archive_result.returncode = 0
        archive_result.stdout = ""
        archive_result.stderr = ""

        mock_run.return_value = archive_result

        result = adapter.clear_active_task(
            "project_a",
            outcome="rejected",
        )

        assert result.ok is True
        assert store.get("project_a") is None

    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_clear_active_task_no_active_task(self, mock_run, tmp_path):
        """clear_active_task without active task should succeed."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        result = adapter.clear_active_task(
            "project_a",
            outcome="merged",
        )

        assert result.ok is True
        assert len(outbox.all()) == 0


# ============================================================================
# TC.5: drain_outbox
# ============================================================================

class TestDrainOutbox:
    """Test drain_outbox retry driver."""

    def test_drain_outbox_empty(self, tmp_path):
        """drain_outbox on empty outbox should do nothing."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)
        adapter = Mock(spec=KanbanClient)
        adapter.set_active_task.return_value = SyncResult(ok=True)

        # Should not raise
        drain_outbox(adapter, outbox)
        assert len(outbox.all()) == 0

    def test_drain_outbox_set_active_task_success(self, tmp_path):
        """drain_outbox should dequeue on successful set_active_task."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = Mock(spec=KanbanClient)
        adapter.set_active_task.return_value = SyncResult(ok=True)

        entry = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test", "phase": "Phase 1"},
        )
        outbox.enqueue(entry, has_task_id=False)
        assert len(outbox.all()) == 1

        drain_outbox(adapter, outbox)

        # Should be dequeued after successful drain
        assert len(outbox.all()) == 0

    def test_drain_outbox_update_phase_success(self, tmp_path):
        """drain_outbox should dequeue on successful update_phase."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)
        adapter = Mock(spec=KanbanClient)
        adapter.update_phase.return_value = SyncResult(ok=True)

        entry = OutboxEntry(
            project="project_a",
            operation="update_phase",
            has_task_id=True,
            params={"task_id": "task-1", "phase": "Phase 2", "status": "running"},
        )
        outbox.enqueue(entry, has_task_id=True)
        assert len(outbox.all()) == 1

        drain_outbox(adapter, outbox)

        assert len(outbox.all()) == 0

    def test_drain_outbox_clear_active_task_success(self, tmp_path):
        """drain_outbox should dequeue on successful clear_active_task."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)
        adapter = Mock(spec=KanbanClient)
        adapter.clear_active_task.return_value = SyncResult(ok=True)

        entry = OutboxEntry(
            project="project_a",
            operation="clear_active_task",
            has_task_id=True,
            params={"task_id": "task-1", "outcome": "merged"},
        )
        outbox.enqueue(entry, has_task_id=True)
        assert len(outbox.all()) == 1

        drain_outbox(adapter, outbox)

        assert len(outbox.all()) == 0

    def test_drain_outbox_failure_leaves_in_outbox(self, tmp_path):
        """drain_outbox should leave entries when operation fails."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        # Create a mock adapter that fails
        mock_adapter = Mock(spec=KanbanClient)
        mock_adapter.set_active_task.return_value = SyncResult(
            ok=False,
            error="Network error",
        )

        entry = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test", "phase": "Phase 1"},
        )
        outbox.enqueue(entry, has_task_id=False)
        assert len(outbox.all()) == 1

        drain_outbox(mock_adapter, outbox)

        # Entry should remain in outbox
        assert len(outbox.all()) == 1

    def test_drain_outbox_multiple_projects(self, tmp_path):
        """drain_outbox should dequeue entries per project."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)
        adapter = Mock(spec=KanbanClient)
        adapter.set_active_task.return_value = SyncResult(ok=True)
        adapter.update_phase.return_value = SyncResult(ok=True)

        entry_a = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test A", "phase": "Phase 1"},
        )
        entry_b = OutboxEntry(
            project="project_b",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 2, "title": "Test B", "phase": "Phase 1"},
        )
        outbox.enqueue(entry_a, has_task_id=False)
        outbox.enqueue(entry_b, has_task_id=False)
        assert len(outbox.all()) == 2

        drain_outbox(adapter, outbox)

        # Both should be dequeued
        assert len(outbox.all()) == 0

    def test_drain_outbox_mixed_success_and_failure(self, tmp_path):
        """drain_outbox should handle mixed success/failure."""
        outbox_path = tmp_path / "outbox.jsonl"
        outbox = KanbanOutbox(outbox_path)

        # Create a mock adapter that fails for project_a, succeeds for project_b
        mock_adapter = Mock(spec=KanbanClient)
        mock_adapter.set_active_task.side_effect = [
            SyncResult(ok=False, error="Error for A"),
            SyncResult(ok=True),
        ]

        entry_a = OutboxEntry(
            project="project_a",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 1, "title": "Test A", "phase": "Phase 1"},
        )
        entry_b = OutboxEntry(
            project="project_b",
            operation="set_active_task",
            has_task_id=False,
            params={"todo_id": 2, "title": "Test B", "phase": "Phase 1"},
        )
        outbox.enqueue(entry_a, has_task_id=False)
        outbox.enqueue(entry_b, has_task_id=False)

        drain_outbox(mock_adapter, outbox)

        # project_a should remain, project_b should be dequeued
        remaining = outbox.all()
        assert len(remaining) == 1
        assert remaining[0].project == "project_a"
