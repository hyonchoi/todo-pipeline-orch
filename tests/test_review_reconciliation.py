from types import SimpleNamespace

from hermes_pipeline.result_contract import ReviewEvidence
from hermes_pipeline.review_reconciliation import (
    _ensure_initial_review,
    _ensure_round,
    reconcile_reviews,
)


def _registration(tmp_path, task_ids=("task-1",)):
    return SimpleNamespace(
        todo_id="TODO-42",
        worktree=tmp_path,
        assignee="implementer",
        review_assignee="reviewer",
        prompt_client="codex",
        manifest=SimpleNamespace(
            tasks=tuple(SimpleNamespace(id=task_id) for task_id in task_ids)
        ),
    )


def _task(task_id, status="done"):
    return SimpleNamespace(task_id=task_id, status=status)


def test_initial_review_is_the_only_card_the_reconciler_creates(tmp_path, mocker):
    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task",
        side_effect=["review-id"],
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )

    _ensure_initial_review(
        project_dir=tmp_path,
        tasks={"plan:task-1": _task("worker-1"), "plan:task-2": _task("worker-2")},
        registration=_registration(tmp_path, ("task-1", "task-2")),
        tenant="demo", tick_id="01TICK",
    )

    assert create.call_args_list[0].kwargs["key"] == "review:0"
    assert create.call_args_list[0].kwargs["assignee"] == "reviewer"
    # The last Plan worker is the review's parent.
    assert create.call_args_list[0].kwargs["parent"] == "worker-2"
    assert "fresh, independent, read-only" in create.call_args_list[0].kwargs["prompt"]
    # Nothing else: TPO synthesizes no card to stand for its own acceptance.
    assert len(create.call_args_list) == 1


def test_initial_review_defers_until_every_plan_worker_is_done(tmp_path, mocker):
    create = mocker.patch("hermes_pipeline.review_reconciliation._create_task")
    head = mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )

    _ensure_initial_review(
        project_dir=tmp_path,
        tasks={
            "plan:task-1": _task("worker-1"),
            "plan:task-2": _task("worker-2", "running"),
        },
        registration=_registration(tmp_path, ("task-1", "task-2")),
        tenant="demo", tick_id="01TICK",
    )

    create.assert_not_called()
    head.assert_not_called()


def test_implementation_head_revalidates_the_chain_without_gate_cards(tmp_path, mocker):
    from hermes_pipeline.review_reconciliation import _implementation_head

    registration = SimpleNamespace(
        todo_id="TODO-42",
        worktree=tmp_path,
        base_sha="a" * 40,
        manifest=SimpleNamespace(tasks=(SimpleNamespace(id="task-1", acceptance_criteria=()),)),
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._show_task_payload",
        return_value={"payload": True},
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.parse_worker_result",
        return_value=SimpleNamespace(
            git=SimpleNamespace(resulting_head_sha="b" * 40)
        ),
    )
    topology = mocker.patch(
        "hermes_pipeline.review_reconciliation.verify_worker_git_topology"
    )

    head = _implementation_head(
        tasks={"plan:task-1": _task("worker-1")},
        registration=registration,
        tick_id="01TICK",
    )

    assert head == "b" * 40
    assert topology.call_args.kwargs["expected_parent_sha"] == "a" * 40


def test_round_registers_one_worker_card_parented_on_the_review(tmp_path, mocker):
    registration = _registration(tmp_path)
    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task", return_value="fix",
    )
    _ensure_round(
        project_dir=tmp_path,
        round_number=1, parent="review", registration=registration,
        tenant="demo", tick_id="01TICK", tasks={}, findings=(),
    )
    assert [call.kwargs["key"] for call in create.call_args_list] == ["review-fix:1"]
    assert create.call_args.kwargs["parent"] == "review"
    assert create.call_args.kwargs["assignee"] == "implementer"

    # Idempotent: an existing fix card registers nothing further.
    create.reset_mock()
    _ensure_round(
        project_dir=tmp_path,
        round_number=1, parent="review", registration=registration,
        tenant="demo", tick_id="01TICK",
        tasks={"review-fix:1": _task("fix", "running")}, findings=(),
    )
    create.assert_not_called()


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
    validation_tasks = {"plan:task-1": _task("worker-1")}
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

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    marker = state / "runs" / "01TICK" / "pending-review-create.json"
    assert '"step_key": "review:0"' in marker.read_text()

    get_tasks.side_effect = [
        validation_tasks,
        {**validation_tasks, "review:0": _task("t_11111111", "todo")},
    ]
    find.side_effect = ["t_11111111"]
    run.reset_mock()

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert not marker.exists()
    run.assert_not_called()


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
        "plan:task-1": _task("worker-1"),
        "review:0": _task("review"),
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
        side_effect=[None],
    )
    run = mocker.patch(
        "hermes_pipeline.review_reconciliation.subprocess.run",
        side_effect=[SimpleNamespace(returncode=0, stdout="not-json")],
    )

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    marker = state / "runs" / "01TICK" / "pending-review-create.json"
    assert '"step_key": "review-fix:1"' in marker.read_text()

    # The board snapshot still lags, so the retry re-enters the round and
    # recovers the created card by its idempotency key instead of creating one.
    find.side_effect = ["t_22222222"]
    run.reset_mock()

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert not marker.exists()
    run.assert_not_called()


def test_clean_review_persists_the_accepted_head_and_creates_no_cards(tmp_path, mocker):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    registration = _registration(tmp_path)
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=registration,
    )
    tasks = {"plan:task-1": _task("worker-1"), "review:0": _task("review")}
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
    create = mocker.patch("hermes_pipeline.review_reconciliation._create_task")

    assert reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    # The accepted head on disk is the only acceptance record; no card is made
    # or completed to mirror it.
    assert (
        state / "runs" / "01TICK" / "accepted-review-head"
    ).read_text().strip() == "a" * 40
    create.assert_not_called()


def test_fifth_findings_stalls_and_creates_no_sixth_round(tmp_path, mocker, caplog):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    registration = _registration(tmp_path)
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=registration,
    )
    tasks = {"plan:task-1": _task("worker-1"), "review:0": _task("review")}
    for round_number in range(1, 6):
        tasks[f"review-fix:{round_number}"] = _task(f"fix-{round_number}")
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
    create = mocker.patch("hermes_pipeline.review_reconciliation._create_task")

    with caplog.at_level("ERROR"):
        assert not reconcile_reviews(
            project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
        )
    create.assert_not_called()
    assert "limit reached" in caplog.text
    assert "super-secret-value" not in caplog.text


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


def _worker_card_registration(tmp_path):
    return SimpleNamespace(
        todo_id="TODO-42",
        worktree=tmp_path,
        assignee="implementer",
        review_assignee="reviewer",
        prompt_client="codex",
        manifest=SimpleNamespace(tasks=(SimpleNamespace(id="task-1"),)),
    )


def test_review_card_publishes_the_full_result_metadata_template(tmp_path, mocker):
    from hermes_pipeline.result_contract import render_result_template

    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task",
        side_effect=["review-id"],
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )

    _ensure_initial_review(
        project_dir=tmp_path,
        tasks={"plan:task-1": _task("worker-1")},
        registration=_worker_card_registration(tmp_path),
        tenant="demo", tick_id="01TICK",
    )

    prompt = create.call_args_list[0].kwargs["prompt"]
    assert render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
        section="review", pinned_head_sha="a" * 40, allow_no_changes=True,
    ) in prompt


def test_rereview_card_publishes_its_own_step_key_template(tmp_path, mocker):
    from hermes_pipeline.result_contract import render_result_template
    from hermes_pipeline.review_reconciliation import _ensure_rereview

    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task", return_value="rereview-id"
    )

    _ensure_rereview(
        project_dir=tmp_path, round_number=2, fix_id="fix-id",
        head_sha="d" * 40, registration=_worker_card_registration(tmp_path),
        tenant="demo", tick_id="01TICK", tasks={},
    )
    assert create.call_args.kwargs["parent"] == "fix-id"

    assert render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="re-review:2",
        section="review", pinned_head_sha="d" * 40, allow_no_changes=True,
    ) in create.call_args.kwargs["prompt"]


def test_review_fix_card_publishes_the_committing_worker_template(tmp_path, mocker):
    from hermes_pipeline.result_contract import render_result_template

    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task",
        side_effect=["fix-id"],
    )

    _ensure_round(
        project_dir=tmp_path, round_number=1, parent="review-id",
        registration=_worker_card_registration(tmp_path),
        tenant="demo", tick_id="01TICK", tasks={},
        findings=({"priority": "P1", "location": "a.py:1",
                   "failure_scenario": "x", "recommendation": "y"},),
    )

    fix_call = next(
        call for call in create.call_args_list
        if call.kwargs["key"] == "review-fix:1"
    )
    assert render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review-fix:1",
    ) in fix_call.kwargs["prompt"]
    # The fix card is the round's only card.
    assert len(create.call_args_list) == 1
