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


CHECKS_PR_URL = "https://github.com/acme/repo/pull/1"
CHECKS_REPO = "acme/repo"
CHECKS_HEAD_SHA = "c" * 40

# The exact stderr `gh pr checks --json state` writes for an empty status-check
# rollup, transcribed from gh 2.89.0 `pkg/cmd/pr/checks/checks.go`:
#   fmt.Errorf("no checks reported on the '%s' branch", pr.HeadRefName)
# Confirmed against live PRs (cli/cli#9000, yehiashouman/WearExerciseManager#4):
# exit 1, EMPTY stdout, this on stderr.
NO_CHECKS_STDERR = "no checks reported on the 'feature-branch' branch\n"


def _gh(
    mocker, *, returncode: int, stdout: str = "", stderr: str = "",
    suites: int = 0, statuses: int = 0, api_returncode: int = 0,
    api_stdout: str | None = None,
):
    """Fake `gh`, dispatching on argv: `gh pr checks` vs the corroborating `gh api`.

    ``suites``/``statuses`` are the ``total_count`` values the two REST endpoints
    report for the head commit; ``api_stdout``/``api_returncode`` override them to
    simulate an API that answers unusably.
    """
    calls: list[list[str]] = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["gh", "api"]:
            if api_stdout is not None:
                out = api_stdout
            else:
                out = f"{suites if argv[2].endswith('/check-suites') else statuses}\n"
            return SimpleNamespace(returncode=api_returncode, stdout=out, stderr="")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    patched = mocker.patch("hermes_pipeline.todos_completion.subprocess.run", side_effect=_run)
    patched.argv_calls = calls
    return patched


def _state(worktree):
    return _check_state(worktree, CHECKS_PR_URL, repo=CHECKS_REPO, head_sha=CHECKS_HEAD_SHA)


def test_repo_without_ci_is_green_rather_than_a_permanent_block(tmp_path, mocker):
    """gh's no-checks error must not wedge the human gate forever.

    gh reports an empty rollup as exit 1 with empty stdout, so the old
    ``json.loads(stdout)`` raised ``checks_unavailable`` and the issue was never
    closed, leaving ``registration_state`` ``active`` for good. Green only once
    the commit itself corroborates the absence.
    """
    run = _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, suites=0, statuses=0)
    assert _state(tmp_path) == "passed"
    api = [c for c in run.argv_calls if c[:2] == ["gh", "api"]]
    assert [c[2] for c in api] == [
        f"repos/{CHECKS_REPO}/commits/{CHECKS_HEAD_SHA}/check-suites",
        f"repos/{CHECKS_REPO}/commits/{CHECKS_HEAD_SHA}/status",
    ]


# P0-LOAD-BEARING, mirroring `test_pr_view_requests_only_fields_gh_supports`
# below. `--json state` is the PREMISE of `_check_state`'s whole model: gh's
# exporter returns before its exit-code tail, so with `--json` gh exits 0 whatever
# the checks say and a nonzero exit never carries a payload. Delete `--json state`
# and gh exits 1 on any failing check with human-readable text on stdout, which
# this module can only read as `checks_unavailable` -- the identical permanent
# wedge this work exists to remove, one token away. Ask for `--json bucket`
# instead and gh exits 1 with `Unknown JSON field`, the `baseRepository` defect
# verbatim. `cwd` is what binds the call to the project's `gh` auth and remote.
EXPECTED_CHECKS_ARGV = ["gh", "pr", "checks", CHECKS_PR_URL, "--json", "state"]


def test_check_state_argv_and_cwd_are_pinned(tmp_path, mocker):
    run = _gh(mocker, returncode=0, stdout='[{"state":"SUCCESS"}]')
    assert _state(tmp_path) == "passed"
    assert run.call_args.args[0] == EXPECTED_CHECKS_ARGV
    assert run.call_args.kwargs["cwd"] == tmp_path
    # A wedged `gh` must not hang the tick; UnicodeError is caught with it below.
    assert run.call_args.kwargs["timeout"] == 60


def test_corroboration_argv_and_cwd_are_pinned(tmp_path, mocker):
    """The corroborating REST calls are pinned like the checks call.

    They must address the head commit of the verified PR, count objects rather
    than re-read the rollup gh already called empty, and stay read-only.
    """
    run = _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, suites=0, statuses=0)
    assert _state(tmp_path) == "passed"
    api = [c for c in run.argv_calls if c[:2] == ["gh", "api"]]
    assert api == [
        ["gh", "api", f"repos/{CHECKS_REPO}/commits/{CHECKS_HEAD_SHA}/check-suites", "--jq", ".total_count"],
        ["gh", "api", f"repos/{CHECKS_REPO}/commits/{CHECKS_HEAD_SHA}/status", "--jq", ".total_count"],
    ]
    for call in run.call_args_list:
        assert call.kwargs["cwd"] == tmp_path
        assert call.kwargs["timeout"] == 60


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("failed to get checks: HTTP 500\n", id="server-error-mentions-checks"),
        pytest.param("no checks could be reported: HTTP 502\n", id="contains-no-checks"),
        pytest.param("error: nothing reported\n", id="contains-reported"),
        pytest.param("checks unavailable\n", id="contains-checks"),
        pytest.param("no checks reported\n", id="truncated-before-the-branch-clause"),
        pytest.param("", id="no-stderr-at-all"),
    ],
)
def test_only_ghs_exact_no_checks_line_can_mean_green(tmp_path, mocker, stderr):
    """A wrong-but-adjacent sentinel turns real gh failures into approvals.

    Broadening the match to `checks`, `no checks` or `reported` makes
    `failed to get checks: HTTP 500` with empty stdout return `passed`. Only gh's
    own `no checks reported on the '<branch>' branch` may mean an empty rollup,
    and the corroborating API must not even be consulted for anything else.
    """
    run = _gh(mocker, returncode=1, stderr=stderr)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)
    assert not [c for c in run.argv_calls if c[:2] == ["gh", "api"]]


def test_no_checks_line_only_counts_with_empty_stdout(tmp_path, mocker):
    """The empty-stdout precondition is half the rule and is pinned separately.

    gh cannot pair a payload with the no-checks error -- that error is returned
    before the exporter runs -- so a nonzero exit carrying both is an unmodelled
    gh, not a green build.
    """
    _gh(mocker, returncode=1, stdout='[{"state":"FAILURE"}]', stderr=NO_CHECKS_STDERR)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_startup_failure_rollup_is_not_green(tmp_path, mocker):
    """Live repro: yehiashouman/WearExerciseManager#4 @ 4c14b532.

    That repository HAS `.github/workflows/android.yml` on `pull_request`. The run
    concluded `failure` with zero jobs, so the rollup is empty and gh says "no
    checks reported" -- while `check-suites` reports `total_count: 1` (with
    `latest_check_runs_count: 0`) and `status` reports `total_count: 0`. CI was
    silently deleted; calling that green closes the issue on a red build. TPO's
    own workers edit `.github/workflows/*`, so this is a shape they can create.
    """
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, suites=1, statuses=0)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_commit_status_without_a_check_suite_is_not_green(tmp_path, mocker):
    """The other half of the corroboration: legacy commit statuses count too."""
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, suites=0, statuses=2)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"api_returncode": 1}, id="api-error"),
        pytest.param({"api_stdout": "null\n"}, id="api-count-missing"),
        pytest.param({"api_stdout": ""}, id="api-empty-output"),
    ],
)
def test_corroboration_failure_is_not_an_approval(tmp_path, mocker, kwargs):
    """An error while proving a negative is not proof of the negative."""
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, **kwargs)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(OSError("missing executable"), id="os-error"),
        pytest.param(subprocess.TimeoutExpired("gh", 60), id="timeout"),
        pytest.param(UnicodeError("undecodable output"), id="unicode-error"),
    ],
)
def test_corroboration_subprocess_failure_is_not_an_approval(tmp_path, mocker, outcome):
    def _run(argv, **kwargs):
        if argv[:2] == ["gh", "api"]:
            raise outcome
        return SimpleNamespace(returncode=1, stdout="", stderr=NO_CHECKS_STDERR)

    mocker.patch("hermes_pipeline.todos_completion.subprocess.run", side_effect=_run)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_no_checks_signal_must_be_anchored_not_a_substring(tmp_path, mocker):
    """`gh pr checks <arg>` echoes its argument, so a substring test is forgeable.

    An approval predicate must not depend on `pr_url` being validated two modules
    away, and the corroborating API must not even be consulted for this shape.
    """
    run = _gh(
        mocker, returncode=1,
        stderr="no pull requests found for branch \"no checks reported on the 'x' branch\"\n",
    )
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)
    assert not [c for c in run.argv_calls if c[:2] == ["gh", "api"]]


def test_gh_nonzero_unusable_response_needs_input(tmp_path, mocker):
    """A nonzero exit that is not the no-checks case is unreadable evidence."""
    _gh(mocker, returncode=1, stderr="auth failed")
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_nonzero_exit_never_classifies_a_payload(tmp_path, mocker):
    """Replaces a test that asserted exit 1 + JSON means "failed".

    With ``--json`` gh returns ``opts.Exporter.Write`` before the exit-code tail,
    so it never pairs a nonzero exit with a payload. Trusting stdout anyway would
    let ``[{"state":"SUCCESS"}]`` alongside an error exit approve a delivery.
    """
    _gh(mocker, returncode=1, stdout='[{"state":"SUCCESS"}]', stderr="checks failed")
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_exit_8_is_unreachable_with_json_and_is_not_pending(tmp_path, mocker):
    """Replaces a test that asserted exit 8 means "pending".

    ``PendingError`` (exit 8) is raised after the ``--json`` exporter has already
    returned, so ``gh pr checks --json`` cannot emit it. If it ever appears it is
    an unmodelled gh change, not a pending run.
    """
    _gh(mocker, returncode=8, stderr="pending")
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


@pytest.mark.parametrize("state", ["SUCCESS", "SKIPPED", "NEUTRAL"])
def test_gh_pass_and_skipping_buckets_are_green(tmp_path, mocker, state):
    """gh buckets SKIPPED and NEUTRAL as non-blocking "skipping"; NEUTRAL used to hang."""
    _gh(mocker, returncode=0, stdout=json.dumps([{"state": state}]))
    assert _state(tmp_path) == "passed"


def test_mixed_green_states_pass(tmp_path, mocker):
    _gh(mocker, returncode=0, stdout=json.dumps(
        [{"state": "SUCCESS"}, {"state": "SKIPPED"}, {"state": "NEUTRAL"}]
    ))
    assert _state(tmp_path) == "passed"


@pytest.mark.parametrize(
    "state",
    ["FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED",
     "STALE", "STARTUP_FAILURE"],
)
def test_terminal_non_success_states_fail_closed(tmp_path, mocker, state):
    """STALE and STARTUP_FAILURE are terminal; gh's pending bucket would hang us."""
    _gh(mocker, returncode=0, stdout=json.dumps([{"state": "SUCCESS"}, {"state": state}]))
    assert _state(tmp_path) == "failed"


@pytest.mark.parametrize(
    "state",
    ["EXPECTED", "REQUESTED", "WAITING", "QUEUED", "PENDING", "IN_PROGRESS",
     pytest.param("", id="completed-with-null-conclusion")],
)
def test_genuinely_transient_states_are_pending(tmp_path, mocker, state):
    """`""` is gh exporting a COMPLETED run whose conclusion has not landed yet.

    `aggregate.go` takes `state` from the conclusion once `status == "COMPLETED"`,
    so a null conclusion exports as the empty string and gh's default arm buckets
    it pending. Raising instead would block a delivery on something that resolves
    itself on the next tick.
    """
    _gh(mocker, returncode=0, stdout=json.dumps([{"state": "SUCCESS"}, {"state": state}]))
    assert _state(tmp_path) == "pending"


def test_failure_outranks_pending(tmp_path, mocker):
    _gh(mocker, returncode=0, stdout=json.dumps(
        [{"state": "IN_PROGRESS"}, {"state": "FAILURE"}]
    ))
    assert _state(tmp_path) == "failed"


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param('["SUCCESS"]', id="list-of-non-dicts"),
        # These two are where the strict per-item loop differs observably from
        # filtering non-dicts out: lenient filtering leaves {"SUCCESS"} and
        # answers `passed`, approving a delivery on a payload it half-read.
        pytest.param('[{"state":"SUCCESS"},"SUCCESS"]', id="non-dict-beside-a-green-dict"),
        pytest.param('[{"state":"SUCCESS"},42]', id="scalar-beside-a-green-dict"),
        pytest.param("[{}]", id="dicts-without-state"),
        pytest.param('[{"state":null}]', id="null-state"),
        pytest.param('[{"state":"SUCCESS"},{"state":null}]', id="one-unreadable-among-green"),
        pytest.param('[{"state":"WARP_SPEED"}]', id="state-gh-does-not-define"),
        pytest.param('[{"state":"SUCCESS"},{"state":"WARP_SPEED"}]', id="unknown-among-green"),
        pytest.param('{"state":"SUCCESS"}', id="not-a-list"),
        # Falsy non-lists: without the list guard these reach `if not checks` and
        # a bare `0` on stdout would approve the delivery.
        pytest.param("0", id="scalar-zero"),
        pytest.param("null", id="scalar-null"),
        pytest.param("{}", id="empty-object"),
        pytest.param('""', id="empty-string"),
        # Truthy non-lists: without the guard these escape as a bare TypeError,
        # crashing the gate instead of blocking it.
        pytest.param("5", id="scalar-int"),
        pytest.param("true", id="scalar-true"),
        pytest.param("not json", id="not-json"),
    ],
)
def test_unreadable_check_payload_is_never_green(tmp_path, mocker, stdout):
    """A payload we cannot interpret must block, never approve a delivery.

    ``["SUCCESS"]`` used to return "passed": the set comprehension dropped every
    non-dict, and ``set() <= {"SUCCESS", "SKIPPED"}`` is True.
    """
    _gh(mocker, returncode=0, stdout=stdout)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_empty_check_list_matches_the_no_ci_rule(tmp_path, mocker):
    """gh does not emit this shape, but "no checks at all" means the same thing.

    Same claim as an empty rollup, so it takes the same corroboration -- an
    approval must not rest on gh continuing to error rather than exporting `[]`.
    """
    run = _gh(mocker, returncode=0, stdout="[]", suites=0, statuses=0)
    assert _state(tmp_path) == "passed"
    api = [c for c in run.argv_calls if c[:2] == ["gh", "api"]]
    assert [c[2] for c in api] == [
        f"repos/{CHECKS_REPO}/commits/{CHECKS_HEAD_SHA}/check-suites",
        f"repos/{CHECKS_REPO}/commits/{CHECKS_HEAD_SHA}/status",
    ]


def test_empty_check_list_is_not_green_when_the_commit_has_suites(tmp_path, mocker):
    """The P0 reached through the other door: `[]` while the commit has 99 suites.

    If gh ever normalises its empty-output wart to `[]` with exit 0, a worker that
    breaks `.github/workflows/*` would otherwise close issues on red builds again,
    silently and permanently.
    """
    _gh(mocker, returncode=0, stdout="[]", suites=99, statuses=0)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_empty_check_list_is_not_green_when_corroboration_fails(tmp_path, mocker):
    _gh(mocker, returncode=0, stdout="[]", api_returncode=1)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(OSError("missing executable"), id="os-error"),
        pytest.param(subprocess.TimeoutExpired("gh", 60), id="timeout"),
        pytest.param(UnicodeError("undecodable output"), id="unicode-error"),
    ],
)
def test_gh_invocation_failure_needs_input(tmp_path, mocker, outcome):
    mocker.patch("hermes_pipeline.todos_completion.subprocess.run", side_effect=outcome)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


# gh 2.89.0 `gh pr view --json` field vocabulary, transcribed verbatim from
# `gh pr view --help`. Requesting anything outside this set makes gh exit 1 with
# `Unknown JSON field`, which _pr_view can only report as `pr_missing`.
#
# P0-LOAD-BEARING. Every integration test in this repository mocks `gh`, so this
# frozenset and its sibling argv test are the only checks standing between the
# codebase and a silent reintroduction of the `baseRepository` defect, where
# delivery verification could never succeed against a real PR. Update it only
# from `gh pr view --help` output, never to make a failing test pass.
GH_PR_VIEW_JSON_FIELDS = frozenset(
    """
    additions assignees author autoMergeRequest baseRefName baseRefOid body
    changedFiles closed closedAt closingIssuesReferences comments commits
    createdAt deletions files fullDatabaseId headRefName headRefOid
    headRepository headRepositoryOwner id isCrossRepository isDraft labels
    latestReviews maintainerCanModify mergeCommit mergeStateStatus mergeable
    mergedAt mergedBy milestone number potentialMergeCommit projectCards
    projectItems reactionGroups reviewDecision reviewRequests reviews state
    statusCheckRollup title updatedAt url
    """.split()
)


def _requested_pr_view_fields(run) -> set[str]:
    argv = run.call_args.args[0]
    return set(argv[argv.index("--json") + 1].split(","))


def test_pr_view_requests_only_fields_gh_supports(tmp_path, mocker):
    run = mocker.patch(
        "hermes_pipeline.todos_completion.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="{}"),
    )
    _pr_view(tmp_path, "https://github.com/acme/repo/pull/1")
    fields = _requested_pr_view_fields(run)
    assert fields - GH_PR_VIEW_JSON_FIELDS == set()


def test_pr_view_requests_every_field_delivery_verification_consumes(tmp_path, mocker):
    run = mocker.patch(
        "hermes_pipeline.todos_completion.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="{}"),
    )
    _pr_view(tmp_path, "https://github.com/acme/repo/pull/1")
    assert {
        "state", "url", "headRefName", "headRefOid", "baseRefName",
        "headRepository", "isCrossRepository",
    } <= _requested_pr_view_fields(run)


def _gh_shaped_view(**overrides) -> dict:
    """A view with the shape `gh pr view --json ...` actually emits."""
    view = {
        "state": "OPEN",
        "url": "https://github.com/acme/repo/pull/1",
        "headRefName": "feat/native",
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "headRepository": {"id": "R_x", "name": "repo", "nameWithOwner": "acme/repo"},
        "isCrossRepository": False,
    }
    view.update(overrides)
    return view


def test_pr_identity_accepts_real_gh_view_without_base_repository(tmp_path, mocker):
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    _verify_pr_identity(
        tmp_path, _gh_shaped_view(), branch="feat/native", repo="acme/repo"
    )


def test_pr_identity_matches_project_repo_case_insensitively(tmp_path, mocker):
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    _verify_pr_identity(
        tmp_path, _gh_shaped_view(), branch="feat/native", repo="ACME/Repo"
    )


ABSENT = object()
"""Parametrization marker: drop the key entirely rather than override it."""


def _mutate(view: dict, override: dict) -> dict:
    """Apply an override, where ``ABSENT`` deletes the key instead of setting it."""
    mutated = view | {k: v for k, v in override.items() if v is not ABSENT}
    for key, value in override.items():
        if value is ABSENT:
            mutated.pop(key, None)
    return mutated


@pytest.mark.parametrize(
    "view",
    [
        pytest.param({"headRefName": "feat/other"}, id="wrong-head-branch"),
        pytest.param({"headRefName": ABSENT}, id="no-head-branch"),
        pytest.param({"baseRefName": "release"}, id="wrong-base-branch"),
        pytest.param({"baseRefName": ABSENT}, id="no-base-branch"),
        pytest.param(
            {"headRepository": {"nameWithOwner": "fork/repo"}}, id="fork-head"
        ),
        pytest.param({"headRepository": None}, id="no-head-repository"),
        pytest.param({"headRepository": ABSENT}, id="absent-head-repository"),
        pytest.param({"isCrossRepository": True}, id="cross-repository-fork-pr"),
        pytest.param({"isCrossRepository": "false"}, id="cross-repository-not-bool"),
        pytest.param({"isCrossRepository": 0}, id="cross-repository-falsy-int"),
        pytest.param({"isCrossRepository": None}, id="cross-repository-null"),
        # `good | override` can never delete a key, so absence needs ABSENT. Without
        # these two params, `view.get("isCrossRepository", False) is not False`
        # survives with the suite green -- and that default reopens the P0 for any
        # degraded or older-gh payload that omits the field.
        pytest.param({"isCrossRepository": ABSENT}, id="cross-repository-absent"),
    ],
)
def test_pr_identity_requires_registered_origin_base_and_repo(tmp_path, mocker, view):
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    good = _gh_shaped_view()
    _verify_pr_identity(tmp_path, good, branch="feat/native", repo="acme/repo")
    with pytest.raises(ResultContractError, match="pr_identity_mismatch"):
        _verify_pr_identity(
            tmp_path, _mutate(good, view), branch="feat/native", repo="acme/repo"
        )


@pytest.mark.parametrize(
    ("origin", "head_repo"),
    [
        pytest.param("Acme/Repo", "acme/repo", id="origin-url-typed-in-mixed-case"),
        pytest.param("acme/repo", "Acme/Repo", id="gh-canonical-case-differs"),
        pytest.param("acme/REPO", "ACME/repo", id="both-sides-differ"),
    ],
)
def test_pr_identity_compares_head_repository_case_insensitively(
    tmp_path, mocker, origin, head_repo
):
    """`repository` carries whatever case the operator typed into the origin remote
    URL; `headRepository.nameWithOwner` carries GitHub's canonical case. Comparing
    them case-sensitively rejects a legitimate same-repo PR forever -- the same
    never-succeeds failure class as the `baseRepository` defect, and latent only
    while nothing got past `_pr_view` to reach this line.
    """
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=(origin, "main"),
    )
    view = _gh_shaped_view(headRepository={"nameWithOwner": head_repo})
    _verify_pr_identity(tmp_path, view, branch="feat/native", repo="acme/repo")


def test_pr_identity_rejects_origin_that_is_not_the_project_repo(tmp_path, mocker):
    """Defence in depth, redundant with `_delivery_authority` in production.

    `_delivery_authority` already requires `origin_repository == repo` when it
    writes the pin, and `reconcile_todo_completion` re-checks `_github_identity`
    against that pin before calling here, so an origin that disagrees with `repo`
    cannot actually reach this clause. Reaching it requires mocking
    `_github_identity` into a state the caller makes impossible; the clause is
    kept as a fail-closed backstop, not because this branch is reachable.
    """
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("other/repo", "main"),
    )
    view = _gh_shaped_view(headRepository={"nameWithOwner": "other/repo"})
    with pytest.raises(ResultContractError, match="pr_identity_mismatch"):
        _verify_pr_identity(tmp_path, view, branch="feat/native", repo="acme/repo")


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


def test_finish_card_publishes_the_delivery_result_template(tmp_path, mocker):
    from hermes_pipeline.result_contract import render_result_template

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
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK",
        repo="acme/repo",
    )

    assert render_result_template(
        tick_id="01TICK", todo_id="TODO-1", step_key="finish",
        section="delivery", pinned_head_sha="a" * 40, branch="feat/native",
        allow_no_changes=True,
    ) in create.call_args.kwargs["prompt"]
