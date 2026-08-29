from types import SimpleNamespace

from hermes_pipeline.result_contract import ReviewEvidence
from hermes_pipeline.review_reconciliation import (
    REVIEW_ACCEPTANCE_KEY,
    _ensure_initial_review,
    _ensure_round,
    reconcile_reviews,
)


def _registration(tmp_path):
    return SimpleNamespace(
        todo_id="TODO-42",
        worktree=tmp_path,
        assignee="implementer",
        review_assignee="reviewer",
        prompt_client="codex",
        manifest=SimpleNamespace(tasks=(SimpleNamespace(id="task-1"),)),
    )


def _task(task_id, status="done"):
    return SimpleNamespace(task_id=task_id, status=status)


def test_initial_review_is_fresh_role_and_persistent_gate(tmp_path, mocker):
    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task",
        side_effect=["review-id", "acceptance-id"],
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )

    _ensure_initial_review(
        project_dir=tmp_path,
        tasks={"validate:task-1": _task("validation")},
        registration=_registration(tmp_path), tenant="demo", tick_id="01TICK",
    )

    assert create.call_args_list[0].kwargs["key"] == "review:0"
    assert create.call_args_list[0].kwargs["assignee"] == "reviewer"
    assert "fresh, independent, read-only" in create.call_args_list[0].kwargs["prompt"]
    assert create.call_args_list[1].kwargs["key"] == REVIEW_ACCEPTANCE_KEY
    assert create.call_args_list[1].kwargs["gate"] is True


def test_partial_round_registration_retry_reuses_barrier_and_defers_rereview(
    tmp_path, mocker
):
    import pytest

    registration = _registration(tmp_path)
    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task",
        side_effect=["barrier", RuntimeError("crash")],
    )
    with pytest.raises(RuntimeError, match="crash"):
        _ensure_round(
            project_dir=tmp_path,
            round_number=1, parent="review", registration=registration,
            tenant="demo", tick_id="01TICK", tasks={}, findings=(),
        )
    assert [call.kwargs["key"] for call in create.call_args_list] == [
        "review:1", "review-fix:1"
    ]

    create.reset_mock()
    create.side_effect = ["fix", "validation"]
    complete = mocker.patch(
        "hermes_pipeline.review_reconciliation.complete_todo_kanban_task",
        return_value=True,
    )
    _ensure_round(
        project_dir=tmp_path,
        round_number=1, parent="review", registration=registration,
        tenant="demo", tick_id="01TICK",
        tasks={"review:1": _task("barrier", "blocked")}, findings=(),
    )
    assert [call.kwargs["key"] for call in create.call_args_list] == [
        "review-fix:1", "fix-validation:1"
    ]
    assert all(call.kwargs["key"] != "re-review:1" for call in create.call_args_list)
    complete.assert_called_once_with("demo", "barrier")


def test_timeout_during_initial_review_create_is_retryable_and_recovers_by_key(
    tmp_path, mocker
):
    import subprocess

    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    registration = _registration(tmp_path)
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=registration,
    )
    validation_tasks = {"validate:task-1": _task("validation")}
    get_tasks = mocker.patch(
        "hermes_pipeline.review_reconciliation.get_todo_kanban_tasks",
        return_value=validation_tasks,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    find = mocker.patch(
        "hermes_pipeline.review_reconciliation._find_task_id_in_snapshot",
        return_value=None,
    )
    run = mocker.patch(
        "hermes_pipeline.review_reconciliation.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["hermes"], 30),
    )
    mocker.patch("hermes_pipeline.review_reconciliation._block_gate_task")
    needs_input = mocker.patch(
        "hermes_pipeline.review_reconciliation._mark_gate_needs_input"
    )

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    marker = state / "runs" / "01TICK" / "pending-review-create.json"
    assert '"step_key": "review:0"' in marker.read_text()
    needs_input.assert_not_called()

    get_tasks.side_effect = [
        validation_tasks,
        {
            **validation_tasks,
            "review:0": _task("t_11111111", "todo"),
            REVIEW_ACCEPTANCE_KEY: _task("t_22222222", "blocked"),
        },
    ]
    find.side_effect = ["t_11111111", "t_22222222"]
    run.reset_mock()

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert not marker.exists()
    run.assert_not_called()
    needs_input.assert_not_called()


def test_malformed_success_mid_round_recovers_partial_chain_without_escalation(
    tmp_path, mocker
):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    registration = _registration(tmp_path)
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=registration,
    )
    base_tasks = {
        "validate:task-1": _task("validation"),
        "review:0": _task("review"),
        REVIEW_ACCEPTANCE_KEY: _task("acceptance", "blocked"),
    }
    get_tasks = mocker.patch(
        "hermes_pipeline.review_reconciliation.get_todo_kanban_tasks",
        return_value=base_tasks,
    )
    mocker.patch("hermes_pipeline.review_reconciliation._ensure_initial_review")
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    finding = {
        "priority": "P2", "location": "x.py:1",
        "failure_scenario": "broken", "recommendation": "fix",
    }
    mocker.patch(
        "hermes_pipeline.review_reconciliation._review_result",
        return_value=SimpleNamespace(review=ReviewEvidence("findings", (finding,))),
    )
    find = mocker.patch(
        "hermes_pipeline.review_reconciliation._find_task_id_in_snapshot",
        side_effect=[None, None],
    )
    run = mocker.patch(
        "hermes_pipeline.review_reconciliation.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=0, stdout='{"id":"t_11111111"}'),
            SimpleNamespace(returncode=0, stdout="not-json"),
        ],
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.complete_todo_kanban_task",
        return_value=True,
    )
    mocker.patch("hermes_pipeline.review_reconciliation._block_gate_task")
    needs_input = mocker.patch(
        "hermes_pipeline.review_reconciliation._mark_gate_needs_input"
    )

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    marker = state / "runs" / "01TICK" / "pending-review-create.json"
    assert '"step_key": "review-fix:1"' in marker.read_text()
    needs_input.assert_not_called()

    get_tasks.return_value = {
        **base_tasks,
        "review:1": _task("t_11111111", "blocked"),
    }
    find.side_effect = ["t_22222222", "t_33333333"]
    run.reset_mock()

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert not marker.exists()
    run.assert_not_called()
    needs_input.assert_not_called()


def test_clean_review_completes_acceptance_without_round_cards(tmp_path, mocker):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    registration = _registration(tmp_path)
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=registration,
    )
    tasks = {
        "validate:task-1": _task("validation"),
        "review:0": _task("review"),
        REVIEW_ACCEPTANCE_KEY: _task("acceptance", "blocked"),
    }
    mocker.patch(
        "hermes_pipeline.review_reconciliation.get_todo_kanban_tasks",
        return_value=tasks,
    )
    mocker.patch("hermes_pipeline.review_reconciliation._ensure_initial_review")
    mocker.patch("hermes_pipeline.review_reconciliation._head", return_value="a" * 40)
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._review_result",
        return_value=SimpleNamespace(review=ReviewEvidence("clean", ())),
    )
    complete = mocker.patch(
        "hermes_pipeline.review_reconciliation.complete_todo_kanban_task",
        return_value=True,
    )
    create = mocker.patch("hermes_pipeline.review_reconciliation._create_task")

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    complete.assert_called_once_with("demo", "acceptance")
    create.assert_not_called()


def test_fifth_findings_blocks_gate_and_creates_no_sixth_round(tmp_path, mocker):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    registration = _registration(tmp_path)
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=registration,
    )
    tasks = {
        "validate:task-1": _task("validation"),
        "review:0": _task("review"),
        REVIEW_ACCEPTANCE_KEY: _task("acceptance", "blocked"),
    }
    for round_number in range(1, 6):
        tasks[f"review-fix:{round_number}"] = _task(f"fix-{round_number}")
        tasks[f"fix-validation:{round_number}"] = _task(f"validation-{round_number}")
        tasks[f"re-review:{round_number}"] = _task(f"rereview-{round_number}")
    mocker.patch(
        "hermes_pipeline.review_reconciliation.get_todo_kanban_tasks",
        return_value=tasks,
    )
    mocker.patch("hermes_pipeline.review_reconciliation._ensure_initial_review")
    mocker.patch("hermes_pipeline.review_reconciliation._head", return_value="a" * 40)
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    mocker.patch("hermes_pipeline.review_reconciliation._ensure_rereview")
    finding = {
        "priority": "P2", "location": "x.py:1",
        "failure_scenario": "token=super-secret-value", "recommendation": "fix it",
    }
    mocker.patch(
        "hermes_pipeline.review_reconciliation._review_result",
        return_value=SimpleNamespace(review=ReviewEvidence("findings", (finding,))),
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.parse_worker_result",
        return_value=SimpleNamespace(
            git=SimpleNamespace(resulting_head_sha="a" * 40),
            review=ReviewEvidence("findings", (finding,)),
        ),
    )
    mocker.patch("hermes_pipeline.review_reconciliation.verify_worker_git_result")
    mocker.patch("hermes_pipeline.review_reconciliation.verify_worker_git_topology")
    block = mocker.patch("hermes_pipeline.review_reconciliation._mark_gate_needs_input")
    create = mocker.patch("hermes_pipeline.review_reconciliation._create_task")

    assert not reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    create.assert_not_called()
    reason = block.call_args.args[1]
    assert "limit reached" in reason
    assert "super-secret-value" not in reason


def test_reconcile_reviews_forwards_repo_to_registration_loader(tmp_path, mocker):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    load = mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=SimpleNamespace(manifest=None),
    )

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", repo="acme/repo"
    )
    assert load.call_args.kwargs["repo"] == "acme/repo"
