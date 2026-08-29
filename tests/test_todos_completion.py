import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_pipeline.github_issues import GitHubIssuesError
from hermes_pipeline.result_contract import ResultContractError
from hermes_pipeline.todos_completion import (
    _accepted_head,
    _check_state,
    _delivery_authority,
    _git,
    _github_identity,
    _pr_view,
    _remote_head,
    _verify_finish,
    _verify_pr_identity,
    close_issue_for_delivery,
    reconcile_todo_completion,
)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(OSError("missing executable"), id="os-error"),
        pytest.param(subprocess.TimeoutExpired("git", 60), id="timeout"),
        pytest.param(SimpleNamespace(returncode=1, stdout=""), id="nonzero"),
    ],
)
def test_git_fact_helper_fails_closed_without_leaking_subprocess_details(
    tmp_path, mocker, outcome
):
    run = mocker.patch("hermes_pipeline.todos_completion.subprocess.run")
    if isinstance(outcome, BaseException):
        run.side_effect = outcome
    else:
        run.return_value = outcome

    with pytest.raises(ResultContractError, match="git_verification_failed: status"):
        _git(tmp_path, "status")


def test_git_fact_helper_returns_trimmed_text(tmp_path, mocker):
    run = mocker.patch("hermes_pipeline.todos_completion.subprocess.run")
    run.return_value = SimpleNamespace(returncode=0, stdout="abc\n")
    assert _git(tmp_path, "rev-parse", "HEAD") == "abc"


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
    assert _delivery_authority(tmp_path, "01TICK", tmp_path, repo="ACME/repo", create=True) == (
        "acme/repo",
        "main",
    )
    authority = run_dir / "delivery-authority.json"
    authority.write_text('{"base_branch":"main","extra":true}')
    with pytest.raises(ResultContractError, match="delivery_authority_invalid"):
        _delivery_authority(tmp_path, "01TICK", tmp_path, repo="acme/repo")


def test_delivery_authority_must_match_the_project_repo(tmp_path, mocker):
    run_dir = tmp_path / "runs" / "01TICK"
    run_dir.mkdir(parents=True)
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("evil/repo", "main"),
    )
    with pytest.raises(ResultContractError, match="delivery_authority_drift"):
        _delivery_authority(tmp_path, "01TICK", tmp_path, repo="acme/repo", create=True)
    assert not (run_dir / "delivery-authority.json").exists()
    (run_dir / "delivery-authority.json").write_text(
        '{"base_branch":"main","origin_repository":"evil/repo"}\n'
    )
    with pytest.raises(ResultContractError, match="delivery_authority_drift"):
        _delivery_authority(tmp_path, "01TICK", tmp_path, repo="acme/repo")


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
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", repo="acme/repo"
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
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", repo="acme/repo"
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


def test_finish_evidence_is_not_rechecked_against_live_head_once_verified(tmp_path):
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


@pytest.mark.parametrize(
    "view",
    [
        pytest.param({"baseRefName": "release"}, id="wrong-base-branch"),
        pytest.param({"baseRepository": {"nameWithOwner": "fork/repo"}}, id="fork-base"),
        pytest.param({"baseRepository": None}, id="no-base-repository"),
        pytest.param({"headRepository": {"nameWithOwner": "fork/repo"}}, id="fork-head"),
    ],
)
def test_pr_identity_requires_registered_origin_base_and_repo(tmp_path, mocker, view):
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    good = {
        "headRefName": "feat/native", "baseRefName": "main",
        "headRepository": {"nameWithOwner": "acme/repo"},
        "baseRepository": {"nameWithOwner": "ACME/repo"},
    }
    _verify_pr_identity(tmp_path, good, branch="feat/native", repo="acme/repo")
    with pytest.raises(ResultContractError, match="pr_identity_mismatch"):
        _verify_pr_identity(tmp_path, {**good, **view}, branch="feat/native", repo="acme/repo")


def test_pr_view_requests_base_repository(tmp_path, mocker):
    run = mocker.patch(
        "hermes_pipeline.todos_completion.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="{}"),
    )
    _pr_view(tmp_path, "https://github.com/acme/repo/pull/1")
    fields = run.call_args.args[0][run.call_args.args[0].index("--json") + 1].split(",")
    assert {"baseRepository", "headRepository", "headRefOid", "state", "url"} <= set(fields)


API = ("gh", "api", "-H", "Accept: application/vnd.github+json")
REPO = "acme/repo"
PR_URL = f"https://github.com/{REPO}/pull/7"
MARKER = "<!-- tpo-completed tick=01TICK pr=7 -->"


class FakeRemoteIssue:
    """Stateful GitHub issue behind ``fake_gh``: reads reflect earlier writes."""

    def __init__(self, fake, number=3, *, state="open", labels=("tpo:todo", "tpo:in-progress"),
                 comments=(), crash_after=None, state_reason=None):
        from tests.gh_fakes import issue_payload

        self.number = number
        self.state = state
        self.state_reason = state_reason
        self.labels = list(labels)
        self.comments = list(comments)
        self.writes: list[str] = []
        self.crash_after = crash_after
        base = f"repos/{REPO}/issues/{number}"
        fake.on(*API, base, handler=lambda argv: (
            0, json.dumps(issue_payload(
                number, state=self.state, labels=self.labels, state_reason=self.state_reason,
            )), ""
        ))
        fake.on(*API, "--paginate", "--slurp", f"{base}/comments", handler=lambda argv: (
            0, json.dumps([[self._comment(entry) for entry in self.comments]]), ""
        ))
        fake.on(*API, "user", "--jq", ".login", stdout=f"{self.login}\n")

        def comment(argv):
            with open(argv[argv.index("--body-file") + 1]) as handle:
                self.comments.append(handle.read())
            return self._wrote("comment")

        def close(argv):
            self.state = "closed"
            self.state_reason = "completed"
            return self._wrote("close")

        def edit(argv):
            self.labels.remove(argv[argv.index("--remove-label") + 1])
            return self._wrote("edit")

        fake.on("gh", "issue", "comment", handler=comment)
        fake.on("gh", "issue", "close", handler=close)
        fake.on("gh", "issue", "edit", handler=edit)

    login = "tpo-bot"

    @classmethod
    def _comment(cls, entry):
        """``str`` entries are TPO's own comments; ``(login, body)`` pairs name another author."""
        login, body = entry if isinstance(entry, tuple) else (cls.login, entry)
        return {"body": body, "user": {"login": login}}

    def _wrote(self, verb):
        self.writes.append(verb)
        if self.crash_after == verb:
            raise RuntimeError(f"crash after {verb}")
        return 0, "", ""


def _run_dir(tmp_path):
    state = tmp_path / ".hermes"
    run_dir = state / "runs" / "01TICK"
    run_dir.mkdir(parents=True)
    return state, run_dir


def _close(tmp_path, state, **overrides):
    kwargs = dict(
        project_dir=tmp_path, state_dir=state, tick_id="01TICK", issue_number=3,
        pr_number=7, pr_url=PR_URL, repo=REPO,
    )
    kwargs.update(overrides)
    return close_issue_for_delivery(**kwargs)


def test_close_issue_for_delivery_comments_closes_unlabels_and_marks_run(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh)

    assert _close(tmp_path, state) == "closed"

    assert issue.writes == ["comment", "close", "edit"]
    assert issue.state == "closed"
    assert "tpo:in-progress" not in issue.labels
    assert len(issue.comments) == 1
    assert issue.comments[0].startswith(f"Completed: PR #7 {PR_URL}, 20")
    assert issue.comments[0].rstrip().endswith(MARKER)
    assert (run_dir / "issue-closed").exists()
    assert (run_dir / "issue-commented").read_text() == "tpo-bot\n"
    assert fake_gh.gh_calls() == [  # login is resolved once, after commenting, for the breadcrumb
        [*API[1:], "repos/acme/repo/issues/3"],
        [*API[1:], "--paginate", "--slurp", "repos/acme/repo/issues/3/comments"],
        ["issue", "comment", "3", "--repo", REPO, "--body-file", fake_gh.gh_calls()[2][-1]],
        [*API[1:], "user", "--jq", ".login"],
        ["issue", "close", "3", "--repo", REPO, "--reason", "completed"],
        ["issue", "edit", "3", "--repo", REPO, "--remove-label", "tpo:in-progress"],
        [*API[1:], "repos/acme/repo/issues/3"],
        [*API[1:], "--paginate", "--slurp", "repos/acme/repo/issues/3/comments"],
    ]


def test_close_issue_for_delivery_is_a_no_op_when_already_delivered(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh, state="closed", labels=("tpo:todo",), comments=[f"x\n{MARKER}"])

    assert _close(tmp_path, state) == "closed"
    assert issue.writes == []
    assert (run_dir / "issue-closed").exists()


@pytest.mark.parametrize(
    ("initial", "expected_writes"),
    [
        pytest.param(
            dict(state="closed", labels=("tpo:todo",), comments=[]), ["comment"], id="closed-without-comment"
        ),
        pytest.param(
            dict(state="open", labels=("tpo:todo",), comments=[f"done\n{MARKER}"]), ["close"], id="comment-still-open"
        ),
        pytest.param(
            dict(state="closed", labels=("tpo:in-progress",), comments=[MARKER]), ["edit"], id="label-left"
        ),
    ],
)
def test_close_issue_for_delivery_repairs_only_the_missing_step(
    tmp_path, fake_gh, initial, expected_writes
):
    state, _run_dir_ = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh, **initial)
    assert _close(tmp_path, state) == "closed"
    assert issue.writes == expected_writes
    assert [c for c in issue.comments if MARKER in c] == [c for c in issue.comments if MARKER in c][:1]


def test_close_issue_for_delivery_adds_its_own_marker_beside_an_older_ticks(tmp_path, fake_gh):
    state, _run_dir_ = _run_dir(tmp_path)
    issue = FakeRemoteIssue(
        fake_gh, state="closed", labels=("tpo:todo",),
        comments=["<!-- tpo-completed tick=00OLD pr=7 -->"],
    )
    assert _close(tmp_path, state) == "closed"
    assert issue.writes == ["comment"]


def test_close_issue_for_delivery_ignores_completion_markers_by_other_authors(tmp_path, fake_gh):
    """Only TPO-authored markers count: a pasted marker can neither conflict nor satisfy dedup."""
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(
        fake_gh,
        comments=[
            ("mallory", "Completed: PR #6\n<!-- tpo-completed tick=00OLD pr=6 -->"),
            ("mallory", MARKER.replace("01TICK", "00ELSEWHERE")),  # no such local run
        ],
    )

    assert _close(tmp_path, state) == "closed"

    assert issue.writes == ["comment", "close", "edit"]
    assert [c for c in issue.comments if isinstance(c, str)] == [issue.comments[-1]]
    assert MARKER in issue.comments[-1]
    assert (run_dir / "issue-closed").exists()


def test_close_issue_for_delivery_ignores_a_foreign_author_reusing_a_local_tick(tmp_path, fake_gh):
    """A pasted marker naming one of our ticks is not ours when that run recorded another login."""
    state, _run_dir_ = _run_dir(tmp_path)
    (state / "runs" / "00OLD").mkdir()
    (state / "runs" / "00OLD" / "issue-commented").write_text("tpo-bot\n")
    issue = FakeRemoteIssue(
        fake_gh,
        comments=[
            ("mallory", "Completed: PR #6\n<!-- tpo-completed tick=00OLD pr=6 -->"),
            ("mallory", "<!-- tpo-completed tick=00OLD pr=7 -->"),
        ],
    )
    assert _close(tmp_path, state) == "closed"  # neither conflict nor dedup
    assert issue.writes == ["comment", "close", "edit"]


def test_close_issue_for_delivery_owns_markers_by_the_login_recorded_for_their_run(tmp_path, fake_gh):
    """Rotated token: the run's breadcrumb still names the login that commented."""
    state, _run_dir_ = _run_dir(tmp_path)
    (state / "runs" / "00OLD").mkdir()
    (state / "runs" / "00OLD" / "issue-commented").write_text("previous-bot\n")
    issue = FakeRemoteIssue(
        fake_gh, comments=[("previous-bot", "Completed: PR #6\n<!-- tpo-completed tick=00OLD pr=6 -->")],
    )
    with pytest.raises(GitHubIssuesError) as excinfo:
        _close(tmp_path, state)
    assert excinfo.value.code == "completion_conflict"
    assert issue.writes == []


def test_close_issue_for_delivery_owns_markers_whose_tick_has_a_local_run_dir(tmp_path, fake_gh):
    """Legacy breadcrumbs (``pr=N`` or missing) recorded no login: the local tick alone suffices."""
    state, _run_dir_ = _run_dir(tmp_path)
    (state / "runs" / "00OLD").mkdir()
    (state / "runs" / "00OLD" / "issue-commented").write_text("pr=6\n")
    issue = FakeRemoteIssue(
        fake_gh, comments=[("previous-bot", "Completed: PR #6\n<!-- tpo-completed tick=00OLD pr=6 -->")],
    )
    with pytest.raises(GitHubIssuesError) as excinfo:
        _close(tmp_path, state)
    assert excinfo.value.code == "completion_conflict"

    # And a rotated-login copy of our own marker still satisfies dedup.
    issue = FakeRemoteIssue(fake_gh, state="closed", labels=("tpo:todo",), comments=[("previous-bot", MARKER)])
    assert _close(tmp_path, state) == "closed"
    assert issue.writes == []


def test_close_issue_for_delivery_skips_login_lookup_when_no_remote_marker_needs_judging(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    # Our own comment already landed (local breadcrumb): nothing to attribute or write.
    (run_dir / "issue-commented").write_text("tpo-bot\n")
    issue = FakeRemoteIssue(fake_gh, comments=["unrelated chatter"])
    assert _close(tmp_path, state) == "closed"
    assert issue.writes == ["close", "edit"]
    assert not any(call[:1] == ["api"] and "user" in call for call in fake_gh.gh_calls())

    # Local issue-commented marker plus a foreign-looking marker copy: the local
    # marker settles dedup, so the login is still not needed.
    state2 = tmp_path / "second" / ".hermes"
    (state2 / "runs" / "01TICK").mkdir(parents=True)
    (state2 / "runs" / "01TICK" / "issue-commented").write_text("pr=7\n")
    fake_gh.calls.clear()
    FakeRemoteIssue(fake_gh, state="closed", labels=("tpo:todo",), comments=[("someone", MARKER)])
    assert _close(tmp_path, state2) == "closed"
    assert not any("user" in call for call in fake_gh.gh_calls())


def test_close_issue_for_delivery_falls_back_to_run_dir_ownership_when_user_lookup_is_forbidden(
    tmp_path, fake_gh, caplog
):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh, comments=[("mallory", "<!-- tpo-completed tick=00OTHER pr=6 -->")])
    fake_gh.on(*API, "user", "--jq", ".login", rc=1, stderr="HTTP 403: Resource not accessible by integration")

    with caplog.at_level("WARNING", logger="hermes_pipeline.todos_completion"):
        assert _close(tmp_path, state) == "closed"

    assert issue.writes == ["comment", "close", "edit"]
    assert (run_dir / "issue-commented").read_text() == "\n"
    assert any("gh_auth" in r.getMessage() and "read:user" in r.getMessage() for r in caplog.records)


def test_close_issue_for_delivery_refuses_a_conflicting_completion_unless_forced(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh, comments=["Completed: PR #6\n<!-- tpo-completed tick=00OLD pr=6 -->"])
    with pytest.raises(GitHubIssuesError) as excinfo:
        _close(tmp_path, state)
    assert excinfo.value.code == "completion_conflict"
    assert issue.writes == []
    assert not (run_dir / "issue-close-started").exists()

    assert _close(tmp_path, state, force=True) == "closed"
    assert issue.writes == ["comment", "close", "edit"]


def test_close_issue_for_delivery_refuses_an_issue_closed_as_not_planned(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh, state="closed", state_reason="not_planned")
    with pytest.raises(GitHubIssuesError) as excinfo:
        _close(tmp_path, state)
    assert excinfo.value.code == "issue_not_planned"
    assert issue.writes == []
    assert not (run_dir / "issue-closed").exists()


def test_close_issue_for_delivery_writes_started_breadcrumb_before_first_mutation(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh, crash_after="comment")
    seen: list[bool] = []
    original = fake_gh._match(["gh", "issue", "comment"]).handler
    fake_gh.on("gh", "issue", "comment", handler=lambda argv: (
        seen.append((run_dir / "issue-close-started").exists()), original(argv))[1])
    with pytest.raises(RuntimeError):
        _close(tmp_path, state)
    assert seen == [True]
    assert not (run_dir / "issue-commented").exists()
    assert issue.writes == ["comment"]


def test_close_issue_for_delivery_bounds_duplicate_comments_when_listing_lags(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh)
    fake_gh.on(*API, "--paginate", "--slurp", f"repos/{REPO}/issues/3/comments", stdout="[[]]")

    assert _close(tmp_path, state) == "closed"
    assert (run_dir / "issue-commented").exists()
    assert _close(tmp_path, state) == "closed"
    assert _close(tmp_path, state) == "closed"
    assert issue.writes.count("comment") == 1
    assert len(issue.comments) == 1


@pytest.mark.parametrize(
    ("break_step", "expected"),
    [
        pytest.param("edit", "label", id="label-still-present"),
        pytest.param("comment", "comment", id="comment-still-missing"),
    ],
)
def test_close_issue_for_delivery_pending_when_a_postcondition_is_unmet(
    tmp_path, fake_gh, break_step, expected
):
    state = tmp_path / ".hermes"  # manual path: no run dir, no breadcrumbs
    issue = FakeRemoteIssue(fake_gh)
    fake_gh.on("gh", "issue", break_step, handler=lambda argv: (issue.writes.append(break_step), (0, "", ""))[1])
    assert _close(tmp_path, state, tick_id="manual") == "pending"
    assert issue.state == "closed"
    if expected == "label":
        assert "tpo:in-progress" in issue.labels
    else:
        assert issue.comments == []
    assert not (state / "runs").exists()


def test_close_issue_for_delivery_stays_pending_on_propagation_lag(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh)
    real_close = fake_gh._match(["gh", "issue", "close"]).handler

    def lagging_close(argv):
        issue.writes.append("close")
        return 0, "", ""  # gh accepted it, but the read replica still says open

    fake_gh.on("gh", "issue", "close", handler=lagging_close)
    assert real_close is not lagging_close
    assert _close(tmp_path, state) == "pending"
    assert not (run_dir / "issue-closed").exists()
    assert issue.state == "open"


def test_close_issue_for_delivery_writes_no_marker_without_a_run_dir(tmp_path, fake_gh):
    state = tmp_path / ".hermes"
    issue = FakeRemoteIssue(fake_gh)
    assert _close(tmp_path, state, tick_id="manual") == "closed"
    assert issue.comments[0].rstrip().endswith("<!-- tpo-completed tick=manual pr=7 -->")
    assert not (state / "runs").exists()


def test_close_issue_for_delivery_propagates_gh_failures(tmp_path, fake_gh):
    state, run_dir = _run_dir(tmp_path)
    FakeRemoteIssue(fake_gh)
    fake_gh.on("gh", "issue", "close", rc=1, stderr="HTTP 429 rate limit exceeded")
    with pytest.raises(GitHubIssuesError) as excinfo:
        _close(tmp_path, state)
    assert excinfo.value.code == "gh_rate_limited"
    assert not (run_dir / "issue-closed").exists()


@pytest.mark.parametrize("crash_after", ["comment", "close"])
def test_close_issue_for_delivery_resumes_idempotently_after_a_crash(tmp_path, fake_gh, crash_after):
    state, run_dir = _run_dir(tmp_path)
    issue = FakeRemoteIssue(fake_gh, crash_after=crash_after)
    with pytest.raises(RuntimeError):
        _close(tmp_path, state)
    assert not (run_dir / "issue-closed").exists()
    issue.crash_after = None
    before = list(issue.writes)

    assert _close(tmp_path, state) == "closed"

    assert issue.writes.count("comment") == 1
    assert issue.writes.count("close") == 1
    assert len(issue.writes) == 3 and len(before) < 3
    assert (run_dir / "issue-closed").exists()


def _finish_done_fixture(tmp_path, mocker, *, tasks, view):
    state, run_dir = _run_dir(tmp_path)
    (run_dir / "registration.json").write_text("{}")
    (run_dir / "accepted-review-head").write_text("a" * 40)
    (run_dir / "delivery-authority.json").write_text(
        '{"base_branch":"main","origin_repository":"acme/repo"}\n'
    )
    registration = SimpleNamespace(
        todo_id="TODO-3", worktree=tmp_path, branch="feat/native",
        assignee="worker", prompt_client="codex", issue_number=3,
        issue_url="https://github.com/acme/repo/issues/3",
    )
    delivery = SimpleNamespace(pr_url=PR_URL, branch="feat/native", head_sha="a" * 40)
    finish_result = SimpleNamespace(
        delivery=delivery,
        git=SimpleNamespace(
            expected_parent_sha="a" * 40, resulting_head_sha="a" * 40,
            task_commit_sha="a" * 40, changed_files=(),
        ),
    )
    mocker.patch("hermes_pipeline.todos_completion.load_validated_registration",
                 return_value=registration)
    mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks",
                 side_effect=lambda *_a, **_k: tasks)
    mocker.patch("hermes_pipeline.todos_completion.parse_worker_result",
                 return_value=finish_result)
    mocker.patch("hermes_pipeline.todos_completion._verify_finish")
    mocker.patch("hermes_pipeline.todos_completion._verify_pr_identity")
    mocker.patch("hermes_pipeline.todos_completion._github_identity",
                 return_value=("acme/repo", "main"))
    mocker.patch("hermes_pipeline.todos_completion._remote_head", return_value="a" * 40)
    mocker.patch("hermes_pipeline.todos_completion._pr_view", side_effect=lambda *_a: dict(view))
    return state


def _gate_tasks(status="blocked"):
    return {
        "review-acceptance": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
        "human-gate": SimpleNamespace(task_id="human-id", status=status),
    }


def _no_gate_tasks():
    return {
        "review-acceptance": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
    }


def _view(state="OPEN", head="a" * 40, url=PR_URL):
    return {"state": state, "url": url, "headRefName": "feat/native", "headRefOid": head}


@pytest.mark.parametrize(
    ("tasks", "view", "code"),
    [
        pytest.param(_no_gate_tasks(), _view("MERGED", "b" * 40), "pr_head_drift", id="no-gate-merged-drifted"),
        pytest.param(_no_gate_tasks(), _view("OPEN", "b" * 40), "pr_head_drift", id="no-gate-open-drifted"),
        pytest.param(_no_gate_tasks(), _view("CLOSED"), "pr_head_drift", id="no-gate-closed"),
        pytest.param(_gate_tasks(), _view("CLOSED"), "pull_request_closed_or_drifted", id="gate-closed"),
        pytest.param(_gate_tasks(), _view("OPEN", "b" * 40), "pr_head_drift", id="gate-open-drifted"),
        pytest.param(_gate_tasks(), _view("MERGED", "b" * 40), "pr_head_drift", id="gate-merged-drifted"),
    ],
)
def test_pr_state_guards_block_the_gate_and_never_touch_the_issue(tmp_path, mocker, tasks, view, code):
    state = _finish_done_fixture(tmp_path, mocker, tasks=tasks, view=view)
    mocker.patch("hermes_pipeline.todos_completion._create_task", return_value="human-id")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")
    checks = mocker.patch("hermes_pipeline.todos_completion._check_state")

    assert _reconcile(tmp_path, state) is False
    mark.assert_called_once_with("human-id", f"TPO delivery blocked: {code}")
    close.assert_not_called()
    checks.assert_not_called()


def test_remote_head_drift_blocks_before_the_gate_is_armed(tmp_path, mocker):
    state = _finish_done_fixture(tmp_path, mocker, tasks=_no_gate_tasks(), view=_view())
    remote_head = mocker.patch("hermes_pipeline.todos_completion._remote_head", return_value="c" * 40)
    mocker.patch("hermes_pipeline.todos_completion._create_task", return_value="human-id")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")

    assert _reconcile(tmp_path, state) is False
    remote_head.assert_called_once_with(tmp_path, "feat/native")
    mark.assert_called_once_with("human-id", "TPO delivery blocked: remote_head_drift")


@pytest.mark.parametrize(
    "pr_url",
    [
        pytest.param("https://github.com/other/repo/pull/7", id="other-repo"),
        pytest.param("https://github.com/acme/repo/pulls/7", id="not-a-pull-path"),
    ],
)
def test_pr_url_outside_the_project_repo_blocks_before_any_pr_read(tmp_path, mocker, pr_url):
    state = _finish_done_fixture(tmp_path, mocker, tasks=_gate_tasks(), view=_view(url=pr_url))
    import hermes_pipeline.todos_completion as module

    module.parse_worker_result.return_value.delivery.pr_url = pr_url
    view = mocker.patch("hermes_pipeline.todos_completion._pr_view")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")

    assert _reconcile(tmp_path, state) is False
    view.assert_not_called()
    close.assert_not_called()
    mark.assert_called_once_with("human-id", "TPO delivery blocked: pr_identity_mismatch")


def test_open_pr_with_green_checks_keeps_waiting_for_the_human(tmp_path, mocker):
    state = _finish_done_fixture(tmp_path, mocker, tasks=_gate_tasks(), view=_view())
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")
    complete = mocker.patch("hermes_pipeline.todos_completion.complete_todo_kanban_task")

    assert _reconcile(tmp_path, state) is True
    mark.assert_not_called()
    close.assert_not_called()
    complete.assert_not_called()


def test_poisoned_worktree_origin_blocks_delivery_without_gh_writes(tmp_path, mocker, fake_gh):
    """A worktree-scoped ``url.insteadOf`` cannot redirect delivery to another repo."""
    state = _finish_done_fixture(tmp_path, mocker, tasks=_gate_tasks(), view=_view("MERGED"))
    mocker.patch("hermes_pipeline.todos_completion._github_identity", return_value=("evil/repo", "main"))
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")

    assert _reconcile(tmp_path, state) is False
    mark.assert_called_once_with("human-id", "TPO delivery blocked: delivery_authority_drift")
    assert fake_gh.calls == []


def test_finish_live_check_is_skipped_only_after_a_verified_marker(tmp_path, mocker):
    import hermes_pipeline.todos_completion as module

    state = _finish_done_fixture(tmp_path, mocker, tasks=_gate_tasks(), view=_view())
    marker = state / "runs" / "01TICK" / "finish-verified"
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="pending")
    module._verify_finish.side_effect = ResultContractError("finish_review_head_mismatch")
    mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")

    assert _reconcile(tmp_path, state) is False
    assert module._verify_finish.call_args.kwargs["require_current"] is True
    assert not marker.exists()

    module._verify_finish.side_effect = None
    assert _reconcile(tmp_path, state) is True
    assert module._verify_finish.call_args.kwargs["require_current"] is True
    assert marker.exists()

    assert _reconcile(tmp_path, state) is True
    assert module._verify_finish.call_args.kwargs["require_current"] is False


def test_unsafe_pr_url_never_reaches_the_human_merge_prompt(tmp_path, mocker):
    import hermes_pipeline.todos_completion as module

    state = _finish_done_fixture(tmp_path, mocker, tasks=_no_gate_tasks(), view=_view())
    module.parse_worker_result.return_value.delivery.pr_url = PR_URL + "\x07"
    module._pr_view.side_effect = lambda *_a: _view(url=PR_URL + "\x07")
    mocker.patch("hermes_pipeline.todos_completion._create_task", return_value="human-id")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")

    assert _reconcile(tmp_path, state) is False
    module._pr_view.assert_not_called()
    mark.assert_called_once_with("human-id", "TPO delivery blocked: pr_identity_mismatch")


def test_retryable_gate_registration_is_not_progress(tmp_path, mocker, caplog):
    from hermes_pipeline.review_reconciliation import RetryableReviewRegistration

    state = _finish_done_fixture(tmp_path, mocker, tasks=_no_gate_tasks(), view=_view())
    mocker.patch(
        "hermes_pipeline.todos_completion._create_task",
        side_effect=RetryableReviewRegistration("pending"),
    )
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")

    with caplog.at_level("WARNING", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    mark.assert_not_called()
    assert "human-gate" in caplog.text


def _reconcile(tmp_path, state, repo="acme/repo"):
    return reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", repo=repo,
    )


def test_reconciliation_finish_to_gate_to_merged_closes_issue_and_completes_gate(
    tmp_path, mocker
):
    tasks = {
        "review-acceptance": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
    }
    view = {"state": "OPEN", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "a" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=tasks, view=view)
    create = mocker.patch("hermes_pipeline.todos_completion._create_task", return_value="human-id")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")
    checks = mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery",
                         return_value="pending")
    complete = mocker.patch("hermes_pipeline.todos_completion.complete_todo_kanban_task",
                            return_value=True)

    assert _reconcile(tmp_path, state)
    assert create.call_args.kwargs["key"] == "human-gate"
    assert create.call_args.kwargs["parent"] == "finish-id"
    mark.assert_called_once_with("human-id", f"Human merge required: {PR_URL}")
    close.assert_not_called()

    tasks["human-gate"] = SimpleNamespace(task_id="human-id", status="blocked")
    view["state"] = "MERGED"
    assert _reconcile(tmp_path, state)
    close.assert_called_once_with(
        project_dir=tmp_path, state_dir=state, tick_id="01TICK", issue_number=3,
        pr_number=7, pr_url=PR_URL, repo="acme/repo",
    )
    # A blocked tick before the gate existed must not have disabled the live check.
    import hermes_pipeline.todos_completion as module
    assert module._verify_finish.call_args.kwargs["require_current"] is False
    complete.assert_not_called()
    assert checks.call_count == 1

    close.return_value = "closed"
    assert _reconcile(tmp_path, state)
    complete.assert_called_once_with("demo", "human-id")
    assert close.call_count == 2
    assert create.call_count == 1


def test_reconciliation_never_creates_a_closeout_card(tmp_path, mocker):
    tasks = {
        "review-acceptance": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
    }
    view = {"state": "OPEN", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "a" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=tasks, view=view)
    create = mocker.patch("hermes_pipeline.todos_completion._create_task", return_value="human-id")
    mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")
    assert _reconcile(tmp_path, state)
    assert [c.kwargs["key"] for c in create.call_args_list] == ["human-gate"]
    assert not (state / "runs" / "01TICK" / "closeout-date").exists()


@pytest.mark.parametrize(
    ("check_state", "should_complete"),
    [pytest.param("passed", True, id="passed"), pytest.param("pending", False, id="pending")],
)
def test_merged_pr_creates_missing_gate_before_checks_without_remote_head(
    tmp_path, mocker, check_state, should_complete
):
    tasks = {
        "review-acceptance": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
    }
    view = {"state": "MERGED", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "a" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=tasks, view=view)
    remote_head = mocker.patch("hermes_pipeline.todos_completion._remote_head",
                               side_effect=ResultContractError("remote_branch_missing"))
    checks = mocker.patch("hermes_pipeline.todos_completion._check_state", return_value=check_state)
    create = mocker.patch("hermes_pipeline.todos_completion._create_task", return_value="human-id")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery",
                         return_value="closed")
    complete = mocker.patch("hermes_pipeline.todos_completion.complete_todo_kanban_task",
                            return_value=True)
    events = mocker.Mock()
    events.attach_mock(create, "create")
    events.attach_mock(checks, "checks")

    assert _reconcile(tmp_path, state)
    remote_head.assert_not_called()
    assert create.call_args.kwargs["key"] == "human-gate"
    assert create.call_args.kwargs["gate"] is True
    assert [event[0] for event in events.mock_calls] == ["create", "checks"]
    if should_complete:
        close.assert_called_once()
        complete.assert_called_once_with("demo", "human-id")
    else:
        close.assert_not_called()
        complete.assert_not_called()


def test_merged_pr_at_wrong_head_blocks_gate_without_touching_issue(tmp_path, mocker):
    tasks = {
        "review-acceptance": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
        "human-gate": SimpleNamespace(task_id="human-id", status="blocked"),
    }
    view = {"state": "MERGED", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "b" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=tasks, view=view)
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")

    assert _reconcile(tmp_path, state) is False
    mark.assert_called_once_with("human-id", "TPO delivery blocked: pr_head_drift")
    close.assert_not_called()


def test_gh_failure_during_issue_close_blocks_gate_and_retries_next_tick(tmp_path, mocker):
    tasks = {
        "review-acceptance": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
        "human-gate": SimpleNamespace(task_id="human-id", status="blocked"),
    }
    view = {"state": "MERGED", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "a" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=tasks, view=view)
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery",
                         side_effect=GitHubIssuesError("gh_auth", "issue close"))
    complete = mocker.patch("hermes_pipeline.todos_completion.complete_todo_kanban_task")

    assert _reconcile(tmp_path, state) is False
    mark.assert_called_once_with("human-id", "TPO delivery blocked: gh_auth")
    complete.assert_not_called()

    close.side_effect = None
    close.return_value = "closed"
    assert _reconcile(tmp_path, state)
    complete.assert_called_once_with("demo", "human-id")


def test_flag_issue_drift_marks_existing_human_gate_and_skips_creation(tmp_path, mocker):
    from hermes_pipeline.todos_completion import flag_issue_drift

    state = tmp_path / ".hermes"
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-1", worktree=tmp_path, prompt_client="codex"
        ),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": SimpleNamespace(task_id="t-1", status="done"),
            "human-gate": SimpleNamespace(task_id="gate-1", status="blocked"),
        },
    )
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")

    assert flag_issue_drift(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK",
        code="issue_drift", repo="acme/repo",
    ) is False

    create.assert_not_called()
    mark.assert_called_once_with("gate-1", "TPO delivery blocked: issue_drift")


def test_flag_issue_drift_creates_human_gate_under_an_existing_card(tmp_path, mocker):
    from hermes_pipeline.todos_completion import flag_issue_drift

    state = tmp_path / ".hermes"
    load = mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-1", worktree=tmp_path, prompt_client="codex"
        ),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={"plan:task-1": SimpleNamespace(task_id="t-1", status="in_progress")},
    )
    create = mocker.patch(
        "hermes_pipeline.todos_completion._create_task", return_value="gate-new"
    )
    mark = mocker.patch("hermes_pipeline.todos_completion._mark_gate_needs_input")

    assert flag_issue_drift(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK",
        code="issue_closed", repo="acme/repo",
    ) is False

    assert load.call_args.kwargs["repo"] == "acme/repo"
    assert create.call_args.kwargs["key"] == "human-gate"
    assert create.call_args.kwargs["parent"] == "t-1"
    assert create.call_args.kwargs["gate"] is True
    mark.assert_called_once_with("gate-new", "TPO delivery blocked: issue_closed")


def test_flag_issue_drift_without_cards_only_logs(tmp_path, mocker, caplog):
    from hermes_pipeline.todos_completion import flag_issue_drift

    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-1", worktree=tmp_path, prompt_client="codex"
        ),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks", return_value={}
    )
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")

    with caplog.at_level("WARNING"):
        assert flag_issue_drift(
            project_dir=tmp_path, state_dir=tmp_path / ".hermes", tenant="demo",
            tick_id="01TICK", code="issue_drift",
        ) is False
    create.assert_not_called()
    assert "issue_drift" in caplog.text


def test_reconcile_todo_completion_forwards_repo_to_registration_loader(tmp_path, mocker):
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    load = mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(manifest=None),
    )

    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", repo="acme/repo"
    )
    assert load.call_args.kwargs["repo"] == "acme/repo"


def test_flag_issue_drift_without_cards_persists_a_decision(tmp_path, mocker):
    import json

    from hermes_pipeline.todos_completion import flag_issue_drift

    state = tmp_path / ".hermes"
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(todo_id="TODO-1", worktree=tmp_path, prompt_client="codex"),
    )
    mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks", return_value={})

    assert flag_issue_drift(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", code="issue_closed",
    ) is False

    decision = json.loads((state / "decisions" / "01TICK-issue-drift.json").read_text())
    assert decision["picked"] is None
    assert decision["rationale"] == "tracker_error: issue_drift:issue_closed"


def test_flag_issue_drift_without_cards_survives_existing_decisions(tmp_path, mocker, caplog):
    """The tick's own decision file is write-once and already exists; drift must not raise."""
    from hermes_pipeline.todos_completion import flag_issue_drift

    state = tmp_path / ".hermes"
    (state / "decisions").mkdir(parents=True)
    (state / "decisions" / "01TICK.json").write_text("{}")
    (state / "decisions" / "01TICK-issue-drift.json").write_text("{}")
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(todo_id="TODO-1", worktree=tmp_path, prompt_client="codex"),
    )
    mocker.patch("hermes_pipeline.todos_completion.get_todo_kanban_tasks", return_value={})

    with caplog.at_level("DEBUG", logger="hermes_pipeline.todos_completion"):
        assert flag_issue_drift(
            project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", code="issue_drift",
        ) is False
    assert (state / "decisions" / "01TICK-issue-drift.json").read_text() == "{}"
    assert any(r.levelname == "DEBUG" and "already" in r.getMessage() for r in caplog.records)
