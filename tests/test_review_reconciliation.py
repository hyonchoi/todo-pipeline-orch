import logging
from types import SimpleNamespace

import pytest

from hermes_pipeline.review_reconciliation import (
    _ensure_initial_review,
    reconcile_reviews,
)


def _registration(tmp_path, task_ids=("task-1",)):
    return SimpleNamespace(
        todo_id="TODO-42",
        worktree=tmp_path,
        assignee="implementer",
        review_assignee="reviewer",
        prompt_client="codex",
        branch="todo-42-feature",
        profile="native-sdd",
        plan_hash="f" * 64,
        plan_reference=SimpleNamespace(value="docs/plan.md"),
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
    # Nothing else: TPO synthesizes no card to stand for its own acceptance.
    assert len(create.call_args_list) == 1


def test_review_card_takes_its_tools_turns_and_timeout_from_the_profile(
    tmp_path, mocker
):
    """The profile declares the reviewer's capabilities; TPO must not override them.

    A hardcoded empty tool set is what silently forced TPO's review to be
    read-only, which is the opposite of what ``phase_5_review`` mandates: it
    applies its own findings and commits them.
    """
    from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

    phase = next(
        p for p in load_phases(resolve_profile_phases_path("native-sdd"))
        if p.phase_key == "phase_5_review"
    )
    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task", return_value="review-id"
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )

    _ensure_initial_review(
        project_dir=tmp_path, tasks={"plan:task-1": _task("worker-1")},
        registration=_registration(tmp_path), tenant="demo", tick_id="01TICK",
    )

    kwargs = create.call_args.kwargs
    assert kwargs["tools"] == phase.tools == "Read,Write,Edit,Bash"
    assert kwargs["turns"] == phase.turns == 30
    assert kwargs["timeout"] == phase.timeout == 2400
    assert kwargs["title"] == phase.name


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


def _accepted_review(tmp_path, mocker, *, reviewed_head):
    """Reconcile one done review card whose report ends at ``reviewed_head``."""
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True, exist_ok=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=_registration(tmp_path),
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.get_todo_kanban_tasks",
        return_value={"plan:task-1": _task("worker-1"), "review:0": _task("review")},
    )
    mocker.patch("hermes_pipeline.review_reconciliation._ensure_initial_review")
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._show_task_payload",
        return_value={"payload": True},
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.parse_worker_result",
        return_value=SimpleNamespace(
            git=SimpleNamespace(
                expected_parent_sha="a" * 40, resulting_head_sha=reviewed_head,
                task_commit_sha=reviewed_head, changed_files=(),
            )
        ),
    )
    verify = mocker.patch(
        "hermes_pipeline.review_reconciliation.verify_optional_single_commit"
    )
    create = mocker.patch("hermes_pipeline.review_reconciliation._create_task")
    reconciled = reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    return reconciled, state, verify, create


def test_done_review_persists_the_accepted_head_and_creates_no_cards(tmp_path, mocker):
    """A done review card IS the pass; there is no verdict object to consult."""
    reconciled, state, verify, create = _accepted_review(
        tmp_path, mocker, reviewed_head="a" * 40
    )

    assert reconciled
    assert (
        state / "runs" / "01TICK" / "accepted-review-head"
    ).read_text().strip() == "a" * 40
    create.assert_not_called()
    # The chain anchor is still proved against the recomputed implementation
    # head, so the accepted head can never be a bare worker claim.
    assert verify.call_args.kwargs["expected_parent_sha"] == "a" * 40
    assert verify.call_args.kwargs["require_current"] is True


def test_accepted_head_records_the_head_the_review_fix_commit_left(tmp_path, mocker):
    """``phase_5_review`` commits its own fixes, so the accepted head advances.

    Recording the pre-review head instead would anchor delivery to a head that
    is missing the review's own fix commit, and ``_verify_finish`` would then
    count that fix against the one metadata commit finish is allowed.
    """
    reconciled, state, _, _ = _accepted_review(
        tmp_path, mocker, reviewed_head="b" * 40
    )

    assert reconciled
    assert (
        state / "runs" / "01TICK" / "accepted-review-head"
    ).read_text().strip() == "b" * 40


def test_review_reconciliation_reports_a_rejected_report_as_no_progress(
    tmp_path, mocker
):
    from hermes_pipeline.result_contract import ResultContractError

    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=_registration(tmp_path),
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.get_todo_kanban_tasks",
        return_value={"plan:task-1": _task("worker-1"), "review:0": _task("review")},
    )
    mocker.patch("hermes_pipeline.review_reconciliation._ensure_initial_review")
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._show_task_payload",
        return_value={"payload": True},
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.parse_worker_result",
        side_effect=ResultContractError("malformed_result", "token=super-secret-value"),
    )

    assert not reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert not (state / "runs" / "01TICK" / "accepted-review-head").exists()


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


def test_review_card_delimited_prompt_is_the_profile_prompt_verbatim(tmp_path, mocker):
    """The profile's words reach the reviewer; TPO adds no instruction of its own."""
    from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

    phase = next(
        p for p in load_phases(resolve_profile_phases_path("native-sdd"))
        if p.phase_key == "phase_5_review"
    )
    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task", return_value="review-id"
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )

    _ensure_initial_review(
        project_dir=tmp_path, tasks={"plan:task-1": _task("worker-1")},
        registration=_registration(tmp_path), tenant="demo", tick_id="01TICK",
    )

    prompt = create.call_args.kwargs["prompt"]
    body = phase.prompt.format(
        todo_id="TODO-42", tick_id="01TICK", project_slug="demo",
        plan_path="docs/plan.md", agent_product="Codex", skill_prefix="$",
        superpowers_skill_prefix="$superpowers:",
    )
    # The profile's own text is the tail of the rendered prompt: the pipeline
    # context header precedes it and nothing follows it.
    assert prompt.endswith(body)
    # The per-card facts are header facts, not appended instructions.
    assert f"- reviewed_head_sha: {'a' * 40}\n" in prompt.removesuffix(body)
    assert "- branch: todo-42-feature\n" in prompt.removesuffix(body)


def test_review_head_that_does_not_descend_from_the_chain_is_not_accepted(
    tmp_path, mocker
):
    """The accepted head must be an anchor, never a worker's bare claim.

    ``verify_read_only_review`` used to prove this by demanding a frozen head.
    With the review free to commit, the real verifier runs here unmocked
    against a live repository so an off-mainline head still cannot be blessed.

    The reported head is a REAL commit off the branch mainline, not a
    fabricated SHA. A fabricated one never reaches the ancestry logic at all:
    git cannot resolve it, ``merge-base --is-ancestor`` exits 128, and the
    result is ``git_verification_failed`` -- a broken-git report that would pass
    just as well if every reachability check here were deleted.
    """
    import subprocess

    repo = tmp_path / "run"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "impl.txt").write_text("impl")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "impl"], cwd=repo, check=True,
                   capture_output=True)
    implementation_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
        text=True,
    ).stdout.strip()
    # A real commit whose real parent IS the implementation head, with an
    # honest diff -- but on a branch the run does not deliver, so it is not on
    # HEAD's first-parent mainline.
    subprocess.run(["git", "checkout", "-q", "-b", "elsewhere"], cwd=repo,
                   check=True, capture_output=True)
    (repo / "invented.py").write_text("off the chain")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "off-chain"], cwd=repo, check=True,
                   capture_output=True)
    off_chain = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True,
                   capture_output=True)
    # git resolves it, so the ancestry logic is really what rejects it.
    assert subprocess.run(
        ["git", "cat-file", "-e", off_chain], cwd=repo, capture_output=True
    ).returncode == 0

    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    registration = _registration(repo)
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=registration,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.get_todo_kanban_tasks",
        return_value={"plan:task-1": _task("worker-1"), "review:0": _task("review")},
    )
    mocker.patch("hermes_pipeline.review_reconciliation._ensure_initial_review")
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value=implementation_head,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._show_task_payload",
        return_value={"payload": True},
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.parse_worker_result",
        return_value=SimpleNamespace(
            git=SimpleNamespace(
                expected_parent_sha=implementation_head,
                resulting_head_sha=off_chain, task_commit_sha=off_chain,
                changed_files=("invented.py",),
            )
        ),
    )

    assert not reconcile_reviews(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert not (state / "runs" / "01TICK" / "accepted-review-head").exists()


def test_the_accepted_head_is_written_once_and_never_re_derived(tmp_path, mocker):
    """The anchor cannot be moved by a card report that changed after acceptance.

    ``accepted-review-head`` is what ``_verify_finish`` measures delivery
    against, and its own presence is what relaxes ``require_current`` on the
    review's re-verification. Rewriting it on every tick the review card reads
    ``done`` therefore let the anchor be re-derived from a mutable report under
    the weaker check -- the reference point moving with the thing it pins.
    """
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    accepted = state / "runs" / "01TICK" / "accepted-review-head"
    accepted.write_text("b" * 40 + "\n")

    reconciled, state, _verify, _create = _accepted_review(
        tmp_path, mocker, reviewed_head="c" * 40
    )

    assert not reconciled
    assert accepted.read_text().strip() == "b" * 40


def test_a_re_reported_accepted_head_that_agrees_is_not_an_error(tmp_path, mocker):
    """Write-once must not turn an idempotent re-read into a permanent stall."""
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    (state / "runs" / "01TICK" / "accepted-review-head").write_text("a" * 40 + "\n")

    reconciled, state, verify, _create = _accepted_review(
        tmp_path, mocker, reviewed_head="a" * 40
    )

    assert reconciled
    # And the relaxation the marker's presence grants is the one it always was.
    assert verify.call_args.kwargs["require_current"] is False


def test_the_review_card_is_rendered_from_the_runs_own_pinned_profile(tmp_path, mocker):
    """Resolved from ``registration.profile``, never from a literal profile name.

    A profile switch in the project's config mid-run must not change the run
    already in flight, and a run registered under another profile must get that
    profile's reviewer -- not ``native-sdd``'s.
    """
    from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

    gstack = next(
        p for p in load_phases(resolve_profile_phases_path("gstack"))
        if p.phase_key == "phase_5_review"
    )
    native = next(
        p for p in load_phases(resolve_profile_phases_path("native-sdd"))
        if p.phase_key == "phase_5_review"
    )
    assert gstack.prompt != native.prompt, "fixture no longer distinguishes profiles"

    registration = _registration(tmp_path)
    registration.profile = "gstack"
    create = mocker.patch(
        "hermes_pipeline.review_reconciliation._create_task", return_value="review-id"
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value="a" * 40,
    )

    _ensure_initial_review(
        project_dir=tmp_path, tasks={"plan:task-1": _task("worker-1")},
        registration=registration, tenant="demo", tick_id="01TICK",
    )

    kwargs = create.call_args.kwargs
    assert kwargs["title"] == gstack.name
    assert kwargs["tools"] == gstack.tools
    assert kwargs["turns"] == gstack.turns
    assert kwargs["timeout"] == gstack.timeout
    assert native.prompt.strip() not in kwargs["prompt"]


def test_per_card_facts_reach_the_header_sanitized(tmp_path, mocker):
    """A per-card fact is interpolated into a prompt, so it is display-safe.

    ``context_facts`` are the one part of the header that is not a TPO
    constant, so they are the one part a hostile registration value could use
    to forge extra header lines or leak credential-shaped text into a card
    body.
    """
    from hermes_pipeline.review_reconciliation import (
        profile_phase,
        render_profile_prompt,
    )

    registration = _registration(tmp_path)
    phases_path, phase = profile_phase(registration, "phase_5_review")

    prompt = render_profile_prompt(
        registration, phase, phases_path, tick_id="01TICK", tenant="demo",
        facts={"branch": "todo-42\n- injected: yes\ntoken: super-secret"},
    )

    header = prompt.split("Work on TODO-42 ONLY.")[0]
    assert "- injected: yes\n" not in header
    assert "super-secret" not in prompt
    assert "[REDACTED]" in header


def _mid_round_upgrade_board(tmp_path, mocker, *, legacy_key):
    """The board a run upgraded mid-review-round really leaves behind.

    Under the old round machinery ``review:0`` was READ-ONLY -- the deleted
    ``verify_read_only_review`` demanded a frozen head -- so its report ends at
    the implementation head, and the fix commit belongs to the ``review-fix:1``
    card that followed. HEAD has therefore moved one commit PAST the head
    ``review:0`` reported, and ``accepted-review-head`` was never written.
    """
    import subprocess

    repo = tmp_path / "run"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    def commit(name):
        (repo / name).write_text(name)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", name], cwd=repo, check=True,
                       capture_output=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
            text=True,
        ).stdout.strip()

    implementation_head = commit("impl.txt")
    commit("review-fix.txt")  # the legacy round's own commit advanced HEAD

    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    mocker.patch(
        "hermes_pipeline.review_reconciliation.load_validated_registration",
        return_value=_registration(repo),
    )
    board = {"plan:task-1": _task("worker-1"), "review:0": _task("review")}
    if legacy_key is not None:
        board[legacy_key] = _task("legacy-round")
    mocker.patch(
        "hermes_pipeline.review_reconciliation.get_todo_kanban_tasks",
        return_value=board,
    )
    mocker.patch("hermes_pipeline.review_reconciliation._ensure_initial_review")
    mocker.patch(
        "hermes_pipeline.review_reconciliation._implementation_head",
        return_value=implementation_head,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation._show_task_payload",
        return_value={"payload": True},
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.parse_worker_result",
        return_value=SimpleNamespace(
            git=SimpleNamespace(
                expected_parent_sha=implementation_head,
                resulting_head_sha=implementation_head,
                task_commit_sha=implementation_head,
                changed_files=(),
            )
        ),
    )
    # No ``accepted-review-head``, so ``require_current`` is True.
    assert not (state / "runs" / "01TICK" / "accepted-review-head").exists()
    return repo, state


@pytest.mark.parametrize(
    "legacy_key",
    [
        pytest.param("review-fix:1", id="review-fix"),
        pytest.param("re-review:1", id="re-review"),
        pytest.param("fix-validation:1", id="fix-validation"),
    ],
)
def test_a_run_upgraded_mid_review_round_names_the_upgrade_not_the_worker(
    tmp_path, mocker, caplog, legacy_key
):
    """A permanent wedge must not be reported as the worker's bad topology.

    Every tick on such a board reconciles identically: ``review:0`` already
    exists so ``_ensure_initial_review`` returns early, ``require_current`` is
    True because no accepted head was ever recorded, and
    ``verify_optional_single_commit`` then demands ``HEAD ==`` the head
    ``review:0`` reported -- the head the legacy round's own commit moved past.
    That is ``head_mismatch``, forever, blaming the worker for a discontinuity
    across the upgrade. Naming the cause does not unwedge the run: the tick must
    be abandoned and the TODO re-selected.
    """
    repo, state = _mid_round_upgrade_board(tmp_path, mocker, legacy_key=legacy_key)
    caplog.set_level(logging.ERROR)

    assert reconcile_reviews(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    ) is False
    assert "review_round_upgrade_discontinuity" in caplog.text
    assert "head_mismatch" not in caplog.text


def test_a_genuine_head_mismatch_is_still_a_head_mismatch(tmp_path, mocker, caplog):
    """The discriminator: without a legacy card the diagnosis must not change.

    The identical board and the identical live repository, minus the legacy
    round card. If the new code were raised on anything else it would swallow
    the real topology failure this reconciler exists to report.
    """
    repo, state = _mid_round_upgrade_board(tmp_path, mocker, legacy_key=None)
    caplog.set_level(logging.ERROR)

    assert reconcile_reviews(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    ) is False
    assert "head_mismatch" in caplog.text
    assert "review_round_upgrade_discontinuity" not in caplog.text
