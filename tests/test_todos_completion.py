import subprocess
from types import SimpleNamespace

import pytest

from hermes_pipeline.result_contract import ResultContractError
from hermes_pipeline.todos_completion import (
    _accepted_head,
    _check_state,
    _delivery_authority,
    _git,
    _git_bytes,
    _github_identity,
    _pr_view,
    _remote_head,
    _verify_closeout_transition,
    _verify_finish,
    _verify_pr_identity,
    reconcile_todo_completion,
)
from hermes_pipeline.todos_md import (
    TodoCompletionError,
    complete_todo_file,
    complete_todo_text,
)

TODO_TEXT = """# TODOS

## Metadata

NEXT_TODO_ID: 2

## Entry Schema

Example only.

## Entries

- [→] **TODO-1: Deliver the native pipeline** — ready
  - **What:** Ship it.
  - **Why:** Make progress safely.
  - **Decisions:** Branch: `feat/native`
"""


def test_complete_todo_text_marks_exact_entry_and_is_idempotent():
    completed = complete_todo_text(
        TODO_TEXT, "TODO-1", pr_number=42, date="2026-08-19"
    )
    assert "- [x] **TODO-1:" in completed
    assert "  - **Completed:** PR #42, 2026-08-19\n" in completed
    assert complete_todo_text(
        completed, "TODO-1", pr_number=42, date="2026-08-19"
    ) == completed


def test_complete_todo_rejects_conflicting_completion():
    completed = complete_todo_text(
        TODO_TEXT, "TODO-1", pr_number=42, date="2026-08-19"
    )
    with pytest.raises(TodoCompletionError, match="conflicts"):
        complete_todo_text(completed, "TODO-1", pr_number=43, date="2026-08-19")


def test_complete_todo_rejects_non_calendar_date():
    with pytest.raises(TodoCompletionError, match="date"):
        complete_todo_text(TODO_TEXT, "TODO-1", pr_number=42, date="2026-02-31")


def test_complete_todo_file_replaces_atomically(tmp_path):
    path = tmp_path / "TODOS.md"
    path.write_text(TODO_TEXT)
    assert complete_todo_file(path, "TODO-1", pr_number=7, date="2026-08-19")
    assert not complete_todo_file(path, "TODO-1", pr_number=7, date="2026-08-19")


@pytest.mark.parametrize("helper", [_git, _git_bytes])
@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(OSError("missing executable"), id="os-error"),
        pytest.param(subprocess.TimeoutExpired("git", 60), id="timeout"),
        pytest.param(SimpleNamespace(returncode=1, stdout=""), id="nonzero"),
    ],
)
def test_git_fact_helpers_fail_closed_without_leaking_subprocess_details(
    tmp_path, mocker, helper, outcome
):
    run = mocker.patch("hermes_pipeline.todos_completion.subprocess.run")
    if isinstance(outcome, BaseException):
        run.side_effect = outcome
    else:
        run.return_value = outcome

    with pytest.raises(ResultContractError, match="git_verification_failed: status"):
        helper(tmp_path, "status")


def test_git_fact_helpers_return_trimmed_text_and_exact_bytes(tmp_path, mocker):
    run = mocker.patch("hermes_pipeline.todos_completion.subprocess.run")
    run.return_value = SimpleNamespace(returncode=0, stdout="abc\n")
    assert _git(tmp_path, "rev-parse", "HEAD") == "abc"
    run.return_value = SimpleNamespace(returncode=0, stdout=b"abc\n")
    assert _git_bytes(tmp_path, "show", "HEAD:TODOS.md") == b"abc\n"


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (OSError("gh missing"), "pr_unavailable"),
        (subprocess.TimeoutExpired("gh", 60), "pr_unavailable"),
        (SimpleNamespace(returncode=1, stdout=""), "pr_missing"),
        (SimpleNamespace(returncode=0, stdout="{"), "pr_invalid"),
        (SimpleNamespace(returncode=0, stdout="[]"), "pr_invalid"),
    ],
)
def test_pr_view_rejects_unavailable_missing_and_malformed_responses(
    tmp_path, mocker, result, code
):
    run = mocker.patch("hermes_pipeline.todos_completion.subprocess.run")
    if isinstance(result, BaseException):
        run.side_effect = result
    else:
        run.return_value = result
    with pytest.raises(ResultContractError, match=code):
        _pr_view(tmp_path, "https://github.com/acme/repo/pull/1")


def test_pr_view_returns_structured_identity(tmp_path, mocker):
    mocker.patch(
        "hermes_pipeline.todos_completion.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout='{"state":"OPEN"}'),
    )
    assert _pr_view(tmp_path, "https://github.com/acme/repo/pull/1") == {
        "state": "OPEN"
    }


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(OSError("git missing"), id="unavailable"),
        pytest.param(SimpleNamespace(returncode=1, stdout=""), id="nonzero"),
        pytest.param(SimpleNamespace(returncode=0, stdout=""), id="deleted"),
    ],
)
def test_remote_head_rejects_unavailable_or_deleted_branch(tmp_path, mocker, result):
    run = mocker.patch("hermes_pipeline.todos_completion.subprocess.run")
    if isinstance(result, BaseException):
        run.side_effect = result
        code = "remote_unavailable"
    else:
        run.return_value = result
        code = "remote_branch_missing"
    with pytest.raises(ResultContractError, match=code):
        _remote_head(tmp_path, "feat/native")


def test_remote_head_extracts_exact_advertised_sha(tmp_path, mocker):
    sha = "a" * 40
    mocker.patch(
        "hermes_pipeline.todos_completion.subprocess.run",
        return_value=SimpleNamespace(
            returncode=0, stdout=f"{sha}\trefs/heads/feat/native\n"
        ),
    )
    assert _remote_head(tmp_path, "feat/native") == sha


def test_github_identity_accepts_https_and_rejects_non_github_origin(tmp_path, mocker):
    git = mocker.patch("hermes_pipeline.todos_completion._git")
    git.side_effect = [
        "https://github.com/acme/repo.git",
        "refs/remotes/origin/main",
    ]
    assert _github_identity(tmp_path) == ("acme/repo", "main")

    git.side_effect = ["https://example.com/acme/repo.git"]
    with pytest.raises(ResultContractError, match="origin_identity_invalid"):
        _github_identity(tmp_path)

    git.side_effect = ["https://evil.example/x?y=github.com/acme/repo"]
    with pytest.raises(ResultContractError, match="origin_identity_invalid"):
        _github_identity(tmp_path)


def test_github_identity_rejects_invalid_origin_head_ref(tmp_path, mocker):
    mocker.patch(
        "hermes_pipeline.todos_completion._git",
        side_effect=["git@github.com:acme/repo.git", "refs/heads/main"],
    )
    with pytest.raises(ResultContractError, match="base_branch_invalid"):
        _github_identity(tmp_path)


@pytest.mark.parametrize(
    ("contents", "code"),
    [(None, "accepted_review_head_missing"), ("not-a-sha", "accepted_review_head_invalid")],
)
def test_accepted_review_head_must_exist_and_be_a_full_sha(tmp_path, contents, code):
    run_dir = tmp_path / "runs" / "01TICK"
    run_dir.mkdir(parents=True)
    if contents is not None:
        (run_dir / "accepted-review-head").write_text(contents)
    with pytest.raises(ResultContractError, match=code):
        _accepted_head(tmp_path, "01TICK")


def test_delivery_authority_is_created_once_and_rejects_drifted_shape(tmp_path, mocker):
    run_dir = tmp_path / "runs" / "01TICK"
    run_dir.mkdir(parents=True)
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    assert _delivery_authority(tmp_path, "01TICK", tmp_path, create=True) == (
        "acme/repo",
        "main",
    )
    authority = run_dir / "delivery-authority.json"
    authority.write_text('{"base_branch":"main","extra":true}')
    with pytest.raises(ResultContractError, match="delivery_authority_invalid"):
        _delivery_authority(tmp_path, "01TICK", tmp_path)


def test_closeout_transition_rejects_invalid_utf8_and_wrong_todos_result(tmp_path, mocker):
    result = SimpleNamespace(
        git=SimpleNamespace(expected_parent_sha="a" * 40, resulting_head_sha="b" * 40)
    )
    git_bytes = mocker.patch("hermes_pipeline.todos_completion._git_bytes")
    git_bytes.side_effect = [b"\xff", TODO_TEXT.encode()]
    with pytest.raises(ResultContractError, match="closeout_transition_invalid"):
        _verify_closeout_transition(
            tmp_path, result, todo_id="TODO-1", pr_number=7, date="2026-08-19"
        )

    git_bytes.side_effect = [TODO_TEXT.encode(), TODO_TEXT.encode()]
    with pytest.raises(ResultContractError, match="closeout_transition_invalid"):
        _verify_closeout_transition(
            tmp_path, result, todo_id="TODO-1", pr_number=7, date="2026-08-19"
        )


def test_delivery_waits_for_clean_review_gate(tmp_path, mocker):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    (state / "runs" / "01TICK" / "accepted-review-head").write_text("a" * 40)
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={"review-acceptance": SimpleNamespace(status="blocked")},
    )
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    create.assert_not_called()


def test_delivery_creates_finish_only_after_clean_review(tmp_path, mocker):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    (state / "runs" / "01TICK" / "accepted-review-head").write_text("a" * 40)
    registration = SimpleNamespace(
        todo_id="TODO-1", worktree=tmp_path, branch="feat/native",
        assignee="worker", prompt_client="codex",
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=registration,
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={
            "review-acceptance": SimpleNamespace(task_id="review-gate", status="done")
        },
    )
    mocker.patch("hermes_pipeline.todos_completion._git", return_value="a" * 40)
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert create.call_args.kwargs["key"] == "finish"
    assert create.call_args.kwargs["parent"] == "review-gate"
    assert "Do not merge" in create.call_args.kwargs["prompt"]


def test_finish_must_remain_on_exact_accepted_review_head(tmp_path, mocker):
    accepted = "a" * 40
    result = SimpleNamespace(git=SimpleNamespace(
        expected_parent_sha=accepted, resulting_head_sha="b" * 40,
        task_commit_sha="b" * 40, changed_files=("release.md",),
    ))
    mocker.patch("hermes_pipeline.todos_completion._git", return_value=accepted)
    with pytest.raises(ResultContractError, match="finish_review_head_mismatch"):
        _verify_finish(tmp_path, result, accepted, require_current=True)


def test_finish_evidence_remains_valid_after_closeout_advances_live_head(tmp_path):
    accepted = "a" * 40
    result = SimpleNamespace(git=SimpleNamespace(
        expected_parent_sha=accepted, resulting_head_sha=accepted,
        task_commit_sha=accepted, changed_files=(),
    ))
    _verify_finish(tmp_path, result, accepted, require_current=False)


def test_gh_failed_exit_still_classifies_documented_check_failure(tmp_path, mocker):
    mocker.patch(
        "hermes_pipeline.todos_completion.subprocess.run",
        return_value=SimpleNamespace(
            returncode=1, stdout='[{"state":"FAILURE"}]', stderr="checks failed"
        ),
    )
    assert _check_state(tmp_path, "https://github.com/acme/repo/pull/1") == "failed"


def test_gh_exit_8_is_pending_and_successful_no_checks_is_green(tmp_path, mocker):
    run = mocker.patch("hermes_pipeline.todos_completion.subprocess.run")
    run.return_value = SimpleNamespace(returncode=8, stdout="", stderr="pending")
    assert _check_state(tmp_path, "https://github.com/acme/repo/pull/1") == "pending"
    run.return_value = SimpleNamespace(returncode=0, stdout="[]", stderr="")
    assert _check_state(tmp_path, "https://github.com/acme/repo/pull/1") == "passed"


def test_gh_nonzero_unusable_response_needs_input(tmp_path, mocker):
    mocker.patch(
        "hermes_pipeline.todos_completion.subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="auth failed"),
    )
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _check_state(tmp_path, "https://github.com/acme/repo/pull/1")


def test_pr_identity_requires_registered_origin_and_base(tmp_path, mocker):
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    with pytest.raises(ResultContractError, match="pr_identity_mismatch"):
        _verify_pr_identity(
            tmp_path,
            {"headRefName": "feat/native", "baseRefName": "release",
             "headRepository": {"nameWithOwner": "acme/repo"}},
            branch="feat/native",
        )


def test_reconciliation_finish_to_closeout_to_human_merge_gate(tmp_path, mocker):
    state = tmp_path / ".hermes"
    run_dir = state / "runs" / "01TICK"
    run_dir.mkdir(parents=True)
    (run_dir / "registration.json").write_text("{}")
    (run_dir / "accepted-review-head").write_text("a" * 40)
    (run_dir / "delivery-authority.json").write_text(
        '{"base_branch":"main","origin_repository":"acme/repo"}\n'
    )
    registration = SimpleNamespace(
        todo_id="TODO-1", worktree=tmp_path, branch="feat/native",
        assignee="worker", prompt_client="codex",
    )
    delivery = SimpleNamespace(
        pr_url="https://github.com/acme/repo/pull/7", branch="feat/native",
        head_sha="a" * 40,
    )
    finish_result = SimpleNamespace(
        delivery=delivery,
        git=SimpleNamespace(
            expected_parent_sha="a" * 40, resulting_head_sha="a" * 40,
            task_commit_sha="a" * 40, changed_files=(),
        ),
    )
    closeout_result = SimpleNamespace(git=SimpleNamespace(
        expected_parent_sha="a" * 40, resulting_head_sha="b" * 40,
        task_commit_sha="b" * 40, changed_files=("TODOS.md",),
    ))
    tasks = {
        "review-acceptance": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
    }
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=registration,
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        side_effect=lambda *_args, **_kwargs: tasks,
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.parse_worker_result",
        side_effect=lambda *_args, step_key, **_kwargs: (
            finish_result if step_key == "finish" else closeout_result
        ),
    )
    mocker.patch("hermes_pipeline.todos_completion._verify_finish")
    mocker.patch("hermes_pipeline.todos_completion.verify_worker_git_result")
    mocker.patch("hermes_pipeline.todos_completion._verify_closeout_transition")
    mocker.patch("hermes_pipeline.todos_completion._verify_pr_identity")
    mocker.patch("hermes_pipeline.todos_completion._github_identity",
                 return_value=("acme/repo", "main"))
    remote_head = mocker.patch(
        "hermes_pipeline.todos_completion._remote_head",
        side_effect=["a" * 40, "b" * 40],
    )
    view = mocker.patch(
        "hermes_pipeline.todos_completion._pr_view",
        return_value={"state": "OPEN", "url": delivery.pr_url,
                      "headRefName": "feat/native", "headRefOid": "a" * 40},
    )
    create = mocker.patch(
        "hermes_pipeline.todos_completion._create_task",
        side_effect=["closeout-id", "human-id"],
    )

    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert create.call_args.kwargs["key"] == "closeout"
    assert view.call_count == 1

    tasks["closeout"] = SimpleNamespace(task_id="closeout-id", status="done")
    view.return_value = {
        "state": "OPEN", "url": delivery.pr_url, "headRefName": "feat/native",
        "headRefOid": "b" * 40,
    }
    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    assert create.call_args.kwargs["key"] == "human-gate"
    assert view.call_count == 2
    assert remote_head.call_count == 2

    tasks["human-gate"] = SimpleNamespace(task_id="human-id", status="blocked")
    view.return_value = {
        "state": "MERGED", "url": delivery.pr_url, "headRefName": "feat/native",
        "headRefOid": "b" * 40,
    }
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    complete = mocker.patch(
        "hermes_pipeline.todos_completion.complete_todo_kanban_task", return_value=True
    )
    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    complete.assert_called_once_with("demo", "human-id")
    assert view.call_count == 3
    assert remote_head.call_count == 2


@pytest.mark.parametrize(
    ("check_state", "should_complete"),
    [
        pytest.param("passed", True, id="passed"),
        pytest.param("pending", False, id="pending"),
    ],
)
def test_merged_closeout_creates_missing_gate_before_checks_without_remote_head(
    tmp_path, mocker, check_state, should_complete
):
    state = tmp_path / ".hermes"
    run_dir = state / "runs" / "01TICK"
    run_dir.mkdir(parents=True)
    (run_dir / "registration.json").write_text("{}")
    (run_dir / "accepted-review-head").write_text("a" * 40)
    (run_dir / "delivery-authority.json").write_text(
        '{"base_branch":"main","origin_repository":"acme/repo"}\n'
    )
    (run_dir / "closeout-date").write_text("2026-08-19\n")
    registration = SimpleNamespace(
        todo_id="TODO-1", worktree=tmp_path, branch="feat/native",
        assignee="worker", prompt_client="codex",
    )
    delivery = SimpleNamespace(
        pr_url="https://github.com/acme/repo/pull/7", branch="feat/native",
        head_sha="a" * 40,
    )
    finish_result = SimpleNamespace(
        delivery=delivery,
        git=SimpleNamespace(
            expected_parent_sha="a" * 40, resulting_head_sha="a" * 40,
            task_commit_sha="a" * 40, changed_files=(),
        ),
    )
    closeout_result = SimpleNamespace(git=SimpleNamespace(
        expected_parent_sha="a" * 40, resulting_head_sha="b" * 40,
        task_commit_sha="b" * 40, changed_files=("TODOS.md",),
    ))
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=registration,
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={
            "review-acceptance": SimpleNamespace(task_id="review", status="done"),
            "finish": SimpleNamespace(task_id="finish-id", status="done"),
            "closeout": SimpleNamespace(task_id="closeout-id", status="done"),
        },
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.parse_worker_result",
        side_effect=lambda *_args, step_key, **_kwargs: (
            finish_result if step_key == "finish" else closeout_result
        ),
    )
    mocker.patch("hermes_pipeline.todos_completion._verify_finish")
    mocker.patch("hermes_pipeline.todos_completion.verify_worker_git_result")
    mocker.patch("hermes_pipeline.todos_completion._verify_closeout_transition")
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion._pr_view",
        return_value={
            "state": "MERGED", "url": delivery.pr_url,
            "headRefName": "feat/native", "headRefOid": "b" * 40,
            "baseRefName": "main",
            "headRepository": {"nameWithOwner": "acme/repo"},
        },
    )
    checks = mocker.patch(
        "hermes_pipeline.todos_completion._check_state", return_value=check_state
    )
    remote_head = mocker.patch(
        "hermes_pipeline.todos_completion._remote_head",
        side_effect=ResultContractError("remote_branch_missing"),
    )
    create = mocker.patch(
        "hermes_pipeline.todos_completion._create_task", return_value="human-id"
    )
    complete = mocker.patch(
        "hermes_pipeline.todos_completion.complete_todo_kanban_task",
        return_value=True,
    )
    events = mocker.Mock()
    events.attach_mock(create, "create")
    events.attach_mock(checks, "checks")

    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    remote_head.assert_not_called()
    assert create.call_args.kwargs["key"] == "human-gate"
    assert create.call_args.kwargs["gate"] is True
    assert [event[0] for event in events.mock_calls] == ["create", "checks"]
    if should_complete:
        complete.assert_called_once_with("demo", "human-id")
    else:
        complete.assert_not_called()
