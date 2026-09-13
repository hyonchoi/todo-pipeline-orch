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


def _delivery_registration(tmp_path):
    return SimpleNamespace(
        todo_id="TODO-1", worktree=tmp_path, branch="feat/native",
        assignee="worker", prompt_client="codex", profile="native-sdd",
        plan_hash="f" * 64,
        plan_reference=SimpleNamespace(value="docs/plan.md"),
    )


def test_delivery_waits_for_the_accepted_review_head(tmp_path, mocker):
    """No accepted head on disk means the review is not clean yet."""
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={"review:0": SimpleNamespace(status="done")},
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
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=_delivery_registration(tmp_path),
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
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", repo="acme/repo"
    )
    assert create.call_args.kwargs["key"] == "finish"
    # No parent: TPO creates the card exactly when its prerequisite is proven.
    assert "parent" not in create.call_args.kwargs
    assert "Do not merge the pull request" in create.call_args.kwargs["prompt"]


def test_finish_card_renders_the_profile_phase_verbatim_with_its_limits(
    tmp_path, mocker
):
    """``phase_8_finish_branch`` is the specification for the delivery card.

    Its prompt, tools, turn budget and timeout all come from the profile: the
    prompt TPO used to author here told the worker to add no commit at all,
    which contradicts the profile's own "commit those as one separate atomic
    commit" and made the live harness untestable against the profile.
    """
    from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

    phase = next(
        p for p in load_phases(resolve_profile_phases_path("native-sdd"))
        if p.phase_key == "phase_8_finish_branch"
    )
    state = tmp_path / ".hermes"
    (state / "runs" / "01TICK").mkdir(parents=True)
    (state / "runs" / "01TICK" / "registration.json").write_text("{}")
    (state / "runs" / "01TICK" / "accepted-review-head").write_text("a" * 40)
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=_delivery_registration(tmp_path),
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

    kwargs = create.call_args.kwargs
    assert kwargs["tools"] == phase.tools == "Read,Write,Edit,Bash"
    assert kwargs["turns"] == phase.turns == 30
    assert kwargs["timeout"] == phase.timeout == 2400
    assert kwargs["title"] == phase.name
    body = phase.prompt.format(
        todo_id="TODO-1", tick_id="01TICK", project_slug="demo",
        plan_path="docs/plan.md", agent_product="Codex", skill_prefix="$",
        superpowers_skill_prefix="$superpowers:",
    )
    assert kwargs["prompt"].endswith(body)
    header = kwargs["prompt"].removesuffix(body)
    assert f"- accepted_review_head_sha: {'a' * 40}\n" in header
    assert "- branch: feat/native\n" in header
    # The instruction TPO used to author in place of the profile's is gone.
    assert "do not modify the worktree or add any commit" not in kwargs["prompt"]


def _finish_repo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _finish_commit(repo, name):
    (repo / name).write_text(name)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", name], cwd=repo, check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True,
        text=True,
    ).stdout.strip()


def _finish_result(parent, head, changed=()):
    return SimpleNamespace(git=SimpleNamespace(
        expected_parent_sha=parent, resulting_head_sha=head,
        task_commit_sha=head, changed_files=tuple(changed),
    ))


def test_finish_accepts_the_one_metadata_commit_the_profile_mandates(tmp_path):
    """The profile requires this commit, so verification must not reject it."""
    repo = _finish_repo(tmp_path, "one-commit")
    accepted = _finish_commit(repo, "reviewed.txt")
    head = _finish_commit(repo, "CHANGELOG.md")

    _verify_finish(
        repo, _finish_result(accepted, head, ("CHANGELOG.md",)), accepted,
        require_current=True,
    )


def test_finish_rejects_two_commits_past_the_accepted_head(tmp_path):
    """One commit is the allowance; a second is work no review ever saw."""
    repo = _finish_repo(tmp_path, "two-commits")
    accepted = _finish_commit(repo, "reviewed.txt")
    _finish_commit(repo, "CHANGELOG.md")
    head = _finish_commit(repo, "sneaky.py")

    # Asserted on the FULL detail, not ``match="finish_review_head_mismatch"``:
    # that is the wrapper code ``_verify_finish`` emits for every inner failure,
    # so a ``re.search`` for it cannot tell ``commit_count_mismatch`` from
    # ``parent_mismatch`` -- widening the accepted count bound to
    # ``("0", "1", "2")`` did not kill this test. Same discipline as
    # ``test_finish_reports_a_broken_git_as_broken_not_as_a_head_mismatch``.
    with pytest.raises(ResultContractError) as exc_info:
        _verify_finish(
            repo, _finish_result(accepted, head, ("CHANGELOG.md", "sneaky.py")),
            accepted, require_current=True,
        )
    assert str(exc_info.value) == "finish_review_head_mismatch: commit_count_mismatch"


def test_finish_rejects_a_head_that_does_not_descend_from_the_accepted_head(tmp_path):
    """The anchor is what stands between a forged history and a blessed delivery."""
    repo = _finish_repo(tmp_path, "forged")
    accepted = _finish_commit(repo, "reviewed.txt")
    subprocess.run(
        ["git", "checkout", "-q", "--orphan", "forged"], cwd=repo, check=True,
        capture_output=True,
    )
    subprocess.run(["git", "rm", "-q", "-rf", "."], cwd=repo, check=True,
                   capture_output=True)
    head = _finish_commit(repo, "rewritten.txt")

    # Full detail, for the reason recorded on the two-commit test above: the
    # wrapper code alone is emitted by every inner failure and pins nothing.
    with pytest.raises(ResultContractError) as exc_info:
        _verify_finish(
            repo, _finish_result(accepted, head, ("rewritten.txt",)), accepted,
            require_current=True,
        )
    assert str(exc_info.value) == "finish_review_head_mismatch: parent_mismatch"


def test_finish_reports_a_fabricated_sha_as_a_bad_report_not_as_broken_git(tmp_path):
    """An invented SHA is the worker's fault, and must be attributed to it.

    An unknown object makes every topology query exit 128, which every
    git-failure helper in ``result_contract`` collapses into
    ``git_verification_failed`` -- and ``_verify_finish`` passes that code
    straight through on purpose, so an operator reads a broken worktree as
    broken. A worker that simply made its 40-hex SHA up was therefore reported
    as broken infrastructure. The old string comparison said
    ``finish_review_head_mismatch``, so this was an attribution regression.
    """
    repo = _finish_repo(tmp_path, "fabricated-sha")
    accepted = _finish_commit(repo, "reviewed.txt")

    with pytest.raises(ResultContractError) as exc_info:
        _verify_finish(
            repo, _finish_result(accepted, "9" * 40, ("CHANGELOG.md",)), accepted,
            require_current=True,
        )
    assert str(exc_info.value) == "finish_review_head_mismatch: unknown_commit"


def test_finish_evidence_is_not_rechecked_against_live_head_once_verified(tmp_path):
    """A resumed tick re-reads the report without demanding the live worktree.

    The topology facts still hold -- they survive a later commit -- so only the
    current-HEAD and cleanliness checks are dropped.
    """
    repo = _finish_repo(tmp_path, "already-verified")
    accepted = _finish_commit(repo, "reviewed.txt")
    head = _finish_commit(repo, "CHANGELOG.md")
    _finish_commit(repo, "later.txt")

    _verify_finish(
        repo, _finish_result(accepted, head, ("CHANGELOG.md",)), accepted,
        require_current=False,
    )


CHECKS_PR_URL = "https://github.com/acme/repo/pull/1"
CHECKS_REPO = "acme/repo"
CHECKS_HEAD_SHA = "c" * 40

# The exact stderr `gh pr checks --json state` writes for an empty status-check
# rollup, transcribed from gh 2.89.0 `pkg/cmd/pr/checks/checks.go`:
#   fmt.Errorf("no checks reported on the '%s' branch", pr.HeadRefName)
# Confirmed against live PRs (cli/cli#9000, yehiashouman/WearExerciseManager#4):
# exit 1, EMPTY stdout, this on stderr.
NO_CHECKS_STDERR = "no checks reported on the 'feature-branch' branch\n"


# The projection `_rollup_is_honestly_empty` asks gh for, pinned here and
# asserted verbatim by `test_corroboration_argv_and_cwd_are_pinned`. The fixtures
# below are the RAW REST payloads GitHub serves and `_project_check_suites`
# applies this filter the way gh's gojq does, so no test can see a check-suites
# shape the live endpoint cannot produce.
CHECK_SUITES_JQ = (
    "{total: .total_count, suites: [.check_suites[] | "
    "{runs: .latest_check_runs_count, conclusion: .conclusion, "
    "status: .status, app: .app.slug}]}"
)
CHECK_SUITES_ENDPOINT = (
    f"repos/{CHECKS_REPO}/commits/{CHECKS_HEAD_SHA}/check-suites?per_page=100"
)
STATUS_ENDPOINT = f"repos/{CHECKS_REPO}/commits/{CHECKS_HEAD_SHA}/status"


def _suite(app: str, *, runs: int = 0, conclusion=None, status: str = "queued") -> dict:
    """One element of `check_suites`, carrying the field set GitHub really serves.

    Verified read-only against live commits on 2026-09-05. Defaults reproduce the
    shape GitHub creates for EVERY installed App subscribing to `check_suite`,
    whether or not that App ever produces a check run -- verbatim
    ``{"latest_check_runs_count": 0, "conclusion": null, "status": "queued"}``:

      dependabot/dependabot-core  github-service-catalog, github-service-catalog-staging, sentry
      sindresorhus/got            codecov, claude
      prettier/prettier           codecov, netlify, circleci-checks, renovate, vercel,
                                  autofix-ci, relativeci
      astral-sh/uv                renovate
      pallets/flask               read-the-docs-community

    The extra fields are kept because the code must ignore them: a projection
    that reads `.conclusion` off the wrong object, or a fixture trimmed down to
    only the fields the rule happens to use, is how this file has been fooled
    before.
    """
    return {
        "id": 41837291057,
        "node_id": "CS_kwDOAA5QjM8AAAAJvBOxsQ",
        "head_branch": "feature-branch",
        "head_sha": CHECKS_HEAD_SHA,
        "status": status,
        "conclusion": conclusion,
        "url": f"https://api.github.com/repos/{CHECKS_REPO}/check-suites/41837291057",
        "before": "b" * 40,
        "after": CHECKS_HEAD_SHA,
        "pull_requests": [],
        "app": {
            "id": 15368,
            "slug": app,
            "node_id": "MDM6QXBwMTUzNjg=",
            "owner": {"login": app, "id": 9919, "type": "Organization"},
            "name": app,
            "events": ["check_suite", "pull_request", "push"],
        },
        "created_at": "2026-09-05T09:12:44Z",
        "updated_at": "2026-09-05T09:12:44Z",
        "rerequestable": True,
        "runs_rerequestable": False,
        "latest_check_runs_count": runs,
        "check_runs_url": (
            f"https://api.github.com/repos/{CHECKS_REPO}"
            "/check-suites/41837291057/check-runs"
        ),
        "head_commit": {
            "id": CHECKS_HEAD_SHA, "tree_id": "d" * 40, "message": "worker delivery",
        },
        "repository": {"id": 938636, "name": "repo", "full_name": CHECKS_REPO},
    }


# The startup failure, transcribed from the live repro this corroboration exists
# for: `yehiashouman/WearExerciseManager` @ 4c14b532d7da2a99a9e3b337fece90a5336fdc43,
# whose `check-suites` reports `total_count: 1` with a single `github-actions`
# suite `{"latest_check_runs_count": 0, "status": "completed", "conclusion":
# "failure"}` while `status` reports `total_count: 0`.
STARTUP_FAILURE_SUITE = _suite("github-actions", conclusion="failure", status="completed")


def _project_check_suites(payload: dict) -> str:
    """Apply ``CHECK_SUITES_JQ`` to a raw payload as gh's gojq + encoder would.

    gh writes each jq result with `json.Encoder.Encode`: compact separators, one
    line, trailing newline.
    """
    return json.dumps(
        {
            "total": payload["total_count"],
            "suites": [
                {
                    "runs": suite["latest_check_runs_count"],
                    "conclusion": suite["conclusion"],
                    "status": suite["status"],
                    # jq's `.app.slug` yields null for a null `app`.
                    "app": (suite["app"] or {}).get("slug"),
                }
                for suite in payload["check_suites"]
            ],
        },
        separators=(",", ":"),
    ) + "\n"


def _gh(
    mocker, *, returncode: int, stdout: str = "", stderr: str = "",
    suites: tuple = (), suites_total: int | None = None, statuses: int = 0,
    api_returncode: int = 0, api_stdout: str | None = None,
):
    """Fake `gh`, dispatching on argv: `gh pr checks` vs the corroborating `gh api`.

    ``suites`` are raw `check_suites` elements (see ``_suite``) for the head
    commit, served through ``_project_check_suites``; ``suites_total`` overrides
    the envelope's ``total_count`` to simulate a page that does not hold every
    suite. ``statuses`` is the ``total_count`` of the legacy status endpoint.
    ``api_stdout``/``api_returncode`` override both to simulate an API that
    answers unusably.
    """
    calls: list[list[str]] = []

    def _run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["gh", "api"]:
            if api_stdout is not None:
                out = api_stdout
            elif "/check-suites" in argv[2]:
                out = _project_check_suites({
                    "total_count": len(suites) if suites_total is None else suites_total,
                    "check_suites": list(suites),
                })
            else:
                out = f"{statuses}\n"
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
    run = _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, suites=(), statuses=0)
    assert _state(tmp_path) == "passed"
    api = [c for c in run.argv_calls if c[:2] == ["gh", "api"]]
    assert [c[2] for c in api] == [CHECK_SUITES_ENDPOINT, STATUS_ENDPOINT]


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
    run = _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, suites=(), statuses=0)
    assert _state(tmp_path) == "passed"
    api = [c for c in run.argv_calls if c[:2] == ["gh", "api"]]
    assert api == [
        ["gh", "api", CHECK_SUITES_ENDPOINT, "--jq", CHECK_SUITES_JQ],
        ["gh", "api", STATUS_ENDPOINT, "--jq", ".total_count"],
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


def test_benign_app_suite_without_runs_is_green(tmp_path, mocker):
    """A repo with Apps installed but no reporting workflow must still progress.

    GitHub opens a check suite for every installed App subscribing to
    `check_suite`, run or no run, so `total_count >= 1` says nothing about
    whether any gate exists. Requiring `total_count == 0` therefore wedged every
    such repository -- `checks_unavailable` on every tick, forever, waiting for a
    human who is not coming. These are prettier/prettier's live suites.
    """
    run = _gh(
        mocker, returncode=1, stderr=NO_CHECKS_STDERR, statuses=0,
        suites=tuple(_suite(app) for app in (
            "codecov", "netlify", "circleci-checks", "renovate", "vercel",
            "autofix-ci", "relativeci",
        )),
    )
    assert _state(tmp_path) == "passed"
    api = [c for c in run.argv_calls if c[:2] == ["gh", "api"]]
    assert [c[2] for c in api] == [CHECK_SUITES_ENDPOINT, STATUS_ENDPOINT]


def test_startup_failure_rollup_is_not_green(tmp_path, mocker):
    """Live repro: yehiashouman/WearExerciseManager#4 @ 4c14b532.

    That repository HAS `.github/workflows/android.yml` on `pull_request`. The run
    concluded `failure` with zero jobs, so the rollup is empty and gh says "no
    checks reported" -- while `check-suites` reports one `github-actions` suite
    with `latest_check_runs_count: 0`, `status: completed`, `conclusion: failure`
    and `status` reports `total_count: 0`. CI was silently deleted; calling that
    green closes the issue on a red build. TPO's own workers edit
    `.github/workflows/*`, so this is a shape they can create.
    """
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, statuses=0,
        suites=(STARTUP_FAILURE_SUITE,))
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_zero_run_suite_with_a_conclusion_is_not_green(tmp_path, mocker):
    """The startup-failure family, isolated from the app-slug rule.

    Zero runs plus a NON-NULL conclusion is the discriminator: the App was asked,
    answered, and produced nothing to read. A benign App suite never carries a
    conclusion. Held for third-party Apps too, so this stays a rule about the
    shape rather than a special case for `github-actions`.
    """
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, statuses=0,
        suites=(_suite("codecov", conclusion="failure", status="completed"),))
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_github_actions_suite_without_runs_is_not_green(tmp_path, mocker):
    """A zero-run `github-actions` suite is ambiguous, so it stays fail-closed.

    It is either workflows about to start (the brief race just after a push) or
    workflows that will never report, and nothing in the payload separates them.
    It is also the same App the startup failure arrives under. Reading it as "no
    gate" is the risky half of the ambiguity, so it is refused; third-party Apps
    are not ambiguous, they were notified and did nothing.
    """
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, statuses=0,
        suites=(_suite("github-actions"),))
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_suite_from_an_unidentifiable_app_is_not_green(tmp_path, mocker):
    """`.app.slug` null leaves condition 4 unverifiable, so it fails closed.

    GitHub serves a null `app` for a suite whose App has been deleted or
    suspended; a null slug cannot be shown NOT to be `github-actions`.
    """
    suite = _suite("codecov")
    suite["app"] = None
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, statuses=0, suites=(suite,))
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_suite_that_produced_runs_is_not_green(tmp_path, mocker):
    """A suite with check runs contradicts the empty rollup gh just reported.

    Two readings, both fatal: gh's rollup is stale, or it is reading a different
    commit. Either way there ARE runs on this head and their states were never
    classified, so it cannot be waved through.
    """
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, statuses=0,
        suites=(_suite("codecov"), _suite("github-service-catalog", runs=3, status="in_progress")))
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_unseen_suites_are_not_green(tmp_path, mocker):
    """A page that does not hold every suite cannot clear every suite.

    The per-suite rule is only as good as the suites it saw, so a `total_count`
    larger than the returned page is unread evidence, not absence.
    """
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, statuses=0,
        suites=(_suite("codecov"),), suites_total=2)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_commit_status_without_a_check_suite_is_not_green(tmp_path, mocker):
    """The other half of the corroboration: legacy commit statuses count too."""
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, suites=(), statuses=2)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


def test_benign_suites_do_not_excuse_a_commit_status(tmp_path, mocker):
    """Condition 1 survives condition 2-4 passing: statuses are a real gate."""
    _gh(mocker, returncode=1, stderr=NO_CHECKS_STDERR, suites=(_suite("codecov"),), statuses=2)
    with pytest.raises(ResultContractError, match="checks_unavailable"):
        _state(tmp_path)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"api_returncode": 1}, id="api-error"),
        pytest.param({"api_stdout": "null\n"}, id="api-count-missing"),
        pytest.param({"api_stdout": ""}, id="api-empty-output"),
        pytest.param({"api_stdout": "{oops\n"}, id="api-unparseable"),
        pytest.param(
            {"api_stdout": '{"total":1,"suites":[["codecov",0]]}\n'},
            id="api-suite-is-not-an-object",
        ),
        pytest.param(
            {"api_stdout": '{"total":1,"suites":[{"runs":"0","conclusion":null,"app":"codecov"}]}\n'},
            id="api-run-count-is-not-a-number",
        ),
        pytest.param(
            {"api_stdout": '{"total":1,"suites":[{"runs":0,"conclusion":{},"app":"codecov"}]}\n'},
            id="api-conclusion-is-not-a-string",
        ),
        pytest.param(
            {"api_stdout": '{"total":"1","suites":[]}\n'},
            id="api-total-is-not-a-number",
        ),
    ],
)
def test_corroboration_failure_is_not_an_approval(tmp_path, mocker, kwargs):
    """An error while proving a negative is not proof of the negative.

    Every unreadable envelope and unreadable suite record lands here rather than
    being skipped: a record this code cannot type-check is a record whose runs
    and conclusion it did not actually verify.
    """
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
    run = _gh(mocker, returncode=0, stdout="[]", suites=(), statuses=0)
    assert _state(tmp_path) == "passed"
    api = [c for c in run.argv_calls if c[:2] == ["gh", "api"]]
    assert [c[2] for c in api] == [CHECK_SUITES_ENDPOINT, STATUS_ENDPOINT]


def test_empty_check_list_is_not_green_when_the_commit_has_a_broken_workflow(tmp_path, mocker):
    """The P0 reached through the other door: `[]` alongside a startup failure.

    If gh ever normalises its empty-output wart to `[]` with exit 0, a worker that
    breaks `.github/workflows/*` would otherwise close issues on red builds again,
    silently and permanently.
    """
    _gh(mocker, returncode=0, stdout="[]", suites=(STARTUP_FAILURE_SUITE,), statuses=0)
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
# A DIFFERENT pull request in the same repository, so it clears the `pr_url`
# regex and only the echoed `url` can tell it apart from the delivered PR.
OTHER_PR_URL = f"https://github.com/{REPO}/pull/8"
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
    mocker.patch("hermes_pipeline.todos_completion.github_issues.check_issue_drift", return_value=None)
    return state


def _finish_tasks():
    return {
        "review:0": SimpleNamespace(task_id="review", status="done"),
        "finish": SimpleNamespace(task_id="finish-id", status="done"),
    }


def _view(state="OPEN", head="a" * 40, url=PR_URL):
    return {"state": state, "url": url, "headRefName": "feat/native", "headRefOid": head}


@pytest.mark.parametrize(
    ("view", "code"),
    [
        pytest.param(_view("CLOSED"), "pull_request_closed_or_drifted", id="closed"),
        pytest.param(_view("OPEN", "b" * 40), "pr_head_drift", id="open-drifted"),
        pytest.param(_view("MERGED", "b" * 40), "pr_head_drift", id="merged-drifted"),
        # `gh pr view` echoes the PR it actually read back as `url`. Delivery must
        # confirm it is the PR the delivery named: everything downstream -- the
        # merge state, the head, `_check_state`, and the issue close -- is then
        # measured on whatever PR gh answered with.
        pytest.param(_view("MERGED", url=OTHER_PR_URL),
                     "pr_identity_mismatch", id="url-mismatch"),
    ],
)
def test_pr_state_guards_stall_delivery_and_never_touch_the_issue(
    tmp_path, mocker, caplog, view, code
):
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=view)
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")
    checks = mocker.patch("hermes_pipeline.todos_completion._check_state")

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    assert code in caplog.text
    # No card is created and no status is forced: the False return is the signal.
    create.assert_not_called()
    close.assert_not_called()
    checks.assert_not_called()


def test_remote_head_drift_stalls_delivery(tmp_path, mocker, caplog):
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=_view())
    remote_head = mocker.patch("hermes_pipeline.todos_completion._remote_head", return_value="c" * 40)
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    remote_head.assert_called_once_with(tmp_path, "feat/native")
    assert "remote_head_drift" in caplog.text
    create.assert_not_called()


@pytest.mark.parametrize(
    "pr_url",
    [
        pytest.param("https://github.com/other/repo/pull/7", id="other-repo"),
        pytest.param("https://github.com/acme/repo/pulls/7", id="not-a-pull-path"),
    ],
)
def test_pr_url_outside_the_project_repo_blocks_before_any_pr_read(
    tmp_path, mocker, caplog, pr_url
):
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=_view(url=pr_url))
    import hermes_pipeline.todos_completion as module

    module.parse_worker_result.return_value.delivery.pr_url = pr_url
    view = mocker.patch("hermes_pipeline.todos_completion._pr_view")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    view.assert_not_called()
    close.assert_not_called()
    assert "pr_identity_mismatch" in caplog.text


def test_open_pr_with_green_checks_keeps_waiting_for_the_human(tmp_path, mocker):
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=_view())
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")

    assert _reconcile(tmp_path, state) is True
    create.assert_not_called()
    close.assert_not_called()


def test_poisoned_worktree_origin_blocks_delivery_without_gh_writes(
    tmp_path, mocker, caplog, fake_gh
):
    """A worktree-scoped ``url.insteadOf`` cannot redirect delivery to another repo."""
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=_view("MERGED"))
    mocker.patch("hermes_pipeline.todos_completion._github_identity", return_value=("evil/repo", "main"))
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    assert "delivery_authority_drift" in caplog.text
    assert fake_gh.calls == []


def test_finish_live_check_is_skipped_only_after_a_verified_marker(tmp_path, mocker):
    """The marker latches a *local worktree* check no card status records."""
    import hermes_pipeline.todos_completion as module

    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=_view())
    marker = state / "runs" / "01TICK" / "finish-verified"
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="pending")
    module._verify_finish.side_effect = ResultContractError("finish_review_head_mismatch")

    assert _reconcile(tmp_path, state) is False
    assert module._verify_finish.call_args.kwargs["require_current"] is True
    assert not marker.exists()

    module._verify_finish.side_effect = None
    assert _reconcile(tmp_path, state) is True
    assert module._verify_finish.call_args.kwargs["require_current"] is True
    assert marker.exists()

    assert _reconcile(tmp_path, state) is True
    assert module._verify_finish.call_args.kwargs["require_current"] is False


def test_unsafe_pr_url_is_refused_before_any_pr_read(tmp_path, mocker, caplog):
    import hermes_pipeline.todos_completion as module

    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=_view())
    module.parse_worker_result.return_value.delivery.pr_url = PR_URL + "\x07"
    module._pr_view.side_effect = lambda *_a: _view(url=PR_URL + "\x07")

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    module._pr_view.assert_not_called()
    assert "pr_identity_mismatch" in caplog.text


def _reconcile(tmp_path, state, repo="acme/repo"):
    return reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK", repo=repo,
    )


def test_reconciliation_open_then_merged_closes_the_issue_without_a_gate_card(
    tmp_path, mocker
):
    tasks = _finish_tasks()
    view = {"state": "OPEN", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "a" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=tasks, view=view)
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    checks = mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery",
                         return_value="pending")

    # Open, green: the delivery is verified and simply waits for the human merge.
    assert _reconcile(tmp_path, state)
    create.assert_not_called()
    close.assert_not_called()

    view["state"] = "MERGED"
    assert _reconcile(tmp_path, state)
    close.assert_called_once_with(
        project_dir=tmp_path, state_dir=state, tick_id="01TICK", issue_number=3,
        pr_number=7, pr_url=PR_URL, repo="acme/repo",
    )
    import hermes_pipeline.todos_completion as module
    assert module._verify_finish.call_args.kwargs["require_current"] is False

    close.return_value = "closed"
    assert _reconcile(tmp_path, state)
    assert close.call_count == 2
    # Checks are read on every pass, including the open-and-green one.
    assert checks.call_count == 3
    create.assert_not_called()


def test_reconciliation_never_creates_a_closeout_card(tmp_path, mocker):
    view = {"state": "OPEN", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "a" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=view)
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    assert _reconcile(tmp_path, state)
    create.assert_not_called()
    assert not (state / "runs" / "01TICK" / "closeout-date").exists()


@pytest.mark.parametrize(
    ("check_state", "should_close"),
    [pytest.param("passed", True, id="passed"), pytest.param("pending", False, id="pending")],
)
def test_merged_pr_reads_checks_without_the_remote_head(
    tmp_path, mocker, check_state, should_close
):
    view = {"state": "MERGED", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "a" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=view)
    remote_head = mocker.patch("hermes_pipeline.todos_completion._remote_head",
                               side_effect=ResultContractError("remote_branch_missing"))
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value=check_state)
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery",
                         return_value="closed")

    assert _reconcile(tmp_path, state)
    remote_head.assert_not_called()
    create.assert_not_called()
    if should_close:
        close.assert_called_once()
    else:
        close.assert_not_called()


def test_merged_pr_at_wrong_head_stalls_without_touching_the_issue(tmp_path, mocker, caplog):
    view = {"state": "MERGED", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "b" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=view)
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    assert "pr_head_drift" in caplog.text
    close.assert_not_called()


def test_gh_failure_during_issue_close_stalls_then_recovers_next_tick(tmp_path, mocker, caplog):
    view = {"state": "MERGED", "url": PR_URL, "headRefName": "feat/native", "headRefOid": "a" * 40}
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=view)
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery",
                         side_effect=GitHubIssuesError("gh_auth", "issue close"))

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    assert "gh_auth" in caplog.text

    close.side_effect = None
    close.return_value = "closed"
    assert _reconcile(tmp_path, state)
    assert close.call_count == 2


def test_flag_issue_drift_stalls_and_creates_no_card(tmp_path, mocker, caplog):
    from hermes_pipeline.todos_completion import flag_issue_drift

    state = tmp_path / ".hermes"
    load = mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-1", worktree=tmp_path, prompt_client="codex"
        ),
    )
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert flag_issue_drift(
            project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK",
            code="issue_drift", repo="acme/repo",
        ) is False

    assert load.call_args.kwargs["repo"] == "acme/repo"
    create.assert_not_called()
    assert "issue_drift" in caplog.text


def test_flag_issue_drift_without_cards_only_logs(tmp_path, mocker, caplog):
    from hermes_pipeline.todos_completion import flag_issue_drift

    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-1", worktree=tmp_path, prompt_client="codex"
        ),
    )
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")

    with caplog.at_level("ERROR"):
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


def test_finish_reports_a_broken_git_as_broken_not_as_a_head_mismatch(tmp_path, mocker):
    """A git exit code >= 2 is not an answer, and must not read as one.

    Collapsing it into ``finish_review_head_mismatch`` would tell the operator
    the worker delivered the wrong history when in fact git could not be asked.

    Asserted on ``exc.code``, not with ``pytest.raises(match=...)``:
    ``match`` is a ``re.search``, and the wrapped message
    ``"finish_review_head_mismatch: git_verification_failed"`` contains the
    string it looks for -- so a ``match`` on the inner code passes whether or
    not the pass-through re-raise exists, and pins nothing.
    """
    repo = _finish_repo(tmp_path, "broken-git")
    accepted = _finish_commit(repo, "reviewed.txt")
    head = _finish_commit(repo, "CHANGELOG.md")
    mocker.patch(
        "hermes_pipeline.result_contract.subprocess.run",
        return_value=SimpleNamespace(returncode=128, stdout="", stderr="fatal"),
    )

    with pytest.raises(ResultContractError) as exc_info:
        _verify_finish(
            repo, _finish_result(accepted, head, ("CHANGELOG.md",)), accepted,
            require_current=True,
        )
    assert exc_info.value.code == "git_verification_failed"


def test_a_failed_delivery_tick_does_not_grant_the_next_one_the_relaxation(
    tmp_path, mocker, caplog
):
    """``finish-verified`` is written last, after every delivery check passed.

    The marker's only job is to relax ``require_current`` on the next tick. It
    used to be written straight after ``_verify_finish``, before
    ``delivery_head_mismatch``, ``delivery_authority_drift`` and
    ``pr_identity_mismatch`` were checked -- so a tick that FAILED delivery
    still handed the weaker verification to its successor.
    """
    import hermes_pipeline.todos_completion as module

    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=_view())
    marker = state / "runs" / "01TICK" / "finish-verified"
    # A delivery check that fails strictly after ``_verify_finish``.
    module.parse_worker_result.return_value.delivery.head_sha = "b" * 40

    with caplog.at_level("ERROR", logger="hermes_pipeline.todos_completion"):
        assert _reconcile(tmp_path, state) is False
    assert "delivery_head_mismatch" in caplog.text
    assert not marker.exists()

    # The next tick therefore still runs the strict check.
    assert module._verify_finish.call_args.kwargs["require_current"] is True
    assert _reconcile(tmp_path, state) is False
    assert module._verify_finish.call_args.kwargs["require_current"] is True


def test_a_broken_git_in_the_worktree_check_does_not_blame_the_registration(
    tmp_path, mocker
):
    """The worktree-clean check goes through ``_git_bytes``.

    That helper raised ``registration_invalid``, which ``_verify_finish``'s
    pass-through guard does not cover, so a git that could not answer surfaced
    as ``finish_review_head_mismatch: registration_invalid`` -- blaming the
    worker for the history AND the registration for being corrupt, neither of
    which had happened.
    """
    repo = _finish_repo(tmp_path, "broken-status")
    accepted = _finish_commit(repo, "reviewed.txt")
    head = _finish_commit(repo, "CHANGELOG.md")
    real_run = subprocess.run

    def fail_status(cmd, *args, **kwargs):
        if "status" in cmd:
            return SimpleNamespace(returncode=128, stdout=b"", stderr=b"fatal")
        return real_run(cmd, *args, **kwargs)

    mocker.patch(
        "hermes_pipeline.result_contract.subprocess.run", side_effect=fail_status
    )

    with pytest.raises(ResultContractError) as exc_info:
        _verify_finish(
            repo, _finish_result(accepted, head, ("CHANGELOG.md",)), accepted,
            require_current=True,
        )
    assert exc_info.value.code == "git_verification_failed"
    assert "registration_invalid" not in str(exc_info.value)


def test_the_finish_card_writes_its_pending_marker_under_the_clone(tmp_path, mocker):
    """The clone's run directory is the only one that exists.

    ``_create_task`` used to default ``project_dir`` to the worktree, so the
    finish card's pending-create marker was written to
    ``<worktree>/.hermes/runs/<tick>/`` -- a directory a fresh
    ``git worktree add`` never has and gitignored ``.hermes`` never gains. The
    write raised ``FileNotFoundError`` and the finish card could not be created
    at all, which is how a live run died before reaching delivery. The review
    card already passed the clone; the finish card did not.
    """
    mocker.patch("hermes_pipeline._agent_supervisor.register_execution", return_value="registered-finish")
    mocker.patch("hermes_pipeline.agent_execution.ExecutionStore").return_value.load.return_value = {"registration": {"timeout": 2400, "manifest": None}}
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    state, run_dir = _run_dir(tmp_path)
    (run_dir / "registration.json").write_text("{}")
    (run_dir / "accepted-review-head").write_text("a" * 40)
    worktree = tmp_path / ".worktrees" / "todo-3-x"
    worktree.mkdir(parents=True)
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-3", worktree=worktree, branch="feat/native",
            assignee="worker", prompt_client="codex", profile="native-sdd",
            plan_hash="f" * 64, manifest=SimpleNamespace(tasks=()),
            plan_reference=SimpleNamespace(value="docs/plan.md"),
        ),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={"review:0": SimpleNamespace(task_id="review", status="done")},
    )
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    # The real ``_create_task`` runs, so the marker write is the real one.
    mocker.patch(
        "hermes_pipeline.review_reconciliation._find_task_id_in_snapshot",
        return_value=None,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout='{"id": "t_12345678"}'),
    )

    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK",
        repo="acme/repo",
    )

    # Written and then cleared, under the clone -- never under the worktree.
    assert not (worktree / ".hermes").exists()
    assert not (run_dir / "pending-review-create.json").exists()


def test_an_ambiguous_finish_create_stays_retryable_instead_of_raising(
    tmp_path, mocker
):
    """The finish create's ambiguous outcome is a retry, not a crashed tick.

    ``hermes kanban create`` timing out (or returning an id nothing can parse)
    leaves the card's existence unknown, which is exactly what
    ``RetryableReviewRegistration`` means: the next tick re-derives the truth
    from the snapshot. ``reconcile_reviews`` already handles it that way for
    ``review:0``. Letting it propagate out of ``reconcile_todo_completion``
    instead makes the whole tick raise -- and now that ``tpo tick`` reports a
    non-zero rc, that raise fails the entire harness run as ``tick_crashed``
    rather than costing one tick.
    """
    mocker.patch("hermes_pipeline._agent_supervisor.register_execution", return_value="registered-finish")
    mocker.patch("hermes_pipeline.agent_execution.ExecutionStore").return_value.load.return_value = {"registration": {"timeout": 2400, "manifest": None}}
    from hermes_pipeline.todos_completion import reconcile_todo_completion

    state, run_dir = _run_dir(tmp_path)
    (run_dir / "registration.json").write_text("{}")
    (run_dir / "accepted-review-head").write_text("a" * 40)
    worktree = tmp_path / ".worktrees" / "todo-3-x"
    worktree.mkdir(parents=True)
    mocker.patch(
        "hermes_pipeline.todos_completion.load_validated_registration",
        return_value=SimpleNamespace(
            todo_id="TODO-3", worktree=worktree, branch="feat/native",
            assignee="worker", prompt_client="codex", profile="native-sdd",
            plan_hash="f" * 64, manifest=SimpleNamespace(tasks=()),
            plan_reference=SimpleNamespace(value="docs/plan.md"),
        ),
    )
    mocker.patch(
        "hermes_pipeline.todos_completion.get_todo_kanban_tasks",
        return_value={"review:0": SimpleNamespace(task_id="review", status="done")},
    )
    mocker.patch(
        "hermes_pipeline.todos_completion._github_identity",
        return_value=("acme/repo", "main"),
    )
    # The real ``_create_task`` runs: no card in the snapshot, and the create
    # itself never reports an outcome.
    mocker.patch(
        "hermes_pipeline.review_reconciliation._find_task_id_in_snapshot",
        return_value=None,
    )
    mocker.patch(
        "hermes_pipeline.review_reconciliation.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=1),
    )

    # Same semantics as ``reconcile_reviews``: return "progress" so the tick
    # ends, and leave the pending marker where it was written.
    assert reconcile_todo_completion(
        project_dir=tmp_path, state_dir=state, tenant="demo", tick_id="01TICK",
        repo="acme/repo",
    )
    assert (run_dir / "pending-review-create.json").exists()


def test_create_task_has_no_worktree_fallback_for_project_dir():
    """The fallback is the bug; a missing ``project_dir`` must be a TypeError."""
    import inspect

    from hermes_pipeline.review_reconciliation import _create_task

    parameter = inspect.signature(_create_task).parameters["project_dir"]
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("failure", [False, ResultContractError("registration_invalid"), RuntimeError("provider secret")])
def test_pending_delivery_sweep_isolates_old_failures_and_skips_terminal_runs(
    tmp_path, mocker, caplog, failure
):
    from hermes_pipeline import todos_completion as module

    state = tmp_path / "state"
    for tick in ("01BAD", "02READY", "03DELIVERED", "04ABANDONED", "05UNVERIFIED", "CURRENT"):
        run = state / "runs" / tick
        run.mkdir(parents=True)
        (run / "registration.json").write_text(json.dumps({"schema_version": 3, "issue_number": 3}))
        if tick != "05UNVERIFIED":
            (run / "finish-verified").write_text("a" * 40)
        if tick == "03DELIVERED":
            (run / "issue-closed").touch()
        if tick == "04ABANDONED":
            (run / "abandoned").touch()
    mocker.patch.object(module.github_issues, "repository_identity", return_value="acme/repo")
    reconcile = mocker.patch.object(module, "reconcile_todo_completion", side_effect=[failure, True])
    module.reconcile_pending_deliveries(
        project_dir=tmp_path, state_dir=state, tenant="demo", current_tick_id="CURRENT",
    )
    assert [call.kwargs["tick_id"] for call in reconcile.call_args_list] == ["01BAD", "02READY"]
    assert "provider secret" not in caplog.text


@pytest.mark.parametrize("drift", ["issue_drift", "issue_on_hold", "issue_not_planned", "issue_identity_mismatch", "issue_unavailable:gh_auth"])
def test_merged_delivery_rechecks_live_issue_drift_before_close(tmp_path, mocker, drift):
    state = _finish_done_fixture(tmp_path, mocker, tasks=_finish_tasks(), view=_view("MERGED"))
    mocker.patch("hermes_pipeline.todos_completion._check_state", return_value="passed")
    check = mocker.patch("hermes_pipeline.todos_completion.github_issues.check_issue_drift", return_value=drift)
    close = mocker.patch("hermes_pipeline.todos_completion.close_issue_for_delivery")
    assert _reconcile(tmp_path, state) is False
    assert check.call_args.kwargs["allow_closed"] is True
    close.assert_not_called()


def test_verified_handoff_with_missing_finish_card_never_recreates_work(tmp_path, mocker):
    state = _finish_done_fixture(tmp_path, mocker, tasks={}, view=_view("MERGED"))
    (state / "runs" / "01TICK" / "finish-verified").write_text("a" * 40)
    mocker.patch("hermes_pipeline.todos_completion.profile_phase", return_value=(
        tmp_path, SimpleNamespace(name="Finish", tools="", turns=1, timeout=1),
    ))
    mocker.patch("hermes_pipeline.todos_completion.render_profile_prompt", return_value="prompt")
    mocker.patch("hermes_pipeline.todos_completion.render_result_template", return_value="template")
    create = mocker.patch("hermes_pipeline.todos_completion._create_task")
    assert _reconcile(tmp_path, state) is False
    create.assert_not_called()
