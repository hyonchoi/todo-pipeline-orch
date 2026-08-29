from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_pipeline.github_issues import MAX_ISSUE_SNAPSHOT_CHARS, snapshot_hash
from hermes_pipeline.result_contract import load_validated_registration
from hermes_pipeline.run_registration import (
    RunRegistrationError,
    register_pinned_run,
    register_pinned_run_from_entry,
)
from hermes_pipeline.todos_md import parse_todo_entries
from tests.gh_fakes import make_issue

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


# -- TODO(1.5): remove with the shim -------------------------------------------

TODOS = (
    "# TODOS\n\n## Entries\n\n"
    "- [ ] **TODO-42: Ship the feature**\n"
    "  - **Plan:** docs/plan.md\n"
    "  - **Branch:** feat/todo-42\n"
)


def _shim_repo(tmp_path, todos: str = TODOS):
    repo, _ = _repo(tmp_path)
    (repo / "TODOS.md").write_text(todos)
    _git(repo, "add", "TODOS.md")
    _git(repo, "commit", "-m", "todos")
    return repo, parse_todo_entries(todos)[0]


def _register_from_entry(repo, entry):
    return register_pinned_run_from_entry(
        project_dir=repo,
        state_dir=repo / ".hermes",
        tick_id="01TICK",
        selected_entry=entry,
        plan_path="docs/plan.md",
        profile="native-sdd",
        prompt_client="codex",
        assignee="implementer",
        review_assignee=None,
        step_keys=("task-1",),
    )


def test_shim_registration_round_trips_through_loader(tmp_path):
    repo, entry = _shim_repo(tmp_path)

    registration = _register_from_entry(repo, entry)

    assert "### Plan\n\ndocs/plan.md" in registration.issue_snapshot
    assert "### Branch\n\nfeat/todo-42" in registration.issue_snapshot
    authority = load_validated_registration(repo, repo / ".hermes", "01TICK")
    assert authority.branch == "feat/todo-42"
    assert authority.worktree.name == "todo-42-ship-the-feature"


def test_shim_is_disabled_outside_tests(tmp_path, monkeypatch):
    repo, entry = _shim_repo(tmp_path)
    monkeypatch.delenv("TPO_LEGACY_TODOS_SHIM")

    with pytest.raises(RunRegistrationError, match="legacy_shim_disabled"):
        _register_from_entry(repo, entry)


def test_shim_requires_github_origin(tmp_path):
    repo, entry = _shim_repo(tmp_path)
    _git(repo, "remote", "remove", "origin")

    with pytest.raises(RunRegistrationError, match="origin_identity_invalid"):
        _register_from_entry(repo, entry)


def test_shim_rejects_todos_drift_against_base(tmp_path):
    repo, entry = _shim_repo(tmp_path)
    (repo / "TODOS.md").write_text(TODOS.replace("Ship the feature", "Changed"))
    _git(repo, "add", "TODOS.md")
    _git(repo, "commit", "-m", "drift")

    with pytest.raises(RunRegistrationError, match="authority_drift"):
        _register_from_entry(repo, entry)


def test_shim_rejects_non_canonical_todo_id(tmp_path):
    repo, entry = _shim_repo(tmp_path, TODOS.replace("TODO-42", "TODO-007"))

    with pytest.raises(RunRegistrationError, match="authority_invalid"):
        _register_from_entry(repo, entry)


def test_shim_preserves_duplicate_branch_values(tmp_path):
    repo, entry = _shim_repo(
        tmp_path, TODOS + "  - **Branch:** feat/other\n"
    )
    assert len(entry.branch_values) == 2

    with pytest.raises(RunRegistrationError, match="branch_invalid"):
        _register_from_entry(repo, entry)
