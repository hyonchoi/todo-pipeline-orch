"""Additional coverage for kanban.py set_active_task JSON parse errors."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

from hermes_pipeline.kanban import (
    HermesKanbanAdapter,
    KanbanOutbox,
    ActiveTasksStore,
)


class TestSetActiveTaskJsonParse:
    """Tests for JSON parse error handling in set_active_task."""

    def test_json_decode_error(self, tmp_path):
        """Invalid JSON output from kanban create raises parse error."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        with patch("hermes_pipeline.kanban.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not valid json",
                stderr="",
            )

            result = adapter.set_active_task(
                "project_a",
                todo_id=1,
                title="Test",
                phase="Phase 1",
            )

            assert result.ok is False
            assert "parse" in result.error.lower()

    def test_missing_id_key(self, tmp_path):
        """JSON output without 'id' key raises parse error."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        with patch("hermes_pipeline.kanban.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"task": "t123"}),
                stderr="",
            )

            result = adapter.set_active_task(
                "project_a",
                todo_id=1,
                title="Test",
                phase="Phase 1",
            )

            assert result.ok is False
            assert "parse" in result.error.lower()

    def test_task_creation_failure_rc_nonzero(self, tmp_path):
        """Non-zero return code from kanban create returns SyncResult ok=False."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        with patch("hermes_pipeline.kanban.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Board not found",
            )

            result = adapter.set_active_task(
                "project_a",
                todo_id=1,
                title="Test",
                phase="Phase 1",
            )

            assert result.ok is False

    def test_set_active_task_uses_tenant_flag(self, tmp_path):
        """Verify --tenant is used instead of --board in set_active_task."""
        outbox_path = tmp_path / "outbox.jsonl"
        store_path = tmp_path / "active_tasks.json"
        outbox = KanbanOutbox(outbox_path)
        store = ActiveTasksStore(store_path)
        adapter = HermesKanbanAdapter(outbox, store)

        with patch("hermes_pipeline.kanban.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"id": "task-001"}),
                stderr="",
            )

            adapter.set_active_task(
                "myproject",
                todo_id=1,
                title="Test",
                phase="Phase 1",
            )

            # Verify the command uses --tenant
            cmd = mock_run.call_args[0][0]
            assert "--tenant" in cmd
            assert "myproject" in cmd
            # Verify --json is used
            assert "--json" in cmd
