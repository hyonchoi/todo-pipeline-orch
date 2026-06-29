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


# --- Task 9: approve_lock ---

from hermes_pipeline.ship import approve_lock, ApproveRefused


def test_approve_lock_excludes_second_holder(tmp_path):
    with approve_lock(tmp_path):
        with pytest.raises(ApproveRefused):
            with approve_lock(tmp_path):
                pass


def test_approve_lock_reacquirable_after_release(tmp_path):
    with approve_lock(tmp_path):
        pass
    # Should not raise the second time.
    with approve_lock(tmp_path):
        pass


# --- Task 10: _check_ship_guards ---

from hermes_pipeline.ship import _check_ship_guards


def _guard_sidecar(**kw):
    base = dict(
        tick_id="01TICK", todo_id=5, pr_number=42, pr_head_sha="reviewed_sha",
        base_branch="main", work_branch="todo-5-feat",
        phase_8_task_id="t_8", bump_version=None,
    )
    base.update(kw)
    return ShipSidecar(**base)


def test_guards_refuse_dirty_tree(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=False)
    with pytest.raises(ApproveRefused, match="dirty"):
        _check_ship_guards(
            sidecar=_guard_sidecar(), live_head_sha="reviewed_sha",
            project_dir=tmp_path, state_dir=tmp_path, force_count=0)


def test_guards_dirty_tree_not_force_bypassable(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=False)
    with pytest.raises(ApproveRefused, match="dirty"):
        _check_ship_guards(
            sidecar=_guard_sidecar(), live_head_sha="reviewed_sha",
            project_dir=tmp_path, state_dir=tmp_path, force_count=2)


def test_guards_refuse_stale_sha(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    with pytest.raises(ApproveRefused, match="SHA"):
        _check_ship_guards(
            sidecar=_guard_sidecar(), live_head_sha="DIFFERENT",
            project_dir=tmp_path, state_dir=tmp_path, force_count=0)


def test_guards_stale_sha_bypassed_by_double_force_and_audited(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    _check_ship_guards(
        sidecar=_guard_sidecar(), live_head_sha="DIFFERENT",
        project_dir=tmp_path, state_dir=tmp_path, force_count=2)
    audit = (tmp_path / "approve_audit.log").read_text()
    assert "force" in audit.lower()
    assert "DIFFERENT" in audit


def test_guards_single_force_does_not_bypass_sha(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    with pytest.raises(ApproveRefused, match="SHA"):
        _check_ship_guards(
            sidecar=_guard_sidecar(), live_head_sha="DIFFERENT",
            project_dir=tmp_path, state_dir=tmp_path, force_count=1)


def test_guards_skip_sha_check_after_bump(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    # bump_version set => SHA already re-baselined; live mismatch is fine here
    # because the live sha equals the bumped sidecar sha in practice. Pass a
    # mismatch to prove the check is skipped, not merely satisfied.
    _check_ship_guards(
        sidecar=_guard_sidecar(bump_version="0.3.4", pr_head_sha="bumped"),
        live_head_sha="something_else",
        project_dir=tmp_path, state_dir=tmp_path, force_count=0)


# --- Task 11: _bump_and_merge ---

from hermes_pipeline.ship import _bump_and_merge


def _merge_sidecar(**kw):
    base = dict(
        tick_id="01TICK", todo_id=5, pr_number=42, pr_head_sha="reviewed_sha",
        base_branch="main", work_branch="todo-5-feat",
        phase_8_task_id="t_8", bump_version=None,
    )
    base.update(kw)
    return ShipSidecar(**base)


def test_bump_then_ci_pending_refuses_with_retry(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.bump_in_pr", return_value=("0.3.4", "bumpedsha"))
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "state": "OPEN", "headRefOid": "bumpedsha",
        "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}],
    })
    merge = mocker.patch("hermes_pipeline.ship.gh_pr_merge_squash")
    sc = _merge_sidecar()
    with pytest.raises(ApproveRefused, match="CI"):
        _bump_and_merge(sidecar=sc, project_dir=tmp_path, state_dir=tmp_path)
    merge.assert_not_called()
    # Sidecar must have been re-baselined so a retry skips the bump.
    persisted = read_sidecar(tmp_path, "01TICK")
    assert persisted.bump_version == "0.3.4"
    assert persisted.pr_head_sha == "bumpedsha"


def test_retry_skips_bump_and_merges_when_green(mocker, tmp_path):
    bump = mocker.patch("hermes_pipeline.ship.bump_in_pr")
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "state": "OPEN", "headRefOid": "bumpedsha",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    })
    merge = mocker.patch("hermes_pipeline.ship.gh_pr_merge_squash")
    sc = _merge_sidecar(bump_version="0.3.4", pr_head_sha="bumpedsha")
    _bump_and_merge(sidecar=sc, project_dir=tmp_path, state_dir=tmp_path)
    bump.assert_not_called()
    merge.assert_called_once()
    _, kwargs = merge.call_args
    assert kwargs["match_head"] == "bumpedsha"


# --- Task 12: approve_ship ---

from hermes_pipeline.ship import approve_ship, write_sidecar, maybe_ship_ready


def _ready_tasks():
    return {
        "phase_8_finish_branch": KanbanTaskInfo("t_8", "phase_8_finish_branch", "done", "TODO-5"),
        GATE_PHASE_KEY: KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"),
    }


def test_maybe_ship_ready_writes_sidecar_and_alerts(tmp_path, mocker):
    (tmp_path / "pipeline_branch.txt").write_text("todo-5-feat\n")
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=_ready_tasks())
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "number": 42, "headRefOid": "reviewed_sha", "baseRefName": "main",
        "state": "OPEN", "statusCheckRollup": [],
    })
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")

    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path,
                     slack_channel="#ship")

    sc = read_sidecar(tmp_path, "01TICK")
    assert sc is not None
    assert sc.todo_id == 5
    assert sc.pr_number == 42
    assert sc.pr_head_sha == "reviewed_sha"
    assert sc.work_branch == "todo-5-feat"
    notify.assert_called_once()
    assert "#ship" == notify.call_args[0][0]


def test_maybe_ship_ready_dedups_on_existing_sidecar(tmp_path, mocker):
    write_sidecar(ShipSidecar(
        tick_id="01TICK", todo_id=5, pr_number=42, pr_head_sha="x",
        base_branch="main", work_branch="b"), state_dir=tmp_path)
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=_ready_tasks())
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")
    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path, slack_channel="#ship")
    notify.assert_not_called()


def test_maybe_ship_ready_noop_when_phase_unfinished(tmp_path, mocker):
    tasks = {
        "phase_8_finish_branch": KanbanTaskInfo("t_8", "phase_8_finish_branch", "running", "TODO-5"),
        GATE_PHASE_KEY: KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"),
    }
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=tasks)
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")
    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path, slack_channel="#ship")
    assert read_sidecar(tmp_path, "01TICK") is None
    notify.assert_not_called()


def test_maybe_ship_ready_noop_when_no_gate(tmp_path, mocker):
    tasks = {
        "phase_8_finish_branch": KanbanTaskInfo("t_8", "phase_8_finish_branch", "done", "TODO-5"),
    }
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=tasks)
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")
    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path, slack_channel="#ship")
    assert read_sidecar(tmp_path, "01TICK") is None
    notify.assert_not_called()


def _seed_sidecar(tmp_path, **kw):
    base = dict(
        tick_id="01TICK", todo_id=5, pr_number=42, pr_head_sha="reviewed_sha",
        base_branch="main", work_branch="todo-5-feat",
        phase_8_task_id="t_8", bump_version=None,
    )
    base.update(kw)
    write_sidecar(ShipSidecar(**base), state_dir=tmp_path)


def test_approve_refuses_without_sidecar(tmp_path, mocker):
    mocker.patch("hermes_pipeline.ship.resolve_ship_task")
    with pytest.raises(ApproveRefused, match="no pending ship"):
        approve_ship(project_dir=tmp_path, project_slug="demo",
                     todo_id=5, state_dir=tmp_path)


def test_approve_idempotent_when_already_merged(tmp_path, mocker):
    _seed_sidecar(tmp_path)
    mocker.patch("hermes_pipeline.ship.resolve_ship_task",
                 return_value=KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"))
    mocker.patch("hermes_pipeline.ship.gh_pr_view",
                 return_value={"state": "MERGED", "headRefOid": "x", "statusCheckRollup": []})
    complete = mocker.patch("hermes_pipeline.ship.complete_gate_task")
    bump = mocker.patch("hermes_pipeline.ship._bump_and_merge")
    summary = approve_ship(project_dir=tmp_path, project_slug="demo",
                           todo_id=5, state_dir=tmp_path)
    bump.assert_not_called()
    complete.assert_called_once_with("t_9")
    assert find_ship_sidecar(tmp_path, 5) is None
    assert "already" in summary.lower()


def test_approve_happy_path_merges_and_completes(tmp_path, mocker):
    _seed_sidecar(tmp_path)
    mocker.patch("hermes_pipeline.ship.resolve_ship_task",
                 return_value=KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"))
    mocker.patch("hermes_pipeline.ship.gh_pr_view",
                 return_value={"state": "OPEN", "headRefOid": "reviewed_sha",
                               "statusCheckRollup": []})
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    bump = mocker.patch("hermes_pipeline.ship._bump_and_merge")
    complete = mocker.patch("hermes_pipeline.ship.complete_gate_task")
    summary = approve_ship(project_dir=tmp_path, project_slug="demo",
                           todo_id=5, state_dir=tmp_path)
    bump.assert_called_once()
    complete.assert_called_once_with("t_9")
    assert find_ship_sidecar(tmp_path, 5) is None
    assert "TODO-5" in summary


def test_approve_refuses_when_no_gate_task(tmp_path, mocker):
    _seed_sidecar(tmp_path)
    mocker.patch("hermes_pipeline.ship.resolve_ship_task", return_value=None)
    with pytest.raises(ApproveRefused, match="gate task"):
        approve_ship(project_dir=tmp_path, project_slug="demo",
                     todo_id=5, state_dir=tmp_path)
