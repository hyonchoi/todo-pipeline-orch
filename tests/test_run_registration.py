from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_pipeline.github_issues import (
    IN_PROGRESS_LABEL,
    MAX_ISSUE_SNAPSHOT_CHARS,
    snapshot_hash,
)
from hermes_pipeline.result_contract import load_validated_registration
from hermes_pipeline.run_registration import (
    RunRegistrationError,
    active_registration_issue_numbers,
    ensure_in_progress_label,
    register_pinned_run,
    registration_state,
)
from tests.gh_fakes import API_ARGV, issue_payload, make_issue

REPO = "acme/repo"
BODY = (
    "### What\n\nShip it.\n\n### Plan\n\ndocs/plan.md\n\n"
    "### Branch\n\nfeat/todo-42\n"
)


def _issue(number: int = 42, *, body: str = BODY, repo: str = REPO, **extra):
    return make_issue(number, repo=repo, title="Ship the feature", body=body, **extra)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text("# Plan\n")
    _git(repo, "add", "docs/plan.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    return repo, _git(repo, "rev-parse", "HEAD")


def _register(
    project: Path,
    *,
    tick_id: str = "01TICK",
    step_keys=("task-1", "gate-1"),
    plan_path: str = "docs/plan.md",
    issue=None,
    **kwargs,
):
    return register_pinned_run(
        project_dir=project,
        state_dir=project / ".hermes",
        tick_id=tick_id,
        selected_issue=issue if issue is not None else _issue(),
        plan_path=plan_path,
        profile="native-sdd",
        prompt_client="codex",
        assignee="implementer",
        review_assignee="reviewer",
        step_keys=step_keys,
        **kwargs,
    )


def test_registers_hashes_and_creates_linked_worktree(tmp_path):
    repo, base_sha = _repo(tmp_path)

    registration = _register(repo)

    expected_path = repo / ".worktrees" / "todo-42-ship-the-feature"
    assert registration.base_sha == base_sha
    assert registration.worktree == expected_path.resolve()
    assert registration.branch == "feat/todo-42"
    assert _git(expected_path, "rev-parse", "HEAD") == base_sha
    assert _git(expected_path, "branch", "--show-current") == "feat/todo-42"
    payload = json.loads(
        (repo / ".hermes" / "runs" / "01TICK" / "registration.json").read_text()
    )
    issue = _issue()
    assert payload == {
        "assignee": "implementer",
        "base_sha": base_sha,
        "branch": "feat/todo-42",
        "issue_number": 42,
        "issue_snapshot": issue.snapshot,
        "issue_url": "https://github.com/acme/repo/issues/42",
        "plan_hash": hashlib.sha256(b"# Plan\n").hexdigest(),
        "plan_path": "docs/plan.md",
        "profile": "native-sdd",
        "prompt_client": "codex",
        "repository": str(repo.resolve()),
        "review_assignee": "reviewer",
        "schema_version": 2,
        "selected_entry_hash": snapshot_hash(issue.snapshot),
        "step_keys": ["task-1", "gate-1"],
        "tick_id": "01TICK",
        "todo_id": "TODO-42",
        "worktree": str(expected_path.resolve()),
    }


def test_registration_does_not_read_todos_md(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "TODOS.md").write_text("- [ ] **TODO-42: Something else**\n")

    registration = _register(repo)

    assert registration.issue_number == 42
    assert registration.todo_id == "TODO-42"


def test_explicit_repo_kwarg_bypasses_origin_lookup(tmp_path):
    repo, _ = _repo(tmp_path)
    _git(repo, "remote", "remove", "origin")

    with pytest.raises(RunRegistrationError, match="origin_identity_invalid"):
        _register(repo)
    registration = _register(repo, repo=REPO)
    assert registration.issue_url == "https://github.com/acme/repo/issues/42"


def test_rejects_issue_from_another_repository(tmp_path):
    repo, _ = _repo(tmp_path)

    with pytest.raises(RunRegistrationError, match="authority_repo_mismatch"):
        _register(repo, issue=_issue(repo="other/repo"))

    assert not (repo / ".hermes" / "runs" / "01TICK").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda issue: replace(issue, snapshot="x" * (MAX_ISSUE_SNAPSHOT_CHARS + 1)),
            id="oversized-snapshot",
        ),
        pytest.param(lambda issue: replace(issue, todo_id="TODO-7"), id="todo-id-mismatch"),
        pytest.param(
            lambda issue: replace(issue, url="https://github.com/acme/repo/issues/43"),
            id="url-number-mismatch",
        ),
        pytest.param(
            lambda issue: replace(issue, url="https://github.com/other/repo/issues/42"),
            id="url-repo-mismatch",
        ),
        pytest.param(
            lambda issue: replace(issue, url="http://github.com/acme/repo/issues/42"),
            id="url-scheme",
        ),
    ],
)
def test_rejects_inconsistent_issue_authority(tmp_path, mutate):
    repo, _ = _repo(tmp_path)

    with pytest.raises(RunRegistrationError, match="authority_invalid"):
        _register(repo, issue=mutate(_issue()))

    assert not (repo / ".hermes" / "runs" / "01TICK").exists()


def test_issue_url_repo_comparison_is_case_insensitive(tmp_path):
    repo, _ = _repo(tmp_path)
    issue = replace(_issue(), url="https://github.com/Acme/Repo/issues/42")

    assert _register(repo, issue=issue).issue_url == issue.url


def test_exact_retry_reuses_registration_and_worktree(tmp_path):
    repo, _ = _repo(tmp_path)
    first = _register(repo)

    second = _register(repo)

    assert second == first


def test_retry_finishes_partial_registration(tmp_path):
    repo, _ = _repo(tmp_path)
    first = _register(repo)
    subprocess.run(
        ["git", "worktree", "remove", str(first.worktree)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    retried = _register(repo)

    assert retried == first
    assert retried.worktree.is_dir()


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda repo: (repo / "docs" / "plan.md").write_text("changed\n"), "authority_drift"),
        (lambda repo: _git(repo, "rm", "--cached", "docs/plan.md"), "authority_drift"),
    ],
)
def test_rejects_untracked_or_drifted_authority(tmp_path, mutation, code):
    repo, _ = _repo(tmp_path)
    mutation(repo)

    with pytest.raises(RunRegistrationError, match=code):
        _register(repo)

    assert not (repo / ".hermes" / "runs" / "01TICK").exists()


def test_rejects_plan_not_tracked_at_base(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "docs" / "draft.md").write_text("draft\n")
    issue = _issue(body=BODY.replace("docs/plan.md", "docs/draft.md"))

    with pytest.raises(RunRegistrationError, match="authority_untracked"):
        _register(repo, plan_path="docs/draft.md", issue=issue)


@pytest.mark.parametrize("branch", ["", "bad branch", "-danger", "refs/heads/main"])
def test_rejects_missing_or_unsafe_branch(tmp_path, branch):
    repo, _ = _repo(tmp_path)
    issue = _issue(body=BODY.replace("feat/todo-42", branch))

    with pytest.raises(RunRegistrationError, match="branch_invalid"):
        _register(repo, issue=issue)


def test_rejects_multiple_branch_sections(tmp_path):
    repo, _ = _repo(tmp_path)
    issue = _issue(body=BODY + "\n### Branch\n\nfeat/other\n")

    with pytest.raises(RunRegistrationError, match="branch_invalid"):
        _register(repo, issue=issue)


def test_rejects_existing_worktree_with_wrong_branch_without_touching_it(tmp_path):
    repo, _ = _repo(tmp_path)
    target = repo / ".worktrees" / "todo-42-ship-the-feature"
    _git(repo, "worktree", "add", "-b", "unrelated", str(target), "HEAD")
    dirty = target / "keep.txt"
    dirty.write_text("preserve me")

    with pytest.raises(RunRegistrationError, match="worktree_mismatch"):
        _register(repo)

    assert (repo / ".hermes" / "runs" / "01TICK" / "registration.json").is_file()
    assert dirty.read_text() == "preserve me"
    assert _git(target, "branch", "--show-current") == "unrelated"


def test_rejects_reused_worktree_with_head_drift(tmp_path):
    repo, _ = _repo(tmp_path)
    registration = _register(repo)
    (registration.worktree / "change.txt").write_text("change")
    _git(registration.worktree, "add", "change.txt")
    _git(registration.worktree, "commit", "-m", "advance")

    with pytest.raises(RunRegistrationError, match="worktree_mismatch"):
        _register(repo)


@pytest.mark.parametrize("dirty_kind", ["staged", "unstaged", "untracked"])
def test_rejects_dirty_exact_worktree_without_mutation(tmp_path, dirty_kind):
    repo, _ = _repo(tmp_path)
    registration = _register(repo)
    worktree = registration.worktree
    if dirty_kind == "untracked":
        dirty_path = worktree / "keep-untracked.txt"
        dirty_path.write_text("preserve untracked")
        expected = "preserve untracked"
    else:
        dirty_path = worktree / "docs" / "plan.md"
        expected = f"preserve {dirty_kind}\n"
        dirty_path.write_text(expected)
        if dirty_kind == "staged":
            _git(worktree, "add", "docs/plan.md")
    status_before = _git(worktree, "status", "--short")

    with pytest.raises(RunRegistrationError, match="worktree_dirty"):
        _register(repo)

    assert dirty_path.read_text() == expected
    assert _git(worktree, "status", "--short") == status_before
    assert _git(worktree, "rev-parse", "HEAD") == registration.base_sha


def test_rejects_existing_worktree_from_wrong_repository(tmp_path):
    repo, _ = _repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-b", "main")
    _git(other, "config", "user.name", "Test")
    _git(other, "config", "user.email", "test@example.com")
    (other / "file").write_text("x")
    _git(other, "add", "file")
    _git(other, "commit", "-m", "other")
    target = repo / ".worktrees" / "todo-42-ship-the-feature"
    target.parent.mkdir()
    _git(other, "worktree", "add", str(target), "HEAD")

    with pytest.raises(RunRegistrationError, match="worktree_mismatch"):
        _register(repo)


def test_unrelated_dirty_state_is_preserved(tmp_path):
    repo, _ = _repo(tmp_path)
    dirty = repo / "notes.txt"
    dirty.write_text("mine")

    _register(repo)

    assert dirty.read_text() == "mine"
    assert "?? notes.txt" in _git(repo, "status", "--short")


def test_existing_registration_is_immutable(tmp_path):
    repo, _ = _repo(tmp_path)
    _register(repo)

    with pytest.raises(RunRegistrationError, match="registration_mismatch"):
        _register(repo, step_keys=("different",))


def test_secret_like_issue_body_is_pinned_authority_not_metadata(tmp_path):
    repo, _ = _repo(tmp_path)
    body = BODY + "\n### Why\n\npassword: hunter2\nAuthorization: Bearer x\x0c\n"

    registration = _register(repo, issue=_issue(body=body))

    assert "hunter2" in registration.issue_snapshot
    assert load_validated_registration(repo, repo / ".hermes", "01TICK").issue_number == 42


@pytest.mark.parametrize("plan_body", ["docs/other.md", "./docs/plan.md"])
def test_rejects_issue_plan_that_differs_from_plan_path(tmp_path, plan_body):
    repo, _ = _repo(tmp_path)
    issue = _issue(body=BODY.replace("docs/plan.md", plan_body))

    with pytest.raises(RunRegistrationError, match="plan_invalid"):
        _register(repo, issue=issue)


def test_rejects_snapshot_that_is_not_canonical_for_its_fields(tmp_path):
    repo, _ = _repo(tmp_path)
    issue = _issue()
    forged = replace(issue, snapshot=issue.snapshot.replace("Ship it.", "Ship it!"))

    with pytest.raises(RunRegistrationError, match="authority_invalid"):
        _register(repo, issue=forged)


def test_repo_identity_comparison_is_case_insensitive(tmp_path):
    repo, _ = _repo(tmp_path)
    _git(repo, "remote", "set-url", "origin", "https://github.com/ACME/Repo.git")

    registration = _register(repo)

    assert registration.issue_number == 42


# -- active-registration predicate ---------------------------------------------


def _write_registration(state_dir: Path, tick_id: str, payload) -> Path:
    run_dir = state_dir / "runs" / tick_id
    run_dir.mkdir(parents=True)
    path = run_dir / "registration.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return run_dir


def test_registration_state_reads_run_markers(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert registration_state(run_dir) == "active"
    (run_dir / "abandoned").touch()
    assert registration_state(run_dir) == "abandoned"
    (run_dir / "issue-closed").touch()
    assert registration_state(run_dir) == "delivered"


def test_active_registration_issue_numbers_filters_state_schema_and_malformed(
    tmp_path, caplog
):
    state = tmp_path / ".hermes"
    _write_registration(state, "t-active", {"schema_version": 2, "issue_number": 7})
    delivered = _write_registration(state, "t-done", {"schema_version": 2, "issue_number": 8})
    (delivered / "issue-closed").touch()
    abandoned = _write_registration(state, "t-gone", {"schema_version": 2, "issue_number": 9})
    (abandoned / "abandoned").touch()
    _write_registration(state, "t-v1", {"schema_version": 1, "issue_number": 10})
    _write_registration(state, "t-bad", "{not json")
    _write_registration(state, "t-num", {"schema_version": 2, "issue_number": "11"})

    with caplog.at_level("WARNING"):
        numbers = active_registration_issue_numbers(state)

    assert numbers == frozenset({7})
    assert isinstance(numbers, frozenset)
    assert "t-bad" in caplog.text and "t-num" in caplog.text and "t-v1" in caplog.text


def test_active_registration_issue_numbers_without_runs_dir(tmp_path):
    assert active_registration_issue_numbers(tmp_path / "missing") == frozenset()


# -- ensure_in_progress_label ---------------------------------------------------


def _live(fake_gh, number, labels):
    fake_gh.on(
        *API_ARGV, f"repos/{REPO}/issues/{number}",
        stdout=json.dumps(issue_payload(number, labels=labels)),
    )
    fake_gh.on("gh", "issue", "edit")


def test_ensure_in_progress_label_adds_missing_label(tmp_path, fake_gh):
    _live(fake_gh, 42, ("tpo:todo", "ready-for-agent"))

    assert ensure_in_progress_label(tmp_path, {"issue_number": 42}, repo=REPO) is True
    assert [
        "issue", "edit", "42", "--repo", REPO, "--add-label", IN_PROGRESS_LABEL
    ] in fake_gh.gh_calls()


def test_ensure_in_progress_label_is_idempotent(tmp_path, fake_gh):
    _live(fake_gh, 42, ("tpo:todo", IN_PROGRESS_LABEL))

    assert ensure_in_progress_label(tmp_path, {"issue_number": 42}, repo=REPO) is False
    assert not any(call[:2] == ["issue", "edit"] for call in fake_gh.gh_calls())


def test_ensure_in_progress_label_is_best_effort(tmp_path, fake_gh, caplog):
    fake_gh.on(*API_ARGV, rc=1, stderr="HTTP 500")

    with caplog.at_level("WARNING"):
        assert ensure_in_progress_label(tmp_path, {"issue_number": 42}, repo=REPO) is False
    assert "gh_unavailable" in caplog.text
    assert ensure_in_progress_label(tmp_path, {"issue_number": "x"}, repo=REPO) is False


# -- branch safety (C1) ---------------------------------------------------------


def test_rejects_default_branch_as_run_branch(tmp_path):
    repo, _ = _repo(tmp_path)

    with pytest.raises(RunRegistrationError, match="branch_invalid: default_branch"):
        _register(repo, issue=_issue(body=BODY.replace("feat/todo-42", "main")))


def test_rejects_default_branch_resolved_from_origin_head(tmp_path):
    repo, _ = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")

    with pytest.raises(RunRegistrationError, match="branch_invalid: default_branch"):
        _register(repo, issue=_issue(body=BODY.replace("feat/todo-42", "develop")))


def test_rejects_branch_that_already_exists_for_another_run(tmp_path):
    repo, _ = _repo(tmp_path)
    _git(repo, "branch", "feat/todo-42")

    with pytest.raises(RunRegistrationError, match="branch_exists"):
        _register(repo)
    assert not (repo / ".hermes" / "runs" / "01TICK" / "registration.json").exists()


def test_rejects_branch_that_exists_only_on_origin(tmp_path):
    repo, _ = _repo(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/feat/todo-42", "HEAD")

    with pytest.raises(RunRegistrationError, match="branch_exists"):
        _register(repo)


def test_resume_of_the_same_issue_may_reuse_its_branch(tmp_path):
    repo, _ = _repo(tmp_path)
    first = _register(repo, tick_id="01FIRST")

    second = _register(repo, tick_id="02SECOND")

    assert second.branch == first.branch == "feat/todo-42"
    assert second.worktree == first.worktree


def test_active_registration_issue_numbers_warns_once_per_run_dir(tmp_path, caplog):
    state = tmp_path / ".hermes"
    _write_registration(state, "t-bad", "{not json")

    with caplog.at_level("DEBUG"):
        active_registration_issue_numbers(state)
        active_registration_issue_numbers(state)

    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "t-bad" in r.getMessage()]
    debugs = [r for r in caplog.records if r.levelname == "DEBUG" and "t-bad" in r.getMessage()]
    assert len(warnings) == 1 and len(debugs) == 1
