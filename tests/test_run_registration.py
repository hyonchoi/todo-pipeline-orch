from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from hermes_pipeline.run_registration import (
    RunRegistrationError,
    register_pinned_run,
)
from hermes_pipeline.todos_md import parse_todo_entries


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
    (repo / "TODOS.md").write_text(
        "# TODOS\n\n## Entries\n\n"
        "- [ ] **TODO-42: Ship the feature**\n"
        "  - **Plan:** docs/plan.md\n"
        "  - **Branch:** feat/todo-42\n"
    )
    _git(repo, "add", "TODOS.md", "docs/plan.md")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _register(
    repo: Path,
    *,
    tick_id: str = "01TICK",
    step_keys=("task-1", "gate-1"),
    plan_path: str = "docs/plan.md",
):
    entry = parse_todo_entries((repo / "TODOS.md").read_text())[0]
    return register_pinned_run(
        project_dir=repo,
        state_dir=repo / ".hermes",
        tick_id=tick_id,
        selected_entry=entry,
        plan_path=plan_path,
        profile="native-sdd",
        prompt_client="codex",
        assignee="implementer",
        review_assignee="reviewer",
        step_keys=step_keys,
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
    entry = parse_todo_entries((repo / "TODOS.md").read_text())[0]
    assert payload == {
        "assignee": "implementer",
        "base_sha": base_sha,
        "branch": "feat/todo-42",
        "plan_hash": hashlib.sha256(b"# Plan\n").hexdigest(),
        "plan_path": "docs/plan.md",
        "profile": "native-sdd",
        "prompt_client": "codex",
        "repository": str(repo.resolve()),
        "review_assignee": "reviewer",
        "schema_version": 1,
        "selected_entry_hash": hashlib.sha256(entry.raw.encode()).hexdigest(),
        "step_keys": ["task-1", "gate-1"],
        "tick_id": "01TICK",
        "todo_id": "TODO-42",
        "worktree": str(expected_path.resolve()),
    }


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
        (
            lambda repo: (repo / "TODOS.md").write_text(
                (repo / "TODOS.md").read_text().replace(
                    "Ship the feature", "Changed feature"
                )
            ),
            "authority_drift",
        ),
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
    text = (repo / "TODOS.md").read_text().replace("docs/plan.md", "docs/draft.md")
    (repo / "TODOS.md").write_text(text)
    _git(repo, "add", "TODOS.md")
    _git(repo, "commit", "-m", "point at draft")

    with pytest.raises(RunRegistrationError, match="authority_untracked"):
        _register(repo, plan_path="docs/draft.md")


def test_unrelated_todos_edit_does_not_drift_selected_entry(tmp_path):
    repo, _ = _repo(tmp_path)
    text = (repo / "TODOS.md").read_text()
    (repo / "TODOS.md").write_text(text.replace("## Entries", "New intro.\n\n## Entries"))

    registration = _register(repo)

    assert registration.todo_id == "TODO-42"


@pytest.mark.parametrize("branch", ["", "bad branch", "-danger", "refs/heads/main"])
def test_rejects_missing_or_unsafe_branch(tmp_path, branch):
    repo, _ = _repo(tmp_path)
    text = (repo / "TODOS.md").read_text().replace("feat/todo-42", branch)
    (repo / "TODOS.md").write_text(text)
    _git(repo, "add", "TODOS.md")
    _git(repo, "commit", "-m", "branch")

    with pytest.raises(RunRegistrationError, match="branch_invalid"):
        _register(repo)


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
