"""Lane F.3: Tests for kill subcommand."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from hermes_pipeline.cli import cmd_kill

def _marker(d: Path, todo_id: str, tick_id: str = "01JT", job_id: str = "job-1") -> Path:
    p = d / "phase_started" / f"{todo_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({
            "todo_id": todo_id,
            "tick_id": tick_id,
            "job_id": job_id,
            "started_at": "2026-06-13T00:00:00Z",
            "phase_key": "autoplan",
        })
    )
    return p

def test_kill_all_kills_each_marker(tmp_path):
    _marker(tmp_path, "TODO-1", tick_id="01JA", job_id="job-A")
    _marker(tmp_path, "TODO-2", tick_id="01JB", job_id="job-B")
    sent = []
    with patch("hermes_pipeline.cli._hermes_run_kill", lambda jid: sent.append(jid) or 0):
        rc = cmd_kill(state_dir=tmp_path, all_=True, todo=None)
    assert rc == 0
    assert set(sent) == {"job-A", "job-B"}
    assert not (tmp_path / "phase_started" / "TODO-1.json").exists()
    assert not (tmp_path / "phase_started" / "TODO-2.json").exists()
    assert json.loads((tmp_path / "outcomes" / "01JA.json").read_text())["outcome"] == "killed_by_operator"
    assert json.loads((tmp_path / "outcomes" / "01JB.json").read_text())["outcome"] == "killed_by_operator"

def test_kill_specific_todo(tmp_path):
    _marker(tmp_path, "TODO-1", tick_id="01JA", job_id="job-A")
    _marker(tmp_path, "TODO-2", tick_id="01JB", job_id="job-B")
    with patch("hermes_pipeline.cli._hermes_run_kill", lambda jid: 0):
        rc = cmd_kill(state_dir=tmp_path, all_=False, todo="TODO-1")
    assert rc == 0
    assert not (tmp_path / "phase_started" / "TODO-1.json").exists()
    assert (tmp_path / "phase_started" / "TODO-2.json").exists()

def test_kill_releases_tick_lock_when_holder_matches(tmp_path):
    _marker(tmp_path, "TODO-1", tick_id="01JA", job_id="job-A")
    (tmp_path / "tick.lock").mkdir()
    (tmp_path / "tick.lock" / "holder.json").write_text('{"tick_id": "01JA"}')
    with patch("hermes_pipeline.cli._hermes_run_kill", lambda jid: 0):
        rc = cmd_kill(state_dir=tmp_path, all_=True, todo=None)
    assert rc == 0
    assert not (tmp_path / "tick.lock").exists()

def test_kill_does_not_release_unrelated_tick_lock(tmp_path):
    """A typo in --todo must not break an unrelated tick's critical section."""
    _marker(tmp_path, "TODO-1", tick_id="01JA", job_id="job-A")
    (tmp_path / "tick.lock").mkdir()
    (tmp_path / "tick.lock" / "holder.json").write_text('{"tick_id": "01JOTHER"}')
    with patch("hermes_pipeline.cli._hermes_run_kill", lambda jid: 0):
        rc = cmd_kill(state_dir=tmp_path, all_=True, todo=None)
    assert rc == 0
    assert (tmp_path / "tick.lock").exists(), "kill must not steal locks it does not own"

def test_kill_missing_todo_does_not_touch_lock(tmp_path):
    """A no-op kill (target missing) must not touch tick.lock at all."""
    _marker(tmp_path, "TODO-1", tick_id="01JA", job_id="job-A")
    (tmp_path / "tick.lock").mkdir()
    (tmp_path / "tick.lock" / "holder.json").write_text('{"tick_id": "01JLIVE"}')
    with patch("hermes_pipeline.cli._hermes_run_kill", lambda jid: 0):
        rc = cmd_kill(state_dir=tmp_path, all_=False, todo="TODO-999")
    assert rc == 2
    assert (tmp_path / "tick.lock" / "holder.json").exists()

def test_kill_uses_child_pid_when_present(tmp_path):
    p = tmp_path / "phase_started" / "TODO-1.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "todo_id": "TODO-1",
        "tick_id": "01JA",
        "child_pid": 99999,
        "started_at": "2026-06-13T00:00:00Z",
        "phase_key": "autoplan",
    }))
    sent = []
    with patch("hermes_pipeline.cli._signal_pid", lambda pid: sent.append(pid) or True):
        rc = cmd_kill(state_dir=tmp_path, all_=True, todo=None)
    assert rc == 0
    assert sent == [99999]
