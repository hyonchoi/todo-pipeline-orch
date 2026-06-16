from __future__ import annotations
import json
import time
from pathlib import Path
from hermes_pipeline.decision.context import build_in_flight, build_context

def _touch(p: Path, body: str = "{}", mtime_ago_s: float = 0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    if mtime_ago_s:
        now = time.time()
        import os
        os.utime(p, (now - mtime_ago_s, now - mtime_ago_s))

def test_in_flight_union_of_rfr_and_markers(tmp_path):
    # `todo-<n>.json` is what `State.write_ready_for_review` writes — the
    # canonical filename merge.run_phase9 reads back. Both bare-int and
    # `todo-` prefixed forms must collapse to `TODO-N`.
    _touch(tmp_path / "ready_for_review" / "todo-1.json", '{"todo_id": 1}')
    _touch(tmp_path / "phase_started" / "TODO-2.json", '{"started_at": "now"}')
    ids = build_in_flight(tmp_path, max_phase_timeout_min=120)
    assert set(ids) == {"TODO-1", "TODO-2"}

def test_rfr_legacy_bare_int_filename_still_recognized(tmp_path):
    _touch(tmp_path / "ready_for_review" / "1.json", '{"todo_id": 1}')
    ids = build_in_flight(tmp_path, max_phase_timeout_min=120)
    assert set(ids) == {"TODO-1"}

def test_stale_markers_without_pid_are_swept(tmp_path):
    _touch(tmp_path / "phase_started" / "TODO-3.json", '{}', mtime_ago_s=60 * 60 * 5)
    ids = build_in_flight(tmp_path, max_phase_timeout_min=120)
    assert "TODO-3" not in ids
    assert not (tmp_path / "phase_started" / "TODO-3.json").exists()

def test_stale_marker_with_live_pid_is_not_swept(tmp_path, monkeypatch):
    """A wedged-but-alive Claude process must remain visible as in-flight.
    Otherwise the next tick would re-pick the same TODO and two phases
    would mutate the repo concurrently."""
    import json as _json
    p = tmp_path / "phase_started" / "TODO-4.json"
    _touch(p, _json.dumps({"child_pid": 12345}), mtime_ago_s=60 * 60 * 5)
    monkeypatch.setattr(
        "hermes_pipeline.decision.context._pid_alive", lambda pid: True,
    )
    ids = build_in_flight(tmp_path, max_phase_timeout_min=120)
    assert "TODO-4" in ids
    assert p.exists(), "must not sweep a live wedged phase"

def test_stale_marker_with_dead_pid_is_swept(tmp_path, monkeypatch):
    import json as _json
    p = tmp_path / "phase_started" / "TODO-5.json"
    _touch(p, _json.dumps({"child_pid": 99999}), mtime_ago_s=60 * 60 * 5)
    monkeypatch.setattr(
        "hermes_pipeline.decision.context._pid_alive", lambda pid: False,
    )
    ids = build_in_flight(tmp_path, max_phase_timeout_min=120)
    assert "TODO-5" not in ids
    assert not p.exists()

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

    def test_build_in_flight_fallback_to_files(self, state_dir, mocker):
        """build_in_flight falls back to file markers when kanban fails."""
        from hermes_pipeline.decision.context import build_in_flight

        mocker.patch(
            "hermes_pipeline.decision.context._kanban_in_flight_ids",
            return_value=None,
        )

        # Create a file marker in phase_started (the fallback reads there)
        marker_dir = state_dir / "phase_started"
        marker_dir.mkdir(exist_ok=True)
        marker = marker_dir / "TODO-3.json"
        marker.write_text(json.dumps({"tick_id": "old", "phase_key": "phase_2_autoplan"}))

        result = build_in_flight(
            state_dir=state_dir,
            max_phase_timeout_min=120,
            board_slug="demo",
        )
        assert "TODO-3" in result

def test_build_context_assembles_all_fields(tmp_path, monkeypatch):
    todos = tmp_path / "TODOS.md"
    todos.write_text("- TODO-1: do thing\n")
    monkeypatch.setattr(
        "hermes_pipeline.decision.context._kanban_snapshot",
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
