"""Post-merge delivery must survive advancement to a no-selection tick."""

import json

import pytest

from hermes_pipeline import github_issues
from hermes_pipeline.cli import _tick_project
from hermes_pipeline.config import CircuitBreakerConfig, Config
from hermes_pipeline.outcomes import CURRENT_TICK_ID_FILE
from hermes_pipeline.run_registration import registration_state
from tests.gh_fakes import seed_project_issues
from tests.test_todos_completion import (
    MARKER,
    FakeRemoteIssue,
    _finish_done_fixture,
    _finish_tasks,
    _view,
)


@pytest.mark.parametrize(
    ("issue_auto_closed", "prior_tick"),
    [(False, "01EMPTY"), (True, "01EMPTY"), (True, "01TICK")],
    ids=["historical-open-issue", "historical-auto-closed", "current-auto-closed"],
)
def test_tick_delivers_verified_run_after_human_merge(
    tmp_path, mocker, fake_gh, issue_auto_closed, prior_tick,
):
    drift_check = github_issues.check_issue_drift
    state = _finish_done_fixture(
        tmp_path, mocker, tasks=_finish_tasks(), view=_view("MERGED"),
    )
    mocker.patch("hermes_pipeline.github_issues.check_issue_drift", side_effect=drift_check)
    run_dir = state / "runs" / "01TICK"
    (run_dir / "finish-verified").write_text("a" * 40 + "\n")
    (state / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\n'
        'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
    )
    (state / CURRENT_TICK_ID_FILE).write_text(prior_tick)
    (state / "outcomes").mkdir()
    (state / "outcomes" / "01EMPTY-phases.json").write_text(
        '{"outcome":"picked_none"}\n'
    )
    seed_project_issues(fake_gh, [])
    issue = FakeRemoteIssue(
        fake_gh,
        state="closed" if issue_auto_closed else "open",
        state_reason="completed" if issue_auto_closed else None,
    )
    live = github_issues.fetch_issue(tmp_path, 3, repo="acme/repo")
    (run_dir / "registration.json").write_text(json.dumps({
        "schema_version": 3, "issue_number": 3, "issue_url": live.url,
        "selected_entry_hash": live.entry_hash,
    }))
    mocker.patch("hermes_pipeline.todos_completion._show_task_payload", return_value={})
    checks = mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    mocker.patch("hermes_pipeline.kanban_tasks.reconcile_pending_task_create", return_value=True)
    mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
    results = mocker.patch("hermes_pipeline.kanban_tasks.reconcile_plan_task_results", return_value=True)
    reviews = mocker.patch("hermes_pipeline.review_reconciliation.reconcile_reviews", return_value=True)
    claim = mocker.patch("hermes_pipeline.run_registration.ensure_in_progress_label")
    mocker.patch("hermes_pipeline.cli.all_phases_complete", return_value=True)
    mocker.patch("hermes_pipeline.kanban_tasks.get_todo_kanban_status", return_value={})
    mocker.patch("hermes_pipeline.cli.observe_outcomes")
    mocker.patch("hermes_pipeline.cli._make_circuit_breaker")
    mocker.patch(
        "hermes_pipeline.decision.context.fetch_kanban_snapshot",
        return_value={"columns": []},
    )

    def tick(tick_id):
        _tick_project(
            project_dir=tmp_path, project_slug="demo", project_state=state,
            config=Config(prompt_client="codex"), cb_cfg=CircuitBreakerConfig(),
            tick_id=tick_id, project_toml={},
        )

    assert registration_state(run_dir) == "active"
    tick("01NEXT")

    assert registration_state(run_dir) == "delivered"
    assert issue.state == "closed"
    assert "tpo:in-progress" not in issue.labels
    assert len(issue.comments) == 1
    assert issue.comments[0].rstrip().endswith(MARKER)
    assert issue.writes == (["comment", "edit"] if issue_auto_closed else ["comment", "close", "edit"])
    checks.assert_called_once()
    if prior_tick == "01TICK":
        results.assert_not_called()
        reviews.assert_not_called()
        claim.assert_not_called()

    tick("01AGAIN")

    assert len(issue.comments) == 1
    checks.assert_called_once()
    assert issue.writes == (["comment", "edit"] if issue_auto_closed else ["comment", "close", "edit"])
