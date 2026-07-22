from __future__ import annotations
import json
import time
from pathlib import Path
from hermes_pipeline.decision.context import build_in_flight, build_context













class TestKanbanInFlight:
    """Tests for _kanban_in_flight_ids() and kanban-aware build_in_flight()."""

    def test_kanban_in_flight_ids_parsing(self, tmp_path, mocker):
        """_kanban_in_flight_ids extracts TODO IDs from kanban JSON with in-flight tasks."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        mock_data = {
            "tasks": [
                {
                    "status": "running",
                    "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\nDo the work',
                },
                {
                    "status": "ready",
                    "body": '{"tick_id":"01HA","phase_key":"phase_3_writing","todo_id":"TODO-10","project_slug":"demo"}\nWrite plan',
                },
                {
                    "status": "done",
                    "body": '{"tick_id":"01H9","phase_key":"phase_2_autoplan","todo_id":"TODO-9","project_slug":"demo"}\nDone',
                },
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _kanban_in_flight_ids("demo")
        assert result == {"TODO-10"}

    def test_kanban_in_flight_returns_none_on_failure(self, tmp_path, mocker):
        """CLI failure -> None (fallback to file markers)."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        mocker.patch("subprocess.run", side_effect=FileNotFoundError)

        result = _kanban_in_flight_ids("demo")
        assert result is None

    def test_kanban_in_flight_skips_no_header(self, tmp_path, mocker):
        """Tasks without JSON header are skipped, not crashed."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        mock_data = {
            "tasks": [
                {
                    "status": "running",
                    "body": "No JSON header — just raw text",
                },
                {
                    "status": "running",
                    "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\nValid header',
                },
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _kanban_in_flight_ids("demo")
        assert result == {"TODO-10"}

    def test_build_in_flight_uses_kanban(self, state_dir, mocker):
        """build_in_flight uses kanban when available."""
        from hermes_pipeline.decision.context import build_in_flight

        mocker.patch(
            "hermes_pipeline.decision.context._kanban_in_flight_ids",
            return_value={"TODO-7"},
        )

        result = build_in_flight(
            state_dir=state_dir,
            max_phase_timeout_min=120,
            board_slug="demo",
        )
        assert result == ["TODO-7"]

    

    def test_kanban_in_flight_json_decode_error(self, tmp_path, mocker):
        """_kanban_in_flight_ids handles JSONDecodeError from subprocess."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json"
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _kanban_in_flight_ids("demo")
        assert result is None

    def test_kanban_in_flight_timeout(self, tmp_path, mocker):
        """_kanban_in_flight_ids handles subprocess timeout."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        import subprocess
        mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("hermes", 10))

        result = _kanban_in_flight_ids("demo")
        assert result is None

    def test_kanban_in_flight_dict_format(self, tmp_path, mocker):
        """_kanban_in_flight_ids handles dict format {'tasks': [...]}."""
        from hermes_pipeline.decision.context import _kanban_in_flight_ids

        mock_data = {
            "tasks": [
                {
                    "status": "running",
                    "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\nDo the work',
                },
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = _kanban_in_flight_ids("demo")
        assert result == {"TODO-10"}

def test_build_context_assembles_all_fields(tmp_path, monkeypatch):
    todos = tmp_path / "TODOS.md"
    todos.write_text("- TODO-1: do thing\n")
    monkeypatch.setattr(
        "hermes_pipeline.decision.context._fetch_kanban_snapshot",
        lambda slug: {"columns": ["doing"]},
    )
    monkeypatch.setattr(
        "hermes_pipeline.decision.context._recent_decisions",
        lambda state_dir, n: [{"tick_id": "old", "picked": "TODO-1", "outcome": "merged"}],
    )
    ctx = build_context(
        tick_id="01JT",
        state_dir=tmp_path,
        todos_path=todos,
        project_slug="demo",
        max_phase_timeout_min=120,
    )
    assert ctx.todos_md == "- TODO-1: do thing\n"
    assert ctx.project_slug == "demo"
    assert ctx.recent_decisions[0]["outcome"] == "merged"
    assert ctx.kanban_snapshot == {"columns": ["doing"]}
