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

from hermes_pipeline.phases import IMPLEMENTATION_KEY
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


def _write_plan(tmp_path):
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


def _prepared_implementation_card(tmp_path):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    from hermes_pipeline.phases import resolve_profile_phases_path

    _write_plan(tmp_path)
    return prepare_todo_phases(
        todo_id="TODO-41", tick_id="01TICK", board_slug="demo",
        phases_path=resolve_profile_phases_path("native-sdd"),
        prompt_client="codex",
        plan_path="docs/plan.md", project_dir=tmp_path,
    )


def test_implementation_card_delimited_prompt_is_exactly_the_profile_prompt(tmp_path):
    """The Plan's implementation card delivers ``phase_4_development`` verbatim.

    This is the byte-exact guard on the defect that motivated deleting the
    per-Plan-task fan-out: the compiled cards passed ``""`` as the template and
    appended TPO-authored prose, so every instruction the profile's phase_4
    prompt carries -- re-open the Plan and exit nonzero, preserve unrelated
    tracked and untracked work, stage explicit files or hunks, never commit a
    red task -- reached no agent at all. Equality, not ``in``: appending or
    prepending anything inside the delimited block must fail this test.
    """
    prepared = _prepared_implementation_card(tmp_path)

    assert [card.phase_key for card in prepared] == ["phase_4_development"]
    dispatcher, delimited = _split_card_body(prepared[0].body)

    _assert_prompt_is_clean(delimited)
    assert delimited == _rendered_profile_prompt(
        _profile_phase("phase_4_development"), todo_id="TODO-41",
        facts={}, plan_hash=None,
    )
    # The template did not vanish: it moved to the dispatcher's half, where
    # test_kanban_tasks::test_implementation_card_publishes_the_result_metadata_template
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
        profile="native-sdd", plan_hash="f" * 64,
        plan_reference=SimpleNamespace(value="docs/plan.md"),
        manifest=SimpleNamespace(tasks=(SimpleNamespace(id="task-1"),)),
    )


def _profile_phase(phase_key):
    from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

    return next(
        phase for phase in load_phases(resolve_profile_phases_path("native-sdd"))
        if phase.phase_key == phase_key
    )


def _rendered_profile_prompt(phase, *, todo_id, facts, plan_hash="f" * 64):
    """The profile's prompt as the pipeline renders it, built independently here.

    ``plan_hash=None`` is the legacy-path case: a Plan referenced by tracked
    path carries no pinned hash, so the header states the path alone.
    """
    header = (
        "Pipeline context:\n"
        f"- todo_id: {todo_id}\n"
        "- tick_id: 01TICK\n"
        "- project_slug: demo\n"
        + "".join(f"- {key}: {value}\n" for key, value in facts.items())
        + f"Work on {todo_id} ONLY. Do not pick a different TODO.\n\n"
        "Plan (execution authority): docs/plan.md\n"
        + (
            f"Plan SHA-256: {plan_hash}\n"
            "Before using the Plan, verify its SHA-256 matches exactly; "
            "fail closed on drift.\n"
            if plan_hash
            else ""
        )
        + "\n"
    )
    body = phase.prompt.format(
        todo_id=todo_id, tick_id="01TICK", project_slug="demo",
        plan_path="docs/plan.md", agent_product="Codex", skill_prefix="$",
        superpowers_skill_prefix="$superpowers:",
    )
    return header + body


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


def test_review_card_delimited_prompt_is_exactly_the_profile_prompt(tmp_path, mocker):
    """Composed end to end: the review card the reconciler really creates.

    The delimited block is what Hermes hands the external client verbatim, so
    it must be the phase profile's ``phase_5_review`` prompt and nothing else.
    A TPO-authored review instruction in here is the exact failure this whole
    change exists to remove: the live harness cannot test a profile whose words
    never reach the reviewer.
    """
    from hermes_pipeline.review_reconciliation import _ensure_initial_review

    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    body = _created_card_body(
        mocker, tmp_path,
        lambda: _ensure_initial_review(
            project_dir=tmp_path,
            tasks={IMPLEMENTATION_KEY: SimpleNamespace(task_id="worker-1", status="done")},
            registration=_reconciler_registration(tmp_path),
            tenant="demo", tick_id="01TICK",
        ),
    )

    dispatcher, delimited = _split_card_body(body)
    _assert_prompt_is_clean(delimited)
    assert delimited == _rendered_profile_prompt(
        _profile_phase("phase_5_review"), todo_id="TODO-42",
        facts={"reviewed_head_sha": "a" * 40, "branch": "feat/x"},
    )
    assert render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
        allow_no_changes=True,
    ) in dispatcher


def test_review_card_passes_its_template_beside_the_prompt(tmp_path, mocker):
    from hermes_pipeline.review_reconciliation import _ensure_initial_review

    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task", return_value="t_1"
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    _ensure_initial_review(
        project_dir=tmp_path,
        tasks={IMPLEMENTATION_KEY: SimpleNamespace(task_id="worker-1", status="done")},
        registration=_reconciler_registration(tmp_path),
        tenant="demo", tick_id="01TICK",
    )

    _assert_prompt_is_clean(create.call_args.kwargs["prompt"])
    assert create.call_args.kwargs["result_template"] == render_result_template(
        tick_id="01TICK", todo_id="TODO-42", step_key="review:0",
        allow_no_changes=True,
    )


def _delivery_card_body(tmp_path, mocker):
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True, exist_ok=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    (state / "runs" / "01TICK" / "accepted-review-head").write_text("a" * 40)
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-1", worktree=tmp_path, branch="feat/native",
            assignee="worker", prompt_client="codex", profile="native-sdd",
            plan_hash="f" * 64,
            plan_reference=SimpleNamespace(value="docs/plan.md"),
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
    return create


def _delivery_card_real_body(tmp_path, mocker):
    """Compose the finish card body through the real ``_create_task``."""
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True, exist_ok=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    (state / "runs" / "01TICK" / "accepted-review-head").write_text("a" * 40)
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-1", worktree=tmp_path, branch="feat/native",
            assignee="worker", prompt_client="codex", profile="native-sdd",
            plan_hash="f" * 64,
            plan_reference=SimpleNamespace(value="docs/plan.md"),
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
    return _created_card_body(
        mocker, tmp_path,
        lambda: reconcile_todo_completion(
            project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK",
            repo="acme/repo",
        ),
    )


def test_finish_card_delimited_prompt_is_exactly_the_profile_prompt(tmp_path, mocker):
    """``phase_8_finish_branch``'s own words reach the delivery worker.

    Asserted on the SPLIT CARD BODY, not on ``_create_task``'s ``prompt``
    keyword. The boundary this file exists to protect is created inside
    ``_create_task`` -- it is what wraps the prompt in the delimiters and keeps
    the result template on the dispatcher's side -- so checking the argument
    going in tests the caller and leaves the composition untested. The review
    card was the only one whose real boundary was ever exercised.
    """
    dispatcher, delimited = _split_card_body(_delivery_card_real_body(tmp_path, mocker))

    _assert_prompt_is_clean(delimited)
    assert delimited == _rendered_profile_prompt(
        _profile_phase("phase_8_finish_branch"), todo_id="TODO-1",
        facts={"accepted_review_head_sha": "a" * 40, "branch": "feat/native"},
    )
    assert RESULT_TEMPLATE_HEADING in dispatcher
    assert render_result_template(
        tick_id="01TICK", todo_id="TODO-1", step_key="finish",
        section="delivery", branch="feat/native", allow_no_changes=True,
    ) in dispatcher


def test_delivery_card_passes_its_template_beside_the_prompt(tmp_path, mocker):
    create = _delivery_card_body(tmp_path, mocker)

    _assert_prompt_is_clean(create.call_args.kwargs["prompt"])
    assert create.call_args.kwargs["result_template"] == render_result_template(
        tick_id="01TICK", todo_id="TODO-1", step_key="finish",
        section="delivery", branch="feat/native", allow_no_changes=True,
    )


def test_result_template_asks_only_for_dispatcher_observable_facts():
    """The dispatcher cannot learn the client's session id or TDD commands."""
    template = render_result_template(
        tick_id="01TICK", todo_id="TODO-1", step_key=IMPLEMENTATION_KEY,
    )

    assert "external_session_id" not in template
    assert '"tdd"' not in template
    assert "red" not in template.split("```json")[1].split("```")[0]


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("manifest", [False, True])
@pytest.mark.parametrize("mode", ["inherit", "delegated"])
def test_native_policy_initial_worker_cards(tmp_path, client, manifest, mode):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    from hermes_pipeline.phases import resolve_profile_phases_path

    _write_plan(tmp_path)
    if not manifest:
        (tmp_path / "docs/plan.md").write_text("# Legacy Plan\nImplement safely.\n")
    kwargs = dict(
        todo_id="TODO-41", tick_id="01TICK", board_slug="demo",
        phases_path=resolve_profile_phases_path("native-sdd"),
        prompt_client=client, plan_path="docs/plan.md", project_dir=tmp_path,
    )
    baseline = prepare_todo_phases(**kwargs)
    cards = prepare_todo_phases(**kwargs, profile_name="native-sdd", agent_policy_mode=mode)
    assert [c.phase_key for c in cards] == (
        ["phase_4_development"] if manifest else
        ["phase_4_development", "phase_5_review", "phase_8_finish_branch"]
    )
    for card, original in zip(cards, baseline, strict=True):
        dispatcher, payload = _split_card_body(card.body)
        original_dispatcher, original_payload = _split_card_body(original.body)
        assert dispatcher == original_dispatcher
        prefix = "AGENT-POLICY-MODE: delegated\n\n" if mode == "delegated" else ""
        assert payload == prefix + original_payload


@pytest.mark.parametrize("declaration", [
    "AGENT-POLICY-MODE: delegated", "AGENT-POLICY-MODE: inherit",
    "AGENT-POLICY-MODE: nonsense", "AGENT-POLICY-MODE:",
    "AGENT-POLICY-MODE delegated", "AGENT-POLICY-MODE nonsense",
    "> `AGENT-POLICY-MODE delegated`",
    " AGENT-POLICY-MODE : delegated ", "\ufeffAGENT-POLICY-MODE: delegated",
    "> `AGENT-POLICY-MODE: delegated`", "  >> ``AGENT-POLICY-MODE: inherit`` ",
    "```\nAGENT-POLICY-MODE: delegated\n```",
    "AGENT-POLICY-MODE: delegated\nAGENT-POLICY-MODE: delegated",
])
@pytest.mark.parametrize("mode", ["inherit", "delegated"])
def test_native_policy_rejects_standalone_declarations_before_card_creation(tmp_path, declaration, mode):
    import json

    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    from hermes_pipeline.phases import PhasePromptRenderError

    path = tmp_path / "phases.yaml"
    path.write_text("phases:\n  - phase_key: phase_4_development\n    name: Work\n    prompt: "
                    + json.dumps(declaration) + "\n")
    with pytest.raises(PhasePromptRenderError, match="agent_policy_declaration_conflict") as exc:
        prepare_todo_phases(todo_id="TODO-41", tick_id="01TICK", board_slug="demo",
                            phases_path=path, profile_name="native-sdd", agent_policy_mode=mode)
    assert declaration not in str(exc.value)


@pytest.mark.parametrize("profile", [None, "gstack", "custom", "Native-sdd"])
def test_other_profiles_never_delegate_even_when_globally_opted_in(tmp_path, profile):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    path = tmp_path / "phases.yaml"
    path.write_text("phases:\n  - phase_key: phase_4_development\n    name: Work\n"
                    "    prompt: 'AGENT-POLICY-MODE: existing'\n")
    kwargs = dict(todo_id="TODO-41", tick_id="01TICK", board_slug="demo", phases_path=path)
    assert prepare_todo_phases(**kwargs, profile_name=profile, agent_policy_mode="delegated") == prepare_todo_phases(**kwargs)


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["inherit", "delegated"])
def test_dynamic_review_uses_pinned_policy(tmp_path, mocker, client, mode):
    from hermes_pipeline.review_reconciliation import _ensure_initial_review
    registration = _reconciler_registration(tmp_path)
    registration.prompt_client = client
    registration.agent_policy_mode = mode
    mocker.patch("hermes_pipeline.review_reconciliation._implementation_head", return_value="a" * 40)
    body = _created_card_body(mocker, tmp_path, lambda: _ensure_initial_review(
        project_dir=tmp_path, tasks={IMPLEMENTATION_KEY: SimpleNamespace(task_id="worker", status="done")},
        registration=registration, tenant="demo", tick_id="01TICK"))
    dispatcher, payload = _split_card_body(body)
    assert "AGENT-POLICY-MODE" not in dispatcher
    assert payload.startswith("AGENT-POLICY-MODE: delegated\n\n") == (mode == "delegated")
    assert "Review" in payload or "review" in payload


def test_native_policy_keeps_prose_and_inline_mentions_byte_exact(tmp_path):
    import json

    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    text = ("Explain `AGENT-POLICY-MODE: delegated` without changing it.\n"
            "The policy AGENT-POLICY-MODE: inherit is an inline mention.\n"
            "Shell text: $(false); `false`; $HOME; 'quote'; \\ remains literal.")
    path = tmp_path / "phases.yaml"
    path.write_text("phases:\n  - phase_key: phase_4_development\n    name: Work\n    prompt: "
                    + json.dumps(text) + "\n")
    cards = prepare_todo_phases(todo_id="TODO-41", tick_id="01TICK", board_slug="demo",
                               phases_path=path, profile_name="native-sdd", agent_policy_mode="delegated")
    _, payload = _split_card_body(cards[0].body)
    assert payload.startswith("AGENT-POLICY-MODE: delegated\n\n")
    assert payload.endswith(text + "\n")


@pytest.mark.parametrize("phase_key", ["phase_5_review", "phase_8_finish_branch"])
def test_dynamic_declaration_conflict_prevents_worker_publication(tmp_path, mocker, caplog, phase_key):
    from dataclasses import replace

    from hermes_pipeline import review_reconciliation as review
    from hermes_pipeline import todos_completion as completion
    registration = _reconciler_registration(tmp_path)
    registration.agent_policy_mode = "delegated"
    state = tmp_path / ".hermes"
    run = state / "runs/01TICK"
    run.mkdir(parents=True)
    (run / "registration.json").write_text("{}")
    (run / "accepted-review-head").write_text("a" * 40)
    module = review if phase_key == "phase_5_review" else completion
    mocker.patch.object(module, "load_validated_registration", return_value=registration)
    mocker.patch.object(module, "get_todo_kanban_tasks", return_value={
        IMPLEMENTATION_KEY: SimpleNamespace(task_id="worker", status="done")})
    phase = replace(_profile_phase(phase_key), prompt="> `AGENT-POLICY-MODE: secret-invalid`")
    mocker.patch.object(module, "profile_phase", return_value=("profile.yaml", phase))
    mocker.patch.object(review, "_implementation_head", return_value="a" * 40)
    mocker.patch.object(completion, "_delivery_authority")
    create = mocker.patch.object(module, "_create_task")
    reconcile = review.reconcile_reviews if module is review else completion.reconcile_todo_completion
    assert reconcile(project_dir=tmp_path, state_dir=state, tick_id="01TICK", tenant="demo", repo="acme/repo") is False
    create.assert_not_called()
    assert not (run / "pending-create.json").exists()
    assert "phase_prompt_preparation_failed" in caplog.text
    assert "secret-invalid" not in caplog.text


@pytest.mark.parametrize("mode", ["inherit", "delegated"])
@pytest.mark.parametrize("text", [
    "AGENT-POLICY-MODE is the configuration declaration key.",
    "> AGENT-POLICY-MODE is the configuration declaration key.",
    "`AGENT-POLICY-MODE: delegated` is the documented opt-in.",
    "> `AGENT-POLICY-MODE: delegated` is the documented opt-in.",
    "``AGENT-POLICY-MODE: delegated`` is the documented opt-in.",
    "`AGENT-POLICY-MODE: delegated` is the documented `opt-in`.",
])
def test_native_policy_preserves_leading_inline_code_mentions(tmp_path, mode, text):
    import json

    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    path = tmp_path / "phases.yaml"
    path.write_text("phases:\n  - phase_key: phase_4_development\n    name: Work\n    prompt: "
                    + json.dumps(text) + "\n")
    kwargs = dict(todo_id="TODO-41", tick_id="01TICK", board_slug="demo", phases_path=path)
    original = prepare_todo_phases(**kwargs)[0]
    card = prepare_todo_phases(**kwargs, profile_name="native-sdd", agent_policy_mode=mode)[0]
    dispatcher, payload = _split_card_body(card.body)
    original_dispatcher, original_payload = _split_card_body(original.body)
    assert dispatcher == original_dispatcher
    prefix = "AGENT-POLICY-MODE: delegated\n\n" if mode == "delegated" else ""
    assert payload == prefix + original_payload
