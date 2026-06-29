from pathlib import Path

import pytest

from hermes_pipeline.ship import (
    ShipSidecar,
    write_sidecar,
    read_sidecar,
    find_ship_sidecar,
    delete_sidecar,
    ShipError,
    gh_pr_view,
    gh_pr_merge_squash,
    git_tree_clean,
    ci_is_green,
    bump_in_pr,
)


def _sidecar(**kw):
    base = dict(
        tick_id="01TICK",
        todo_id=5,
        pr_number=42,
        pr_head_sha="abc123",
        base_branch="main",
        work_branch="todo-5-feature",
        phase_8_task_id="t_8",
        bump_version=None,
    )
    base.update(kw)
    return ShipSidecar(**base)


def test_write_then_read_roundtrip(tmp_path):
    sc = _sidecar()
    path = write_sidecar(sc, state_dir=tmp_path)
    assert path == tmp_path / "outcomes" / "01TICK-ship.json"
    assert path.exists()
    got = read_sidecar(tmp_path, "01TICK")
    assert got == sc


def test_read_missing_returns_none(tmp_path):
    assert read_sidecar(tmp_path, "NOPE") is None


def test_write_is_atomic_no_temp_left(tmp_path):
    write_sidecar(_sidecar(), state_dir=tmp_path)
    leftovers = list((tmp_path / "outcomes").glob("*.tmp"))
    assert leftovers == []


def test_find_by_todo_id(tmp_path):
    write_sidecar(_sidecar(tick_id="01AAA", todo_id=5), state_dir=tmp_path)
    write_sidecar(_sidecar(tick_id="01BBB", todo_id=9), state_dir=tmp_path)
    got = find_ship_sidecar(tmp_path, 9)
    assert got is not None
    assert got.todo_id == 9
    assert got.tick_id == "01BBB"
    assert find_ship_sidecar(tmp_path, 123) is None


def test_delete_sidecar(tmp_path):
    write_sidecar(_sidecar(), state_dir=tmp_path)
    delete_sidecar(tmp_path, "01TICK")
    assert read_sidecar(tmp_path, "01TICK") is None
    # idempotent
    delete_sidecar(tmp_path, "01TICK")


# --- Task 6: gh/git subprocess wrappers + CI-green parser ---


def test_gh_pr_view_parses_json(mocker, tmp_path):
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(
        returncode=0, stdout='{"number": 42, "state": "OPEN"}', stderr="")
    out = gh_pr_view("todo-5-feature", cwd=tmp_path)
    assert out["number"] == 42
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["gh", "pr", "view"]
    assert "--json" in cmd


def test_gh_pr_view_raises_on_failure(mocker, tmp_path):
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=1, stdout="", stderr="no pr")
    with pytest.raises(ShipError):
        gh_pr_view("nope", cwd=tmp_path)


def test_gh_pr_merge_squash_uses_match_head(mocker, tmp_path):
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")
    gh_pr_merge_squash("todo-5-feature", match_head="deadbeef", cwd=tmp_path)
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["gh", "pr", "merge"]
    assert "--squash" in cmd
    assert cmd[cmd.index("--match-head-commit") + 1] == "deadbeef"


def test_git_tree_clean(mocker, tmp_path):
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")
    assert git_tree_clean(tmp_path) is True
    mock_run.return_value = mocker.Mock(returncode=0, stdout=" M file.py\n", stderr="")
    assert git_tree_clean(tmp_path) is False


def test_ci_is_green():
    assert ci_is_green([]) is True  # no checks configured
    assert ci_is_green([{"status": "COMPLETED", "conclusion": "SUCCESS"}]) is True
    assert ci_is_green([{"state": "SUCCESS"}]) is True
    assert ci_is_green([{"status": "IN_PROGRESS", "conclusion": ""}]) is False
    assert ci_is_green([{"status": "COMPLETED", "conclusion": "FAILURE"}]) is False
    assert ci_is_green([{"state": "PENDING"}]) is False
    assert ci_is_green([
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]) is False


# --- Task 7: bump_in_pr ---

def test_bump_in_pr_writes_files_and_pushes(mocker, tmp_path):
    (tmp_path / "VERSION").write_text("0.3.3\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-pipeline"\nversion = "0.3.3"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        stdout = "newsha999\n" if cmd[:2] == ["git", "rev-parse"] else ""
        return mocker.Mock(returncode=0, stdout=stdout, stderr="")

    mocker.patch("hermes_pipeline.ship.subprocess.run", side_effect=fake_run)

    new_version, new_sha = bump_in_pr(
        project_dir=tmp_path, work_branch="todo-5-feat", todo_id=5)

    assert new_version == "0.3.4"
    assert new_sha == "newsha999"
    assert (tmp_path / "VERSION").read_text() == "0.3.4\n"
    assert 'version = "0.3.4"' in (tmp_path / "pyproject.toml").read_text()
    assert "0.3.4" in (tmp_path / "CHANGELOG.md").read_text()
    assert "TODO-5" in (tmp_path / "CHANGELOG.md").read_text()

    flat = [" ".join(c) for c in calls]
    assert any(c.startswith("git checkout todo-5-feat") for c in flat)
    assert any(c.startswith("git commit") for c in flat)
    assert any(c.startswith("git push origin todo-5-feat") for c in flat)


# --- Task 8: resolve_ship_task ---

from hermes_pipeline.ship import resolve_ship_task, GATE_PHASE_KEY
from hermes_pipeline.kanban_tasks import KanbanTaskInfo


def test_resolve_ship_task_returns_gate(mocker):
    tasks = {
        "phase_8_finish_branch": KanbanTaskInfo("t_8", "phase_8_finish_branch", "done", "TODO-5"),
        GATE_PHASE_KEY: KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"),
    }
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=tasks)
    got = resolve_ship_task(project_slug="demo", tick_id="01TICK")
    assert got is not None
    assert got.task_id == "t_9"


def test_resolve_ship_task_none_when_absent(mocker):
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value={})
    assert resolve_ship_task(project_slug="demo", tick_id="01TICK") is None
