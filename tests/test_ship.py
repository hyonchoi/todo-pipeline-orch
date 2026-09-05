
import pytest

from hermes_pipeline.ship import (
    ChecksInconclusive,
    ShipError,
    ShipSidecar,
    bump_in_pr,
    ci_is_green,
    delete_sidecar,
    find_ship_sidecar,
    gh_pr_merge_squash,
    gh_pr_view,
    git_tree_clean,
    read_sidecar,
    write_sidecar,
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
    # This line used to read `assert ci_is_green([]) is True  # no checks
    # configured`, and that assertion pinned a bug. An empty status-check
    # rollup has several causes and only one of them is "this repo configures
    # no CI"; the dangerous one is a workflow startup failure. `ci_is_green`
    # is handed a bare list with no repo or sha, so it cannot tell the causes
    # apart and must refuse to answer instead of guessing "green".
    with pytest.raises(ChecksInconclusive):
        ci_is_green([])
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
        # Distinguish the two rev-parse calls: --abbrev-ref returns the branch
        # name (orig_branch), plain rev-parse returns the new HEAD sha.
        if cmd[:2] == ["git", "rev-parse"]:
            if "--abbrev-ref" in cmd:
                return mocker.Mock(returncode=0, stdout="main\n", stderr="")
            return mocker.Mock(returncode=0, stdout="newsha999\n", stderr="")
        return mocker.Mock(returncode=0, stdout="", stderr="")

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
    # Original branch is restored after the bump completes.
    assert any(c.startswith("git checkout main") for c in flat)


def test_bump_in_pr_pyproject_only_does_not_create_version_file(mocker, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.3.3"\n'
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "0.3.3"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")

    def fake_run(cmd, **kw):
        stdout = "main\n" if "--abbrev-ref" in cmd else "newsha999\n"
        return mocker.Mock(returncode=0, stdout=stdout, stderr="")

    mocker.patch("hermes_pipeline.ship.subprocess.run", side_effect=fake_run)
    version, _sha = bump_in_pr(
        project_dir=tmp_path, work_branch="todo-5-feat", todo_id=5
    )
    assert version == "0.3.4"
    assert not (tmp_path / "VERSION").exists()
    assert 'version = "0.3.4"' in (tmp_path / "pyproject.toml").read_text()


def test_bump_in_pr_version_only_remains_supported(mocker, tmp_path):
    (tmp_path / "VERSION").write_text("0.3.3\n")

    def fake_run(cmd, **kw):
        stdout = "main\n" if "--abbrev-ref" in cmd else "newsha999\n"
        return mocker.Mock(returncode=0, stdout=stdout, stderr="")

    mocker.patch("hermes_pipeline.ship.subprocess.run", side_effect=fake_run)
    version, _sha = bump_in_pr(
        project_dir=tmp_path, work_branch="todo-5-feat", todo_id=5
    )
    assert version == "0.3.4"
    assert (tmp_path / "VERSION").read_text() == "0.3.4\n"
    assert not (tmp_path / "pyproject.toml").exists()


def test_bump_in_pr_rejects_conflicting_manifests(mocker, tmp_path):
    from hermes_pipeline.ship import _bump_version

    (tmp_path / "VERSION").write_text("0.3.3\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.3.4"\n'
    )
    with pytest.raises(ShipError, match="disagree"):
        _bump_version(tmp_path)


def test_bump_in_pr_rejects_missing_manifest(mocker, tmp_path):
    from hermes_pipeline.ship import _bump_version

    with pytest.raises(ShipError, match="neither"):
        _bump_version(tmp_path)


def test_bump_in_pr_restores_original_branch_on_failure(mocker, tmp_path):
    """bump_in_pr restores the original branch even if bump fails."""
    (tmp_path / "VERSION").write_text("0.3.3\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-pipeline"\nversion = "0.3.3"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            if "--abbrev-ref" in cmd:
                return mocker.Mock(returncode=0, stdout="main\n", stderr="")
            return mocker.Mock(returncode=0, stdout="newsha999\n", stderr="")
        if cmd[:2] == ["git", "push"]:
            raise Exception("push failed")  # simulate CI-red before push
        return mocker.Mock(returncode=0, stdout="", stderr="")

    mocker.patch("hermes_pipeline.ship.subprocess.run", side_effect=fake_run)

    with pytest.raises(Exception, match="push failed"):
        bump_in_pr(
            project_dir=tmp_path, work_branch="todo-5-feat", todo_id=5)

    # The last git command should be checkout back to main.
    checkouts = [c for c in calls if c[:2] == ["git", "checkout"]]
    assert checkouts[0] == ["git", "checkout", "todo-5-feat"]
    assert checkouts[1] == ["git", "checkout", "main"]


# --- Task 8: resolve_ship_task ---

from hermes_pipeline.kanban_tasks import KanbanTaskInfo
from hermes_pipeline.ship import GATE_PHASE_KEY, resolve_ship_task


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

from hermes_pipeline.ship import ApproveRefused, approve_lock


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

# Reuse _guard_sidecar defined above — no need for duplicate


def test_bump_then_ci_pending_refuses_with_retry(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.bump_in_pr", return_value=("0.3.4", "bumpedsha"))
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "state": "OPEN", "headRefOid": "bumpedsha",
        "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}],
    })
    merge = mocker.patch("hermes_pipeline.ship.gh_pr_merge_squash")
    sc = _guard_sidecar()
    with pytest.raises(ApproveRefused, match="CI"):
        _bump_and_merge(sidecar=sc, project_dir=tmp_path, state_dir=tmp_path)
    merge.assert_not_called()
    # Sidecar must have been re-baselined so a retry skips the bump.
    persisted = read_sidecar(tmp_path, "01TICK")
    assert persisted.bump_version == "0.3.4"
    assert persisted.pr_head_sha == "bumpedsha"


def test_bump_and_merge_refuses_empty_rollup_instead_of_merging(mocker, tmp_path):
    """An empty rollup is never on its own a licence to merge.

    ``_bump_and_merge`` is the one production caller of ``ci_is_green``. It has
    the repo and the head sha, so it corroborates the emptiness against the
    commit rather than inheriting it; a rollup that stays uncorroborated still
    ends in a refusal that says the CI status is undetermined. Here the commit's
    check-suites report a suite that DID produce check runs, which contradicts
    the empty rollup gh just reported: those run states were never classified,
    so they cannot be waved through.

    The assertion on the ``gh api`` calls is what keeps this test honest. Without
    it a blanket refusal that never asks the commit anything would pass it just
    as happily as the corroborating one, and the refusal would be right for the
    wrong reason.
    """
    merge = _empty_rollup(mocker)
    run = _corroboration(mocker, suites=(_projected_suite("codecov", runs=3),))
    with pytest.raises(ApproveRefused, match="undetermined"):
        _bump_and_merge(sidecar=_guard_sidecar(), project_dir=tmp_path,
                        state_dir=tmp_path)
    assert [c[:2] for c in run.argv_calls].count(["gh", "api"]) >= 1
    merge.assert_not_called()


def test_retry_skips_bump_and_merges_when_green(mocker, tmp_path):
    bump = mocker.patch("hermes_pipeline.ship.bump_in_pr")
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "state": "OPEN", "headRefOid": "bumpedsha",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    })
    merge = mocker.patch("hermes_pipeline.ship.gh_pr_merge_squash")
    sc = _guard_sidecar(bump_version="0.3.4", pr_head_sha="bumpedsha")
    _bump_and_merge(sidecar=sc, project_dir=tmp_path, state_dir=tmp_path)
    bump.assert_not_called()
    merge.assert_called_once()
    _, kwargs = merge.call_args
    assert kwargs["match_head"] == "bumpedsha"


# --- Task 12: approve_ship ---

from hermes_pipeline.ship import approve_ship, maybe_ship_ready


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


# --- Task 14: wire maybe_ship_ready into _tick_project ---


def test_tick_project_calls_maybe_ship_ready_before_early_return(mocker, tmp_path):
    """maybe_ship_ready must run even when all_phases_complete is False."""
    import hermes_pipeline.cli as cli

    # Force the in-flight early-return path.
    mocker.patch.object(cli, "_read_prior_tick_id", return_value="01TICK")
    mocker.patch.object(cli, "all_phases_complete", return_value=False)
    mocker.patch.object(cli, "_make_circuit_breaker", return_value=mocker.Mock())
    mocker.patch("hermes_pipeline.project_config._resolve_slack_channel",
                 return_value="#ship")
    called = mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

    # all_phases_complete is False, so _tick_project returns cleanly at the
    # early-return — but maybe_ship_ready must have already fired before it.
    cli._tick_project(
        project_dir=tmp_path,
        project_slug="demo",
        project_state=tmp_path,
        tick_id="02NEXT",
        config=mocker.Mock(slack_channel=None),
        cb_cfg=mocker.Mock(),
        project_toml={},
    )

    called.assert_called_once()
    kwargs = called.call_args.kwargs
    assert kwargs["prior_tick_id"] == "01TICK"
    assert kwargs["project_slug"] == "demo"


# --- Task 15: uncovered codepaths ---


def test_read_sidecar_corrupt_json_returns_none(tmp_path, mocker, caplog):
    """read_sidecar handles corrupt JSON gracefully, returns None and warns."""
    from hermes_pipeline.ship import write_sidecar
    # Write a valid sidecar first, then corrupt it.
    sc = _sidecar()
    path = write_sidecar(sc, state_dir=tmp_path)
    path.write_text("not valid json{{{")
    assert read_sidecar(tmp_path, "01TICK") is None
    assert "corrupt" in caplog.text.lower()


def test_ci_is_green_neutral_and_skipped_treated_as_green():
    """NEUTRAL and SKIPPED conclusions are considered green."""
    assert ci_is_green([{"status": "COMPLETED", "conclusion": "NEUTRAL"}]) is True
    assert ci_is_green([{"status": "COMPLETED", "conclusion": "SKIPPED"}]) is True
    assert ci_is_green([
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
    ]) is True


def test_ci_is_green_mixed_checkrun_and_statuscontext():
    """Mixed CheckRun and StatusContext entries all pass."""
    assert ci_is_green([
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"state": "SUCCESS"},
    ]) is True


def test_ci_is_green_empty_rollup_may_be_a_workflow_startup_failure():
    """An empty rollup is evidence of emptiness, not of an absent gate.

    Live worked example: ``yehiashouman/WearExerciseManager`` at
    ``4c14b532d7da2a99a9e3b337fece90a5336fdc43`` reports no checks through
    ``gh``, while ``check-suites`` shows one ``github-actions`` suite with
    ``latest_check_runs_count: 0`` and ``conclusion: failure``. That is a
    workflow *startup failure* -- a run is created, concludes ``failure`` and
    produces zero jobs -- which is exactly the shape an agent that breaks
    ``.github/workflows/*`` leaves behind. Reading it as green merges a branch
    whose CI never ran a single job. Separating that cause from "no CI is
    configured" needs the commit itself, which this predicate is never handed,
    so raising is where its responsibility ends. The corroboration happens one
    layer up, in ``_bump_and_merge``, against the rule stated in
    ``todos_completion._rollup_is_honestly_empty``; the tests for it are at the
    end of this file. Note that the rule is not "a zero-run suite is a failure"
    -- a zero-run suite with a NULL conclusion from a third-party App is benign;
    it is the conclusion and the App slug that separate this shape from that one.
    """
    with pytest.raises(ChecksInconclusive, match="corroborat"):
        ci_is_green([])


def test_ci_is_green_none_state_and_none_status():
    """Missing state/status fields are treated as failure."""
    assert ci_is_green([{}]) is False
    assert ci_is_green([{"state": None}]) is False
    assert ci_is_green([{"status": None, "conclusion": "SUCCESS"}]) is False


def test_find_ship_sidecar_no_outcomes_dir(tmp_path):
    """find_ship_sidecar returns None when outcomes dir doesn't exist."""
    # Don't create the outcomes directory at all
    assert find_ship_sidecar(tmp_path, 5) is None


def test_find_ship_sidecar_skips_corrupt_files(tmp_path):
    """find_ship_sidecar skips corrupt files and still finds valid ones."""
    outcomes = tmp_path / "outcomes"
    outcomes.mkdir()
    (outcomes / "bad-ship.json").write_text("not json")
    write_sidecar(_sidecar(tick_id="01GOOD", todo_id=5), state_dir=tmp_path)
    got = find_ship_sidecar(tmp_path, 5)
    assert got is not None
    assert got.tick_id == "01GOOD"


def test_complete_gate_task_raises_on_failure(mocker):
    """complete_gate_task raises ShipError when hermes CLI fails."""
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=1, stdout="", stderr="no task")
    from hermes_pipeline.ship import complete_gate_task
    with pytest.raises(ShipError, match="hermes kanban complete"):
        complete_gate_task("t_nonexistent")


def test_gh_pr_view_json_decode_error(mocker, tmp_path):
    """gh_pr_view raises ShipError when output is not valid JSON."""
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="not json{{{", stderr="")
    with pytest.raises(ShipError, match="non-JSON"):
        gh_pr_view("todo-5-feature", cwd=tmp_path)


def test_bump_in_pr_no_changelog_creates_new(mocker, tmp_path):
    """bump_in_pr creates CHANGELOG.md from scratch when it doesn't exist."""
    (tmp_path / "VERSION").write_text("0.3.3\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.3.3"\n')
    # No CHANGELOG.md

    def fake_run(cmd, **kw):
        stdout = "newsha\n" if cmd[:2] == ["git", "rev-parse"] else ""
        return mocker.Mock(returncode=0, stdout=stdout, stderr="")

    mocker.patch("hermes_pipeline.ship.subprocess.run", side_effect=fake_run)

    new_version, _ = bump_in_pr(
        project_dir=tmp_path, work_branch="todo-5-feat", todo_id=5)

    changelog = tmp_path / "CHANGELOG.md"
    assert changelog.exists()
    content = changelog.read_text()
    assert "# Changelog" in content
    assert new_version in content


def test_maybe_ship_ready_no_pipeline_branch_file(tmp_path, mocker, caplog):
    """maybe_ship_ready returns when pipeline_branch.txt is missing."""
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks",
                 return_value=_ready_tasks())
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "number": 42, "headRefOid": "x", "baseRefName": "main",
    })
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")

    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path,
                     slack_channel="#ship")

    assert read_sidecar(tmp_path, "01TICK") is None
    notify.assert_not_called()
    assert "no pipeline_branch.txt" in caplog.text


def test_maybe_ship_ready_empty_pipeline_branch(tmp_path, mocker):
    """maybe_ship_ready returns when pipeline_branch.txt is empty."""
    (tmp_path / "pipeline_branch.txt").write_text("  \n")
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks",
                 return_value=_ready_tasks())
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")

    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path,
                     slack_channel="#ship")

    assert read_sidecar(tmp_path, "01TICK") is None
    notify.assert_not_called()


def test_maybe_ship_ready_exception_swallowed(tmp_path, mocker, caplog):
    """maybe_ship_ready never breaks the tick — exceptions are logged."""
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks",
                 side_effect=RuntimeError("oops"))
    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path,
                     slack_channel="#ship")
    # Should not raise; the exception is swallowed
    assert "maybe_ship_ready failed" in caplog.text


# --- Corroborating an empty rollup at the one site that can ---
#
# `ci_is_green` is handed a bare list, so it can only raise `ChecksInconclusive`.
# `_bump_and_merge` is the layer that knows the repository and the head sha, so
# it is the layer that can go and ask the commit itself whether a gate exists.
# The rule it applies is `todos_completion._rollup_is_honestly_empty` -- imported,
# not restated, because ship and todos_completion drifting apart on exactly this
# question is what produced the live false green.

import json
from types import SimpleNamespace

from hermes_pipeline.github_issues import GitHubIssuesError

WEM_REPO = "yehiashouman/WearExerciseManager"
WEM_SHA = "4c14b532d7da2a99a9e3b337fece90a5336fdc43"


def _projected_suite(app, *, runs=0, conclusion=None, status="queued"):
    """One `check_suites` element in the shape gh's `--jq` projection emits.

    The RAW REST payloads and the projection that produces this shape are pinned
    in `tests/test_todos_completion.py`; these tests deliberately work in the
    projected shape, which is what `_rollup_is_honestly_empty` parses.
    """
    return {"runs": runs, "conclusion": conclusion, "status": status, "app": app}


def _corroboration(mocker, *, suites=(), statuses=0, returncode=0, repo=WEM_REPO):
    """Stub the two read-only REST calls `_rollup_is_honestly_empty` makes."""
    mocker.patch(
        "hermes_pipeline.github_issues.repository_identity", return_value=repo
    )
    calls = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        if "check-suites" in argv[2]:
            out = json.dumps({"total": len(suites), "suites": list(suites)})
        else:
            out = f"{statuses}\n"
        return SimpleNamespace(returncode=returncode, stdout=out, stderr="")

    patched = mocker.patch(
        "hermes_pipeline.todos_completion.subprocess.run", side_effect=_run
    )
    patched.argv_calls = calls
    return patched


def _empty_rollup(mocker, *, head="bumpedsha", view_head=None):
    """Drive `_bump_and_merge` to the empty-rollup branch; return the merge mock."""
    mocker.patch("hermes_pipeline.ship.bump_in_pr", return_value=("0.3.4", head))
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "state": "OPEN", "headRefOid": view_head or head, "statusCheckRollup": [],
    })
    return mocker.patch("hermes_pipeline.ship.gh_pr_merge_squash")


def test_bump_and_merge_merges_when_the_absence_of_ci_is_corroborated(mocker, tmp_path):
    """A genuinely CI-less repo must still be able to ship.

    Fail-closed on an empty rollup was the right first move, but left alone it
    costs the autonomy property outright: nothing could ever merge on a repo
    that has no CI. The commit here carries one zero-run third-party App suite
    with a NULL conclusion and no legacy statuses -- an App that was notified
    and did nothing -- which is proof that there is no gate to pass.
    """
    merge = _empty_rollup(mocker)
    run = _corroboration(mocker, suites=(_projected_suite("renovate"),), statuses=0)

    _bump_and_merge(sidecar=_guard_sidecar(), project_dir=tmp_path, state_dir=tmp_path)

    merge.assert_called_once()
    assert merge.call_args.kwargs["match_head"] == "bumpedsha"
    api = [c for c in run.argv_calls if c[:2] == ["gh", "api"]]
    assert [c[2] for c in api] == [
        f"repos/{WEM_REPO}/commits/bumpedsha/check-suites?per_page=100",
        f"repos/{WEM_REPO}/commits/bumpedsha/status",
    ]


def test_bump_and_merge_refuses_the_wear_exercise_manager_startup_failure(mocker, tmp_path):
    """The live shape that must never merge.

    `yehiashouman/WearExerciseManager` at
    `4c14b532d7da2a99a9e3b337fece90a5336fdc43`: gh reports no checks, while
    `check-suites` holds one `github-actions` suite with zero check runs and
    `conclusion: failure`. A run was created, concluded `failure` and produced
    zero jobs -- exactly what a worker that breaks `.github/workflows/*` leaves
    behind. Merging it ships a branch whose CI never ran a single job.
    """
    merge = _empty_rollup(mocker, head=WEM_SHA)
    run = _corroboration(mocker, suites=(
        _projected_suite("github-actions", conclusion="failure", status="completed"),
    ), statuses=0)

    with pytest.raises(ApproveRefused, match="corroborat"):
        _bump_and_merge(sidecar=_guard_sidecar(), project_dir=tmp_path,
                        state_dir=tmp_path)

    merge.assert_not_called()
    assert [c[:2] for c in run.argv_calls].count(["gh", "api"]) >= 1


def test_bump_and_merge_refuses_when_corroboration_cannot_be_established(mocker, tmp_path):
    """An error while proving a negative is not proof of the negative.

    A merge is irreversible and outward-facing, so a failed corroboration call
    refuses exactly as loudly as a disproved one.
    """
    merge = _empty_rollup(mocker)
    run = _corroboration(mocker, returncode=1)

    with pytest.raises(ApproveRefused, match="checks_unavailable"):
        _bump_and_merge(sidecar=_guard_sidecar(), project_dir=tmp_path,
                        state_dir=tmp_path)

    merge.assert_not_called()
    assert [c[:2] for c in run.argv_calls].count(["gh", "api"]) >= 1


def test_bump_and_merge_refuses_when_the_repository_cannot_be_identified(mocker, tmp_path):
    """No repo means no commit to ask, which is an ambiguity, not an absence."""
    merge = _empty_rollup(mocker)
    mocker.patch(
        "hermes_pipeline.github_issues.repository_identity",
        side_effect=GitHubIssuesError("origin_identity_invalid", "git remote"),
    )

    with pytest.raises(ApproveRefused, match="origin_identity_invalid"):
        _bump_and_merge(sidecar=_guard_sidecar(), project_dir=tmp_path,
                        state_dir=tmp_path)

    merge.assert_not_called()


def test_bump_and_merge_refuses_when_the_rollup_describes_another_commit(mocker, tmp_path):
    """Corroborating the wrong commit would prove nothing about this one.

    If the PR head moved between the bump push and the view, the empty rollup
    belongs to a commit that is not the one `--match-head-commit` would merge.
    """
    merge = _empty_rollup(mocker, head="bumpedsha", view_head="someone_elses_sha")
    _corroboration(mocker, suites=(_projected_suite("renovate"),), statuses=0)

    with pytest.raises(ApproveRefused, match="does not describe"):
        _bump_and_merge(sidecar=_guard_sidecar(), project_dir=tmp_path,
                        state_dir=tmp_path)

    merge.assert_not_called()
