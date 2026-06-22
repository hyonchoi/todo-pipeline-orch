"""Lane F.3: Tests for kill subcommand."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from hermes_pipeline.cli import cmd_kill, build_parser, _cmd_kill

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
    with patch(
        "hermes_pipeline.cli._confirm_pid_exited",
        lambda pid, **kw: sent.append(pid) or True,
    ):
        rc = cmd_kill(state_dir=tmp_path, all_=True, todo=None)
    assert rc == 0
    assert sent == [99999]
    assert not (tmp_path / "phase_started" / "TODO-1.json").exists()
    assert (tmp_path / "outcomes" / "01JA.json").exists()

def test_kill_leaves_marker_when_pid_does_not_exit(tmp_path):
    """A SIGTERM-ignoring Claude process must NOT be recorded as
    killed_by_operator while it keeps running. The marker stays, no
    outcome sidecar is written, and rc=1 surfaces the failure."""
    p = tmp_path / "phase_started" / "TODO-1.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "todo_id": "TODO-1",
        "tick_id": "01JA",
        "child_pid": 99999,
        "started_at": "2026-06-13T00:00:00Z",
        "phase_key": "autoplan",
    }))
    with patch(
        "hermes_pipeline.cli._confirm_pid_exited",
        lambda pid, **kw: False,
    ):
        rc = cmd_kill(state_dir=tmp_path, all_=True, todo=None)
    assert rc == 1
    assert (tmp_path / "phase_started" / "TODO-1.json").exists(), \
        "marker must stay so next tick still sees TODO as in-flight"
    assert not (tmp_path / "outcomes" / "01JA.json").exists(), \
        "killed_by_operator outcome must NOT be written if exit unconfirmed"

def test_kill_does_not_release_lock_when_exit_unconfirmed(tmp_path):
    _marker(tmp_path, "TODO-1", tick_id="01JA", job_id="job-A")
    (tmp_path / "phase_started" / "TODO-1.json").write_text(json.dumps({
        "todo_id": "TODO-1", "tick_id": "01JA",
        "child_pid": 99999, "phase_key": "autoplan",
    }))
    (tmp_path / "tick.lock").mkdir()
    (tmp_path / "tick.lock" / "holder.json").write_text('{"tick_id": "01JA"}')
    with patch("hermes_pipeline.cli._confirm_pid_exited", lambda pid, **kw: False):
        rc = cmd_kill(state_dir=tmp_path, all_=True, todo=None)
    assert rc == 1
    assert (tmp_path / "tick.lock").exists(), \
        "unconfirmed kill must NOT release the tick lock"

# ---- Task 2: multi-project kill ----

def test_kill_parser_accepts_project_argument():
    """kill should parse with an optional project argument."""
    parser = build_parser()
    args = parser.parse_args(["kill", "--all"])
    assert args.all_ is True
    assert not hasattr(args, "project") or getattr(args, "project", None) is None

    args2 = parser.parse_args(["kill", "--all", "myproject"])
    assert args2.project == "myproject"

    args3 = parser.parse_args(["kill", "--todo", "TODO-1", "myproject"])
    assert args3.todo == "TODO-1"
    assert args3.project == "myproject"


def test_kill_without_project_scans_all_projects(tmp_path: Path):
    """kill without a project argument should scan all projects for in-flight phases."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    pb = projects_dir / "project-b"
    pb.mkdir()
    (pb / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    from hermes_pipeline.config import Config
    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    parser = build_parser()
    args = parser.parse_args(["kill", "--all"])

    with patch("hermes_pipeline.cli.cmd_kill") as mock_kill:
        mock_kill.return_value = 0
        result = _cmd_kill(args, config)

        assert result == 0
        # cmd_kill should be called with all_=True and config
        mock_kill.assert_called_once()
        call_kwargs = mock_kill.call_args.kwargs
        assert call_kwargs["all_"] is True
        assert "config" in call_kwargs


def test_cmd_kill_without_project_routes_to_multi_project(tmp_path: Path):
    """cmd_kill with project=None and config should route to _kill_all_projects."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    # Create in-flight marker in project-a
    project_state = pa / ".hermes"
    ps_dir = project_state / "phase_started"
    ps_dir.mkdir(parents=True, exist_ok=True)
    (ps_dir / "TODO-1.json").write_text(json.dumps({
        "todo_id": "TODO-1",
        "tick_id": "01JA",
        "job_id": "job-A",
    }))

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    from hermes_pipeline.config import Config
    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    sent = []
    with patch("hermes_pipeline.cli._hermes_run_kill", lambda jid: sent.append(jid) or 0):
        rc = cmd_kill(
            state_dir=state_dir,
            all_=True,
            project=None,
            config=config,
        )
    assert rc == 0
    assert "job-A" in sent
