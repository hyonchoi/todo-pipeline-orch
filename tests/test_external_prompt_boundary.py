"""The delimited external-agent prompt carries only the work instruction.

TPO splits every worker card into two halves on purpose: a delegation block
addressed to the Hermes dispatcher, and a delimited block Hermes passes
verbatim to the external client. The result-metadata template is dispatcher
instrumentation -- it names ``metadata.tpo_result``, a Hermes concept -- so it
belongs on the dispatcher side of that boundary. When it leaks inside the
delimiters, the external client receives the phase profile's prompt plus a JSON
schema addressed to somebody else, and a failure can no longer be attributed to
the profile under test.
"""
from types import SimpleNamespace

import pytest

from hermes_pipeline.result_contract import (
    RESULT_TEMPLATE_HEADING,
    render_result_template,
)

# Every marker that only the result-metadata template introduces. None of them
# may appear inside the delimited block for any card kind.
TEMPLATE_MARKERS = (
    RESULT_TEMPLATE_HEADING,
    "tpo_result",
    "```json",
    "schema_version",
    "expected_parent_sha",
    "resulting_head_sha",
    "task_commit_sha",
    "changed_files",
    "step_key",
    "verdict",
)


def _split_card_body(body: str) -> tuple[str, str]:
    """Return ``(dispatcher_half, delimited_prompt)`` for one card body."""
    assert body.count("BEGIN EXTERNAL AGENT PROMPT") == 1
    assert body.count("END EXTERNAL AGENT PROMPT") == 1
    dispatcher, _, rest = body.partition("BEGIN EXTERNAL AGENT PROMPT\n")
    delimited, _, _ = rest.partition("END EXTERNAL AGENT PROMPT")
    return dispatcher, delimited


def _assert_prompt_is_clean(delimited: str) -> None:
    for marker in TEMPLATE_MARKERS:
        assert marker not in delimited, f"{marker!r} leaked into the external prompt"


def _plan_phases(tmp_path):
    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "requires_plan: true\n"
        "phases:\n"
        "  - phase_key: development\n"
        "    name: Development\n"
        "    prompt: implement legacy plan\n"
        "    tools: Read,Write,Edit,Bash\n"
        "    turns: 20\n"
        "    compile_plan_tasks: true\n"
    )
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "plan.md").write_text(
        "```json tpo-plan\n"
        '{"schema_version":1,"todo_id":"TODO-41","tasks":['
        '{"id":"task-1","title":"First","instructions":"Exact first instruction.",'
        '"acceptance_criteria":["First exact criterion."],'
        '"verification":["uv run pytest tests/test_first.py"],'
        '"commit_message":"feat: first"}]}'
        "\n```\n"
    )
    return phases_path


def _prepared_plan_worker(tmp_path):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases

    return prepare_todo_phases(
        todo_id="TODO-41", tick_id="01TICK", board_slug="demo",
        phases_path=_plan_phases(tmp_path), prompt_client="codex",
        plan_path="docs/plan.md", project_dir=tmp_path,
    )[0]


def test_plan_worker_delimited_prompt_is_exactly_the_plan_task_prompt(tmp_path):
    """A Plan task's card delivers the Plan's own words and nothing else."""
    body = _prepared_plan_worker(tmp_path).body
    dispatcher, delimited = _split_card_body(body)

    _assert_prompt_is_clean(delimited)
    assert delimited == (
        "Pipeline context:\n"
        "- todo_id: TODO-41\n"
        "- tick_id: 01TICK\n"
        "- project_slug: demo\n"
        "Work on TODO-41 ONLY. Do not pick a different TODO.\n\n"
        "Plan (execution authority): docs/plan.md\n\n"
        "Implement Plan task task-1: First\n\n"
        "Instructions:\nExact first instruction.\n\n"
        "Acceptance criteria:\n- First exact criterion.\n\n"
        "Verification:\n- uv run pytest tests/test_first.py\n\n"
        "Required commit message: feat: first\n"
        "Complete only this task using red-green-refactor TDD.\n"
    )
    # The template did not vanish: it moved to the dispatcher's half, where
    # test_kanban_tasks::test_plan_worker_card_publishes_the_result_metadata_template
    # asserts the exact rendered object.
    assert RESULT_TEMPLATE_HEADING in dispatcher


def test_profile_phase_delimited_prompt_is_exactly_the_profile_prompt(tmp_path):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_5_review\n"
        "    name: Review\n"
        "    prompt: 'Review the branch. Report defects.'\n"
        "    tools: Read,Bash\n"
        "    turns: 5\n"
    )

    prepared = prepare_todo_phases(
        todo_id="TODO-41", tick_id="01TICK", board_slug="demo",
        phases_path=phases_path, prompt_client="claude", project_dir=tmp_path,
    )

    _, delimited = _split_card_body(prepared[0].body)
    _assert_prompt_is_clean(delimited)
    assert delimited.endswith("Review the branch. Report defects.\n")


def _reconciler_registration(tmp_path):
    return SimpleNamespace(
        todo_id="TODO-42", worktree=tmp_path, assignee="implementer",
        review_assignee="reviewer", prompt_client="codex", branch="feat/x",
        manifest=SimpleNamespace(tasks=(SimpleNamespace(id="task-1"),)),
    )


def _created_card_body(mocker, tmp_path, run):
    """Compose one reconciler card body through the real ``_create_task``."""
    from hermes_pipeline import review_reconciliation

    (tmp_path / ".hermes" / "runs" / "01TICK").mkdir(parents=True, exist_ok=True)
    mocker.patch.object(
        review_reconciliation, "_find_task_id_in_snapshot", return_value=None
    )
    subprocess_run = mocker.patch.object(
        review_reconciliation.subprocess, "run",
        return_value=SimpleNamespace(returncode=0, stdout='{"id": "t_12345678"}'),
    )
    run()
    cmd = subprocess_run.call_args.args[0]
    return cmd[cmd.index("--body") + 1]


def test_review_card_body_keeps_the_template_outside_the_delimited_prompt(
    tmp_path, mocker
):
    """Composed end to end: the review card the reconciler really creates."""
    from hermes_pipeline.review_reconciliation import _ensure_initial_review

    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    body = _created_card_body(
        mocker, tmp_path,
        lambda: _ensure_initial_review(
            project_dir=tmp_path,
            tasks={"plan:task-1": SimpleNamespace(task_id="worker-1", status="done")},
            registration=_reconciler_registration(tmp_path),
            tenant="demo", tick_id="01TICK",
        ),
    )

    dispatcher, delimited = _split_card_body(body)
    _assert_prompt_is_clean(delimited)
    assert "fresh, independent, read-only" in delimited
    assert render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
        section="review", pinned_head_sha="a" * 40, allow_no_changes=True,
    ) in dispatcher


@pytest.mark.parametrize("step_key", ["review:0", "re-review:3"])
def test_review_prompts_pass_their_template_beside_the_prompt(
    tmp_path, mocker, step_key
):
    from hermes_pipeline.review_reconciliation import (
        _ensure_initial_review,
        _ensure_rereview,
    )

    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task", return_value="t_1"
    )
    if step_key == "review:0":
        mocker.patch(
            "hermes_pipeline.review_reconciliation._implementation_head",
            return_value="a" * 40,
        )
        _ensure_initial_review(
            project_dir=tmp_path,
            tasks={"plan:task-1": SimpleNamespace(task_id="worker-1", status="done")},
            registration=_reconciler_registration(tmp_path),
            tenant="demo", tick_id="01TICK",
        )
    else:
        _ensure_rereview(
            project_dir=tmp_path, round_number=3, fix_id="fix-id",
            head_sha="a" * 40, registration=_reconciler_registration(tmp_path),
            tenant="demo", tick_id="01TICK", tasks={},
        )

    _assert_prompt_is_clean(create.call_args.kwargs["prompt"])
    assert create.call_args.kwargs["result_template"] == render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key=step_key,
        section="review", pinned_head_sha="a" * 40, allow_no_changes=True,
    )


def test_review_fix_card_passes_its_template_beside_the_prompt(tmp_path, mocker):
    from hermes_pipeline.review_reconciliation import _ensure_round

    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task", return_value="t_1"
    )
    _ensure_round(
        project_dir=tmp_path, round_number=1, parent="review-id",
        registration=_reconciler_registration(tmp_path),
        tenant="demo", tick_id="01TICK", tasks={},
        findings=({"priority": "P1", "location": "a.py:1",
                   "failure_scenario": "x", "recommendation": "y"},),
    )

    _assert_prompt_is_clean(create.call_args.kwargs["prompt"])
    assert create.call_args.kwargs["result_template"] == render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review-fix:1",
    )


def test_delivery_card_passes_its_template_beside_the_prompt(tmp_path, mocker):
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    (state / "runs" / "01TICK" / "accepted-review-head").write_text("a" * 40)
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-1", worktree=tmp_path, branch="feat/native",
            assignee="worker", prompt_client="codex",
        ),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={"review:0": SimpleNamespace(task_id="review-id", status="done")},
    )
    mocker.patch("hermes_pipeline.todos_completion._git", return_value="a" * 40)
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")

    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK",
        repo="acme/repo",
    )

    _assert_prompt_is_clean(create.call_args.kwargs["prompt"])
    assert create.call_args.kwargs["result_template"] == render_result_template(
        tick_id="01TICK", todo_id="TODO-1", step_key="finish",
        section="delivery", pinned_head_sha="a" * 40, branch="feat/native",
        allow_no_changes=True,
    )


def test_result_template_asks_only_for_dispatcher_observable_facts():
    """The dispatcher cannot learn the client's session id or TDD commands."""
    template = render_result_template(
        tick_id="01TICK", todo_id="TODO-1", step_key="plan:task-1",
    )

    assert "external_session_id" not in template
    assert '"tdd"' not in template
    assert "red" not in template.split("```json")[1].split("```")[0]
