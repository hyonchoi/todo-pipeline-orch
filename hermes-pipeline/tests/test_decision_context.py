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
