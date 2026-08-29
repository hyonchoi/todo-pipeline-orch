"""Tests for the gh-CLI subprocess layer in hermes_pipeline.github_issues."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from hermes_pipeline import github_issues as gi
from hermes_pipeline.github_issues import (
    GH_BIN_ENV,
    MAX_ISSUE_BODY_CHARS,
    GitHubIssuesError,
    add_blocked_by,
    add_comment,
    add_label,
    check_auth,
    close_issue,
    create_issue,
    ensure_labels,
    fetch_issue,
    find_issues_by_label,
    gh_bin,
    list_comment_bodies,
    list_labels,
    list_todo_issues,
    parse_github_remote,
    remove_label,
    repository_identity,
)
from tests.gh_fakes import issue_payload

REPO = "acme/repo"
TOKEN = "ghp_" + "A" * 36
ORIGIN = ("git", "remote", "get-url", "origin")
ACCEPT = ["-H", "Accept: application/vnd.github+json"]
API = ("gh", "api", *ACCEPT)


def _pages(*pages) -> str:
    return json.dumps([list(page) for page in pages])


def test_gh_bin_honours_environment_override(monkeypatch):
    monkeypatch.delenv(GH_BIN_ENV, raising=False)
    assert gh_bin() == "gh"
    monkeypatch.setenv(GH_BIN_ENV, "/opt/gh")
    assert gh_bin() == "/opt/gh"


def test_gh_bin_is_used_as_argv0(fake_gh, tmp_path, monkeypatch):
    monkeypatch.setenv(GH_BIN_ENV, "/opt/gh")
    fake_gh.on("/opt/gh", "auth", "status")
    check_auth(tmp_path)
    assert fake_gh.calls == [["/opt/gh", "auth", "status", "--hostname", "github.com"]]


def test_fake_gh_does_not_patch_subprocess_run_globally(fake_gh):
    assert subprocess.run is not fake_gh
    assert gi._run is fake_gh


def test_gh_subprocess_kwargs_contract(fake_gh, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_HOST", "evil.example")
    fake_gh.on("gh", "auth", "status")
    check_auth(tmp_path)
    kwargs = fake_gh.kwargs[0]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["check"] is False and kwargs["timeout"] == 60.0
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["env"]["GH_HOST"] == "github.com"
    assert "shell" not in kwargs and "input" not in kwargs


def test_list_calls_use_long_timeout(fake_gh, tmp_path):
    fake_gh.on(*API, stdout="[]")
    list_todo_issues(tmp_path, repo=REPO)
    list_comment_bodies(tmp_path, 7, repo=REPO)
    find_issues_by_label(tmp_path, "tpo:todo", repo=REPO)
    assert [k["timeout"] for k in fake_gh.kwargs] == [180, 180, 180]


@pytest.mark.parametrize(
    ("raises", "rc", "stderr", "code"),
    [
        (FileNotFoundError("gh"), None, "", "gh_missing"),
        (subprocess.TimeoutExpired("gh", 60), None, "", "gh_unavailable"),
        (None, 4, f"To get started with GitHub CLI, please run: gh auth login {TOKEN}", "gh_auth"),
        (None, 1, "HTTP 401: Bad credentials", "gh_auth"),
        (None, 1, "error: not logged in", "gh_auth"),
        (None, 1, "gh: API rate limit exceeded for user ID 1. (HTTP 403)", "gh_rate_limited"),
        (None, 1, "HTTP 429: too many requests", "gh_rate_limited"),
        (None, 1, "You have exceeded a secondary rate limit", "gh_rate_limited"),
        (None, 1, "HTTP 404: Not Found", "gh_not_found"),
        (None, 1, "Could not resolve to a Repository", "gh_not_found"),
        (None, 1, "gh: Validation Failed (HTTP 422)", "gh_rejected"),
        (None, 1, "HTTP 409: Conflict", "gh_rejected"),
        (None, 1, "HTTP 400: Bad Request", "gh_rejected"),
        (None, 1, "could not add label: 'x' not found", "gh_rejected"),
        (None, 1, "HTTP 403: Resource not accessible by integration", "gh_unavailable"),
        (None, 2, f"something exploded {TOKEN}", "gh_unavailable"),
    ],
)
def test_gh_errors_are_classified_without_leaking_stderr(
    fake_gh, tmp_path, caplog, raises, rc, stderr, code
):
    fake_gh.on(*API, rc=rc or 0, stderr=stderr, raises=raises)
    with caplog.at_level(logging.WARNING, logger="hermes_pipeline.github_issues"):
        with pytest.raises(GitHubIssuesError) as info:
            fetch_issue(tmp_path, 7, repo=REPO)
    exc = info.value
    assert exc.code == code
    assert exc.verb == "api"
    assert str(exc) == f"{code}: gh api"
    assert TOKEN not in str(exc)
    assert "exploded" not in str(exc)
    assert caplog.records, "failures are logged at WARNING"
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert all("exploded" not in record.getMessage() for record in caplog.records)


def test_timeout_keeps_partial_stdout_and_suppresses_cause(fake_gh, tmp_path):
    exc = subprocess.TimeoutExpired("gh", 60, output=b"partial \xff")
    fake_gh.on(*API, raises=exc)
    with pytest.raises(GitHubIssuesError) as info:
        fetch_issue(tmp_path, 7, repo=REPO)
    assert info.value.code == "gh_unavailable"
    assert info.value.partial_stdout == "partial �"
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_gh_invalid_json_and_shape(fake_gh, tmp_path):
    fake_gh.on(*API, stdout="{not json")
    with pytest.raises(GitHubIssuesError, match="gh_invalid: gh api"):
        fetch_issue(tmp_path, 7, repo=REPO)
    fake_gh.on(*API, stdout="[1, 2]")
    with pytest.raises(GitHubIssuesError, match="gh_invalid"):
        fetch_issue(tmp_path, 7, repo=REPO)


@pytest.mark.parametrize(
    "payload",
    [
        issue_payload(7, labels=()) | {"labels": "abc"},
        issue_payload(7, labels=()) | {"labels": {"name": "x"}},
        issue_payload(7) | {"number": True},
        issue_payload(7) | {"title": ["a"]},
        issue_payload(7, blocked_by=None) | {"issue_dependencies_summary": {"blocked_by": True}},
        issue_payload(7, blocked_by=None) | {"issue_dependencies_summary": {"blocked_by": "3"}},
    ],
)
def test_fetch_issue_rejects_malformed_payload_fields(fake_gh, tmp_path, payload):
    fake_gh.on(*API, stdout=json.dumps(payload))
    with pytest.raises(GitHubIssuesError, match="gh_invalid"):
        fetch_issue(tmp_path, 7, repo=REPO)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/repo", "acme/repo"),
        ("https://github.com/acme/repo.git\n", "acme/repo"),
        ("https://github.com/acme/repo/", "acme/repo"),
        ("https://user@github.com/acme/repo.git", "acme/repo"),
        ("git@github.com:acme/repo.git", "acme/repo"),
        ("ssh://git@github.com/acme/repo", "acme/repo"),
        ("https://gitlab.com/acme/repo.git", None),
        ("https://notgithub.com/acme/repo", None),
        ("https://evil.example/x?y=github.com/acme/repo", None),
        ("https://github.com/acme/repo?foo", None),
        ("https://github.com/../..", None),
        ("https://github.com/./repo", None),
        ("https://github.com/o/.git", None),
        ("https://github.com/o/r..", None),
        ("https://github.com/acme/re po", None),
        ("https://github.com/acme/repo\nevil", None),
        ("https://github.com/acme/repo&x", None),
        ("", None),
    ],
)
def test_parse_github_remote(url, expected):
    assert parse_github_remote(url) == expected


@pytest.mark.parametrize(
    "repo", ["../..", "acme/re po", "acme", "acme/repo\n", "a/b/c", "./x", "o/.git", "o/r..x"]
)
def test_caller_supplied_repo_is_validated(fake_gh, tmp_path, repo):
    with pytest.raises(GitHubIssuesError) as info:
        fetch_issue(tmp_path, 7, repo=repo)
    assert info.value.code == "origin_identity_invalid"
    assert fake_gh.calls == []


def test_repository_identity_reads_origin(fake_gh, tmp_path):
    fake_gh.on(*ORIGIN, stdout="git@github.com:acme/repo.git\n")
    assert repository_identity(tmp_path) == "acme/repo"
    assert fake_gh.calls == [list(ORIGIN)]
    assert fake_gh.kwargs[0]["cwd"] == tmp_path


@pytest.mark.parametrize(
    "rule",
    [
        {"stdout": "https://gitlab.com/acme/repo.git\n"},
        {"rc": 128, "stderr": "fatal: no such remote"},
        {"raises": FileNotFoundError("git")},
    ],
)
def test_repository_identity_rejects_bad_origin(fake_gh, tmp_path, rule):
    fake_gh.on(*ORIGIN, **rule)
    with pytest.raises(GitHubIssuesError, match="origin_identity_invalid: gh git remote"):
        repository_identity(tmp_path)


def test_repo_none_resolves_identity_from_origin(fake_gh, tmp_path):
    fake_gh.on(*ORIGIN, stdout="https://github.com/acme/repo\n")
    fake_gh.on(*API, stdout=json.dumps(issue_payload(3)))
    issue = fetch_issue(tmp_path, 3)
    assert issue.repo == REPO
    assert fake_gh.calls[0] == list(ORIGIN)
    assert fake_gh.calls[1][1:] == ["api", *ACCEPT, "repos/acme/repo/issues/3"]


@pytest.mark.parametrize("number", [0, -1, True, "7"])
def test_issue_numbers_must_be_positive_ints(fake_gh, tmp_path, number):
    with pytest.raises(ValueError):
        fetch_issue(tmp_path, number, repo=REPO)
    with pytest.raises(ValueError):
        add_blocked_by(tmp_path, 7, number, repo=REPO)
    assert fake_gh.calls == []


def test_list_todo_issues_flattens_pages_skips_prs_and_sorts(fake_gh, tmp_path):
    fake_gh.on(
        *API,
        stdout=_pages(
            [issue_payload(9), issue_payload(4, pull_request=True)],
            [issue_payload(2), issue_payload(5)],
        ),
    )
    issues = list_todo_issues(tmp_path, repo=REPO)
    assert [issue.number for issue in issues] == [2, 5, 9]
    assert all(issue.repo == REPO and issue.todo_id == f"TODO-{issue.number}" for issue in issues)
    assert fake_gh.gh_calls() == [
        [
            "api",
            *ACCEPT,
            "--paginate",
            "--slurp",
            "repos/acme/repo/issues?state=open&labels=tpo%3Atodo&per_page=100",
        ]
    ]


def test_list_todo_issues_empty_and_invalid(fake_gh, tmp_path):
    fake_gh.on(*API, stdout="")
    assert list_todo_issues(tmp_path, repo=REPO) == ()
    fake_gh.on(*API, stdout='{"number": 1}')
    with pytest.raises(GitHubIssuesError, match="gh_invalid"):
        list_todo_issues(tmp_path, repo=REPO)
    fake_gh.on(*API, stdout='[[{"title": "no number"}]]')
    with pytest.raises(GitHubIssuesError, match="gh_invalid"):
        list_todo_issues(tmp_path, repo=REPO)


def test_find_issues_by_label_quotes_label_and_skips_prs(fake_gh, tmp_path):
    fake_gh.on(*API, stdout=_pages([issue_payload(8, state="closed"), issue_payload(1, pull_request=True)]))
    issues = find_issues_by_label(tmp_path, "legacy-id:TODO-12", repo=REPO)
    assert [issue.number for issue in issues] == [8]
    assert fake_gh.gh_calls() == [
        ["api", *ACCEPT, "--paginate", "--slurp",
         "repos/acme/repo/issues?state=all&labels=legacy-id%3ATODO-12&per_page=100"]
    ]
    fake_gh.on(*API, stdout="")
    assert find_issues_by_label(tmp_path, "x", state="open", repo=REPO) == ()
    assert fake_gh.gh_calls()[-1][-1].startswith("repos/acme/repo/issues?state=open&")


def test_find_issues_by_label_rejects_unknown_state(fake_gh, tmp_path):
    with pytest.raises(ValueError):
        find_issues_by_label(tmp_path, "tpo:todo", state="open&labels=admin", repo=REPO)
    assert fake_gh.calls == []


def test_fetch_issue_rejects_pull_requests(fake_gh, tmp_path):
    fake_gh.on(*API, stdout=json.dumps(issue_payload(7, pull_request=True)))
    with pytest.raises(GitHubIssuesError) as info:
        fetch_issue(tmp_path, 7, repo=REPO)
    assert info.value.code == "not_an_issue"
    assert isinstance(info.value.__cause__, gi.NotAnIssueError)


def test_list_comment_bodies_flattens_pages(fake_gh, tmp_path):
    fake_gh.on(*API, stdout=_pages([{"body": "one"}, {"body": None}], [{"body": "three"}]))
    assert list_comment_bodies(tmp_path, 7, repo=REPO) == ("one", "", "three")
    assert fake_gh.gh_calls() == [
        ["api", *ACCEPT, "--paginate", "--slurp", "repos/acme/repo/issues/7/comments"]
    ]
    fake_gh.on(*API, stdout="")
    assert list_comment_bodies(tmp_path, 7, repo=REPO) == ()


def test_check_auth_maps_unknown_failure_to_gh_auth_and_logs_once(fake_gh, tmp_path, caplog):
    fake_gh.on("gh", "auth", "status", rc=1, stderr="You are not logged into any GitHub hosts")
    with caplog.at_level(logging.WARNING, logger="hermes_pipeline.github_issues"):
        with pytest.raises(GitHubIssuesError) as info:
            check_auth(tmp_path)
    assert (info.value.code, info.value.verb) == ("gh_auth", "auth status")
    assert len(caplog.records) == 1
    fake_gh.on("gh", "auth", "status", rc=1, stderr="unrelated failure")
    with pytest.raises(GitHubIssuesError, match="gh_auth"):
        check_auth(tmp_path)


@pytest.mark.parametrize(
    ("rule", "code"),
    [
        ({"raises": FileNotFoundError("gh")}, "gh_missing"),
        ({"raises": subprocess.TimeoutExpired("gh", 60)}, "gh_unavailable"),
        ({"rc": 1, "stderr": "gh: API rate limit exceeded for user ID 1. (HTTP 403)"}, "gh_rate_limited"),
    ],
)
def test_check_auth_passes_through_infrastructure_errors(fake_gh, tmp_path, rule, code):
    fake_gh.on("gh", "auth", "status", **rule)
    with pytest.raises(GitHubIssuesError) as info:
        check_auth(tmp_path)
    assert info.value.code == code


def test_list_labels(fake_gh, tmp_path):
    fake_gh.on("gh", "label", "list", stdout='[{"name": "bug"}, {"name": "tpo:todo"}]')
    assert list_labels(tmp_path, repo=REPO) == frozenset({"bug", "tpo:todo"})
    assert fake_gh.gh_calls() == [
        ["label", "list", "--repo", "acme/repo", "--json", "name", "--limit", "1000"]
    ]
    fake_gh.on("gh", "label", "list", stdout='{"name": "bug"}')
    with pytest.raises(GitHubIssuesError, match="gh_invalid"):
        list_labels(tmp_path, repo=REPO)


def test_list_labels_rejects_truncated_listing(fake_gh, tmp_path):
    fake_gh.on("gh", "label", "list", stdout=_label_list_stdout(f"l{i}" for i in range(1000)))
    with pytest.raises(GitHubIssuesError, match="gh_invalid: gh label list"):
        list_labels(tmp_path, repo=REPO)


def test_add_and_remove_label_argv(fake_gh, tmp_path):
    fake_gh.on("gh", "issue", "edit")
    add_label(tmp_path, 7, "tpo:in-progress", repo=REPO)
    remove_label(tmp_path, 7, "ready-for-agent", repo=REPO)
    assert fake_gh.gh_calls() == [
        ["issue", "edit", "7", "--repo", "acme/repo", "--add-label", "tpo:in-progress"],
        ["issue", "edit", "7", "--repo", "acme/repo", "--remove-label", "ready-for-agent"],
    ]


def test_add_label_failure_surfaces_code(fake_gh, tmp_path):
    fake_gh.on("gh", "issue", "edit", rc=1, stderr="HTTP 404: Not Found")
    with pytest.raises(GitHubIssuesError) as info:
        add_label(tmp_path, 7, "x", repo=REPO)
    assert (info.value.code, info.value.verb) == ("gh_not_found", "issue edit")


def _capture_body_file(seen: dict):
    def handler(argv):
        path = Path(argv[argv.index("--body-file") + 1])
        seen["exists"] = path.exists()
        seen["content"] = path.read_text(encoding="utf-8")
        seen["path"] = path
        return 0, "", ""

    return handler


def test_add_comment_passes_body_via_tempfile(fake_gh, tmp_path):
    seen: dict = {}
    fake_gh.on("gh", "issue", "comment", handler=_capture_body_file(seen))
    body = "first line\n\n```json\n{\"a\": 1}\n```\n"
    add_comment(tmp_path, 7, body, repo=REPO)
    assert seen["exists"] and seen["content"] == body
    assert not seen["path"].exists(), "tempfile removed after the call"
    argv = fake_gh.gh_calls()[0]
    assert argv[:5] == ["issue", "comment", "7", "--repo", "acme/repo"]
    assert argv[5] == "--body-file" and len(argv) == 7


def test_oversized_bodies_are_rejected_before_any_call(fake_gh, tmp_path):
    body = "x" * (MAX_ISSUE_BODY_CHARS + 1)
    with pytest.raises(ValueError):
        add_comment(tmp_path, 7, body, repo=REPO)
    with pytest.raises(ValueError):
        create_issue(tmp_path, title="T", body=body, labels=[], repo=REPO)
    assert fake_gh.calls == []


def test_close_issue_argv_and_idempotency(fake_gh, tmp_path):
    fake_gh.on("gh", "issue", "close")
    close_issue(tmp_path, 7, repo=REPO)
    assert fake_gh.gh_calls() == [
        ["issue", "close", "7", "--repo", "acme/repo", "--reason", "completed"]
    ]
    fake_gh.on("gh", "issue", "close", rc=0, stderr="! Issue acme/repo#7 (t) is already closed\n")
    close_issue(tmp_path, 7, repo=REPO)
    fake_gh.on("gh", "issue", "close", rc=1, stderr="HTTP 401: Bad credentials")
    with pytest.raises(GitHubIssuesError, match="gh_auth: gh issue close"):
        close_issue(tmp_path, 7, repo=REPO)


def _label_list_stdout(names) -> str:
    return json.dumps([{"name": name} for name in names])


def test_ensure_labels_zero_writes_when_all_present_case_insensitively(fake_gh, tmp_path):
    names = [n.upper() if i % 2 else n for i, (n, _, _) in enumerate(gi.LABEL_VOCABULARY)]
    fake_gh.on("gh", "label", "list", stdout=_label_list_stdout(names))
    assert ensure_labels(tmp_path, repo=REPO) == ()
    assert [c[:2] for c in fake_gh.gh_calls()] == [["label", "list"]]


def test_ensure_labels_creates_only_missing_including_extra(fake_gh, tmp_path):
    present = [n for n, _, _ in gi.LABEL_VOCABULARY if n not in {"tpo:on-hold", "effort:L"}]
    fake_gh.on("gh", "label", "list", stdout=_label_list_stdout(present))
    fake_gh.on("gh", "label", "create")
    created = ensure_labels(
        tmp_path, repo=REPO, extra=[("phase:4-development", "abcdef", "Phase 4")]
    )
    assert created == ("effort:L", "phase:4-development", "tpo:on-hold")
    creates = [c for c in fake_gh.gh_calls() if c[:2] == ["label", "create"]]
    assert creates == [
        ["label", "create", "--repo", "acme/repo", "--color", "e4e669",
         "--description", "TODO is paused and must not be selected", "--force", "--", "tpo:on-hold"],
        ["label", "create", "--repo", "acme/repo", "--color", "9ecbff",
         "--description", "Effort: large", "--force", "--", "effort:L"],
        ["label", "create", "--repo", "acme/repo", "--color", "abcdef",
         "--description", "Phase 4", "--force", "--", "phase:4-development"],
    ]


def test_ensure_labels_treats_rejected_create_as_present(fake_gh, tmp_path):
    present = [n for n, _, _ in gi.LABEL_VOCABULARY if n not in {"tpo:on-hold", "effort:L"}]
    fake_gh.on("gh", "label", "list", stdout=_label_list_stdout(present))
    fake_gh.on("gh", "label", "create")
    fake_gh.on("gh", "label", "create", handler=lambda argv: (
        (1, "", "Validation Failed: already_exists (HTTP 422)")
        if argv[-1] == "tpo:on-hold" else (0, "", "")
    ))
    assert ensure_labels(tmp_path, repo=REPO) == ("effort:L",)


def test_ensure_labels_attaches_partial_progress_on_hard_failure(fake_gh, tmp_path):
    present = [n for n, _, _ in gi.LABEL_VOCABULARY if n not in {"tpo:on-hold", "effort:L"}]
    fake_gh.on("gh", "label", "list", stdout=_label_list_stdout(present))
    fake_gh.on("gh", "label", "create")
    fake_gh.on("gh", "label", "create", handler=lambda argv: (
        (1, "", "HTTP 401") if argv[-1] == "effort:L" else (0, "", "")
    ))
    with pytest.raises(GitHubIssuesError) as info:
        ensure_labels(tmp_path, repo=REPO)
    assert info.value.code == "gh_auth"
    assert info.value.created == ("tpo:on-hold",)


def test_create_issue_argv_body_file_and_url_parsing(fake_gh, tmp_path):
    seen: dict = {}
    inner = _capture_body_file(seen)

    def handler(argv):
        inner(argv)
        return 0, "https://github.com/Acme/Repo/issues/42\n", ""

    fake_gh.on("gh", "issue", "create", handler=handler)
    number = create_issue(
        tmp_path, title="T", body="### What\n\nx\n", labels=["tpo:todo", "priority:P1"], repo=REPO
    )
    assert number == 42
    assert seen["content"] == "### What\n\nx\n" and not seen["path"].exists()
    argv = fake_gh.gh_calls()[0]
    assert argv[:5] == ["issue", "create", "--repo", "acme/repo", "--title"]
    assert argv[5] == "T" and argv[6] == "--body-file"
    assert argv[8:] == ["--label", "tpo:todo", "--label", "priority:P1"]


def test_create_issue_omits_label_flag_when_empty(fake_gh, tmp_path):
    fake_gh.on("gh", "issue", "create", stdout="https://github.com/acme/repo/issues/1\n")
    assert create_issue(tmp_path, title="T", body="b", labels=[], repo=REPO) == 1
    assert "--label" not in fake_gh.gh_calls()[0]


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "garbage",
        "https://github.com/acme/repo/pull/3\n",
        "https://github.com/other/repo/issues/3\n",
        "https://evil.example/acme/repo/issues/3\n",
    ],
)
def test_create_issue_rejects_unparseable_output(fake_gh, tmp_path, stdout):
    fake_gh.on("gh", "issue", "create", stdout=stdout)
    with pytest.raises(GitHubIssuesError, match="gh_invalid: gh issue create"):
        create_issue(tmp_path, title="T", body="b", labels=["tpo:todo"], repo=REPO)


def test_create_issue_recovers_number_from_timeout_partial_stdout(fake_gh, tmp_path):
    exc = subprocess.TimeoutExpired("gh", 60, output=b"https://github.com/acme/repo/issues/9\n")
    fake_gh.on("gh", "issue", "create", raises=exc)
    assert create_issue(tmp_path, title="T", body="b", labels=[], repo=REPO) == 9
    fake_gh.on("gh", "issue", "create", raises=subprocess.TimeoutExpired("gh", 60))
    with pytest.raises(GitHubIssuesError, match="gh_unavailable"):
        create_issue(tmp_path, title="T", body="b", labels=[], repo=REPO)


def test_add_blocked_by_two_step_argv(fake_gh, tmp_path):
    fake_gh.on(*API, "repos/acme/repo/issues/3", stdout=json.dumps(issue_payload(3) | {"id": 123456}))
    fake_gh.on(*API, "--method", "POST")
    add_blocked_by(tmp_path, 7, 3, repo=REPO)
    assert fake_gh.gh_calls() == [
        ["api", *ACCEPT, "repos/acme/repo/issues/3"],
        ["api", *ACCEPT, "--method", "POST", "repos/acme/repo/issues/7/dependencies/blocked_by",
         "-F", "issue_id=123456"],
    ]


@pytest.mark.parametrize("payload", [{"id": "123"}, {"id": True}, {}])
def test_add_blocked_by_rejects_bad_ids(fake_gh, tmp_path, payload):
    fake_gh.on(*API, "repos/acme/repo/issues/3", stdout=json.dumps(issue_payload(3) | payload))
    with pytest.raises(GitHubIssuesError, match="gh_invalid"):
        add_blocked_by(tmp_path, 7, 3, repo=REPO)
    assert len(fake_gh.calls) == 1


def test_add_blocked_by_rejects_pull_request_blocker(fake_gh, tmp_path):
    fake_gh.on(*API, "repos/acme/repo/issues/3",
               stdout=json.dumps(issue_payload(3, pull_request=True) | {"id": 1}))
    with pytest.raises(GitHubIssuesError, match="not_an_issue"):
        add_blocked_by(tmp_path, 7, 3, repo=REPO)


def test_add_blocked_by_treats_rejected_post_as_existing_edge(fake_gh, tmp_path):
    fake_gh.on(*API, "repos/acme/repo/issues/3", stdout=json.dumps(issue_payload(3) | {"id": 5}))
    fake_gh.on(*API, "--method", "POST", rc=1, stderr="gh: Validation Failed (HTTP 422)")
    add_blocked_by(tmp_path, 7, 3, repo=REPO)
    fake_gh.on(*API, "--method", "POST", rc=1, stderr="HTTP 404: Not Found")
    with pytest.raises(GitHubIssuesError, match="gh_not_found"):
        add_blocked_by(tmp_path, 7, 3, repo=REPO)
