"""Additional coverage for decision/context kanban-aware paths and edge cases."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_pipeline.decision.context import (
    build_in_flight,
    _extract_in_flight_ids,
    _fetch_kanban_snapshot,
    _kanban_snapshot,
)


class TestExtractInFlightIds:
    """Tests for _extract_in_flight_ids() — the core parsing function."""

    def test_list_format(self):
        """Snapshot as a bare list is handled."""
        snapshot = [
            {
                "status": "running",
                "body": '{"tick_id":"01HA","phase_key":"phase_2","todo_id":"TODO-10","project_slug":"demo"}\n...',
            },
        ]
        result = _extract_in_flight_ids(snapshot)
        assert result == {"TODO-10"}

    def test_dict_format(self):
        """Snapshot as a dict with 'tasks' key is handled."""
        snapshot = {
            "tasks": [
                {
                    "status": "running",
                    "body": '{"tick_id":"01HA","phase_key":"phase_2","todo_id":"TODO-10","project_slug":"demo"}\n...',
                },
            ]
        }
        result = _extract_in_flight_ids(snapshot)
        assert result == {"TODO-10"}

    def test_done_tasks_skipped(self):
        """Done/completed tasks are not included."""
        snapshot = {
            "tasks": [
                {
                    "status": "done",
                    "body": '{"todo_id":"TODO-10"}\n...',
                },
                {
                    "status": "running",
                    "body": '{"todo_id":"TODO-11"}\n...',
                },
            ]
        }
        result = _extract_in_flight_ids(snapshot)
        assert result == {"TODO-11"}

    def test_created_tasks_included(self):
        """Created status tasks are included."""
        snapshot = {
            "tasks": [
                {
                    "status": "created",
                    "body": '{"todo_id":"TODO-5"}\n...',
                },
            ]
        }
        result = _extract_in_flight_ids(snapshot)
        assert result == {"TODO-5"}

    def test_no_header_skipped(self):
        """Tasks without a parseable JSON header are skipped."""
        snapshot = {
            "tasks": [
                {
                    "status": "running",
                    "body": "No JSON here — just text",
                },
                {
                    "status": "running",
                    "body": '{"todo_id":"TODO-10"}\n...',
                },
            ]
        }
        result = _extract_in_flight_ids(snapshot)
        assert result == {"TODO-10"}

    def test_missing_todo_id_skipped(self):
        """Tasks with JSON header but no todo_id are skipped."""
        snapshot = {
            "tasks": [
                {
                    "status": "running",
                    "body": '{"tick_id":"01HA"}\n...',
                },
            ]
        }
        result = _extract_in_flight_ids(snapshot)
        assert result == set()

    def test_empty_tasks(self):
        """Empty task list returns empty set."""
        snapshot = {"tasks": []}
        result = _extract_in_flight_ids(snapshot)
        assert result == set()

    def test_duplicate_todo_ids(self):
        """Multiple tasks for the same TODO only appear once."""
        snapshot = {
            "tasks": [
                {
                    "status": "running",
                    "body": '{"todo_id":"TODO-10","phase_key":"phase_2"}\n...',
                },
                {
                    "status": "ready",
                    "body": '{"todo_id":"TODO-10","phase_key":"phase_4"}\n...',
                },
            ]
        }
        result = _extract_in_flight_ids(snapshot)
        assert result == {"TODO-10"}

    def test_empty_body(self):
        """Tasks with empty body are skipped."""
        snapshot = {
            "tasks": [
                {
                    "status": "running",
                    "body": "",
                },
            ]
        }
        result = _extract_in_flight_ids(snapshot)
        assert result == set()


class TestFetchKanbanSnapshot:
    """Tests for _fetch_kanban_snapshot() — the CLI wrapper."""

    def test_success(self, mocker):
        """Successful fetch returns parsed JSON."""
        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"tasks": []})
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _fetch_kanban_snapshot("demo")
        assert result == {"tasks": []}

    def test_cli_failure_returns_none(self, mocker):
        """hermes not found -> None."""
        mocker.patch("subprocess.run", side_effect=FileNotFoundError("hermes not found"))
        result = _fetch_kanban_snapshot("demo")
        assert result is None

    def test_timeout_returns_none(self, mocker):
        """Subprocess timeout -> None."""
        import subprocess
        mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("hermes", 10))
        result = _fetch_kanban_snapshot("demo")
        assert result is None

    def test_json_error_returns_none(self, mocker):
        """Invalid JSON from CLI -> None."""
        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json"
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _fetch_kanban_snapshot("demo")
        assert result is None

    def test_nonzero_returncode_returns_none(self, mocker):
        """Non-zero return code from CLI -> None."""
        mock_result = mocker.MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _fetch_kanban_snapshot("demo")
        assert result is None


class TestKanbanSnapshot:
    """Tests for _kanban_snapshot() — the error-marker wrapper."""

    def test_success_delegates_to_fetch(self, mocker):
        """On success, returns the parsed snapshot."""
        mocker.patch(
            "hermes_pipeline.decision.context._fetch_kanban_snapshot",
            return_value={"tasks": [{"id": "t1"}]},
        )
        result = _kanban_snapshot("demo")
        assert result == {"tasks": [{"id": "t1"}]}

    def test_failure_returns_error_marker(self, mocker):
        """On failure, returns the error marker dict."""
        mocker.patch(
            "hermes_pipeline.decision.context._fetch_kanban_snapshot",
            return_value=None,
        )
        result = _kanban_snapshot("demo")
        assert result == {"columns": [], "_error": "kanban snapshot unavailable"}


class TestBuildInFlightKanbanOutage:
    """build_in_flight() has no file-marker fallback (strict single-kanban
    design) — on any kanban lookup failure it must return [], never raise
    and never silently resurrect the deleted phase_started/ready_for_review
    fallback."""

    def test_no_snapshot_and_no_board_slug_returns_empty(self, tmp_path):
        """No snapshot, no board_slug — nothing to look up, returns []."""
        result = build_in_flight(tmp_path, max_phase_timeout_min=60)
        assert result == []

    def test_snapshot_none_and_kanban_lookup_fails_returns_empty(self, tmp_path, mocker):
        """snapshot=None and the board_slug lookup itself fails (returns
        None) — must return [], not raise and not fall back to reading
        state_dir markers."""
        mocker.patch(
            "hermes_pipeline.decision.context._kanban_in_flight_ids",
            return_value=None,
        )
        result = build_in_flight(
            tmp_path, max_phase_timeout_min=60, board_slug="demo",
        )
        assert result == []

    def test_snapshot_provided_bypasses_board_slug_lookup(self, tmp_path, mocker):
        """When a snapshot is pre-fetched, it's used directly and
        _kanban_in_flight_ids (the CLI-calling path) is never invoked."""
        spy = mocker.patch(
            "hermes_pipeline.decision.context._kanban_in_flight_ids",
        )
        snapshot = {
            "tasks": [
                {"status": "running", "body": json.dumps({"todo_id": "TODO-7"})},
            ]
        }
        result = build_in_flight(
            tmp_path,
            max_phase_timeout_min=60,
            board_slug="demo",
            snapshot=snapshot,
        )
        assert result == ["TODO-7"]
        spy.assert_not_called()



