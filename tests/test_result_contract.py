from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_pipeline.result_contract import (
    ResultContractError,
    load_validated_registration,
    parse_worker_result,
    sanitize_result_text,
    verify_worker_git_result,
    verify_worker_git_topology,
)

PLAN = '''# Plan

```json tpo-plan
{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-1","title":"Do it","instructions":"Implement it.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it"}]}
```
'''


def _result(**updates):
    value = {
        "schema_version": 1,
        "tick_id": "01TICK",
        "todo_id": "TODO-42",
        "step_key": "plan:task-1",
        "verdict": "success",
        "external_session_id": "session-1",
        "git": {
            "expected_parent_sha": "a" * 40,
            "resulting_head_sha": "b" * 40,
            "task_commit_sha": "b" * 40,
            "changed_files": ["src/example.py"],
        },
        "tdd": {
            "red": {"command": "uv run pytest tests/test_example.py", "exit_code": 1},
            "green": {"command": "uv run pytest tests/test_example.py", "exit_code": 0},
            "refactor": {"command": "uv run pytest tests/test_example.py", "exit_code": 0},
        },
        "acceptance": [{"criterion": "Observable criterion", "status": "passed"}],
    }
    value.update(updates)
    return value


def test_parse_valid_final_successful_run():
    payload = {
        "runs": [
            {"status": "failed", "metadata": {"tpo_result": _result()}},
            {"status": "succeeded", "summary": "done", "metadata": {"tpo_result": _result()}},
        ]
    }
    parsed = parse_worker_result(
        payload,
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    assert parsed.external_session_id == "session-1"
    assert parsed.git.changed_files == ("src/example.py",)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda p: p.update(tick_id="wrong"), "identity_mismatch"),
        (lambda p: p.update(external_session_id=""), "invalid_session"),
        (lambda p: p["git"].update(task_commit_sha="c" * 40), "invalid_git"),
        (lambda p: p["tdd"]["red"].update(exit_code=0), "invalid_tdd"),
        (lambda p: p["tdd"]["green"].update(exit_code=1), "invalid_tdd"),
        (lambda p: p["acceptance"][0].update(status="pending"), "invalid_acceptance"),
        (lambda p: p["tdd"]["red"].update(command="x" * 501), "size_limit"),
    ],
)
def test_parse_rejects_invalid_contract(mutation, code):
    result = _result()
    mutation(result)
    with pytest.raises(ResultContractError, match=code):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def test_parse_rejects_malformed_missing_and_oversized_metadata():
    for payload in ({}, {"runs": [{}]}, {"runs": [{"status": "succeeded", "metadata": {}}]}):
        with pytest.raises(ResultContractError):
            parse_worker_result(
                payload,
                tick_id="01TICK",
                todo_id="TODO-42",
                step_key="plan:task-1",
                acceptance_criteria=("Observable criterion",),
            )
    oversized = _result(extra="x" * 65536)
    with pytest.raises(ResultContractError, match="size_limit"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": oversized}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


@pytest.mark.parametrize(
    "sibling",
    [
        {"provider_body": "password=super-secret"},
        {"unsafe\x00key": "value"},
        {"padding": "x" * 65536},
    ],
)
def test_parse_validates_entire_enclosing_run_metadata(sibling):
    metadata = {"tpo_result": _result(), **sibling}
    with pytest.raises(ResultContractError, match="unsafe_metadata|size_limit|malformed_result"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": metadata}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def test_summary_and_diagnostics_are_sanitized():
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    sanitized = sanitize_result_text(f"bad\x00 token {secret}", maximum=8192)
    assert "\x00" not in sanitized
    assert secret not in sanitized
    assert "[REDACTED]" in sanitized


@pytest.mark.parametrize(
    "path",
    [
        ("git", "changed_files", 0),
        ("acceptance", 0, "criterion"),
        ("delivery", "checks", 0, "command"),
    ],
)
def test_parse_rejects_secrets_and_controls_at_any_metadata_depth(path):
    result = _result()
    result["delivery"] = {
        "pr_url": "https://github.com/example/repo/pull/1",
        "branch": "todo-42",
        "head_sha": "b" * 40,
        "checks": [{"command": "uv run pytest", "exit_code": 0}],
    }
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "password=super-secret\x00"
    with pytest.raises(ResultContractError, match="unsafe_metadata"):
        parse_worker_result(
            {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
            tick_id="01TICK",
            todo_id="TODO-42",
            step_key="plan:task-1",
            acceptance_criteria=("Observable criterion",),
        )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def test_verify_git_requires_exactly_one_commit_and_matching_changed_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "change.txt").write_text("change")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "change")
    head = _git(repo, "rev-parse", "HEAD")
    result = _result()
    result["git"] = {
        "expected_parent_sha": parent,
        "resulting_head_sha": head,
        "task_commit_sha": head,
        "changed_files": ["change.txt"],
    }
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)
    with pytest.raises(ResultContractError, match="changed_files_mismatch"):
        verify_worker_git_result(
            repo,
            parsed.git.__class__(parent, head, head, ("wrong.txt",)),
            expected_parent_sha=parent,
        )
    (repo / "untracked.txt").write_text("keep")
    with pytest.raises(ResultContractError, match="worktree_dirty"):
        verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)


def test_historical_git_topology_remains_valid_after_later_fix_commit(tmp_path):
    repo = tmp_path / "repo-history"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "first.txt").write_text("first")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("later")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "later")

    from hermes_pipeline.result_contract import GitResult

    verify_worker_git_topology(
        repo,
        GitResult(parent, first, first, ("first.txt",)),
        expected_parent_sha=parent,
    )
    with pytest.raises(ResultContractError, match="head_mismatch"):
        verify_worker_git_result(
            repo,
            GitResult(parent, first, first, ("first.txt",)),
            expected_parent_sha=parent,
        )


@pytest.mark.skipif(os.name == "nt", reason="byte filenames require POSIX")
def test_verify_git_reports_invalid_byte_untracked_filename_as_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "change.txt").write_text("change")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "change")
    head = _git(repo, "rev-parse", "HEAD")
    try:
        fd = os.open(
            os.fsencode(repo) + b"/invalid-\xff", os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError as exc:
        pytest.skip(f"filesystem rejects invalid-byte filenames: errno={exc.errno}")
    os.close(fd)
    git_result = _result()["git"]
    git_result.update(
        expected_parent_sha=parent,
        resulting_head_sha=head,
        task_commit_sha=head,
        changed_files=["change.txt"],
    )
    parsed = parse_worker_result(
        {"runs": [{"status": "succeeded", "metadata": {"tpo_result": {**_result(), "git": git_result}}}]},
        tick_id="01TICK",
        todo_id="TODO-42",
        step_key="plan:task-1",
        acceptance_criteria=("Observable criterion",),
    )
    with pytest.raises(ResultContractError, match="worktree_dirty"):
        verify_worker_git_result(repo, parsed.git, expected_parent_sha=parent)


def test_registration_rejects_unknown_keys_and_mutable_plan_drift(tmp_path):
    repo, worktree, state, _parent = _registered_repo(tmp_path)
    registration = state / "runs" / "01TICK" / "registration.json"
    payload = json.loads(registration.read_text())
    payload["unknown"] = True
    registration.write_text(json.dumps(payload))
    with pytest.raises(ResultContractError, match="registration_invalid"):
        load_validated_registration(repo, state, "01TICK")

    del payload["unknown"]
    registration.write_text(json.dumps(payload))
    (worktree / "plan.md").write_text("mutable drift")
    authority = load_validated_registration(repo, state, "01TICK")
    assert authority.manifest.tasks[0].id == "task-1"


def _registered_repo(tmp_path):
    from hermes_pipeline.run_registration import register_pinned_run
    from hermes_pipeline.todos_md import parse_todo_entries

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    todos = (
        "## Entries\n\n"
        "- [ ] **TODO-42: Do it** — Result contract.\n"
        "  - **Plan:** plan.md\n"
        "  - **Branch:** todo-42\n"
    )
    (repo / "TODOS.md").write_text(todos)
    (repo / "plan.md").write_text(PLAN)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    state = repo / ".hermes"
    registration = register_pinned_run(
        project_dir=repo,
        state_dir=state,
        tick_id="01TICK",
        selected_entry=parse_todo_entries(todos)[0],
        plan_path="plan.md",
        profile="native-sdd",
        prompt_client="claude",
        assignee="pipeline",
        review_assignee=None,
        step_keys=("plan:task-1", "validate:task-1"),
    )
    return repo, registration.worktree, state, parent


def test_reconcile_completed_worker_validates_then_completes_gate(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, parent = _registered_repo(tmp_path)
    (worktree / "change.txt").write_text("change")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "change")
    head = _git(worktree, "rev-parse", "HEAD")
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42"),
            "validate:task-1": KanbanTaskInfo("gate", "validate:task-1", "blocked", "TODO-42"),
        },
    )
    result = _result()
    result["git"] = {
        "expected_parent_sha": parent,
        "resulting_head_sha": head,
        "task_commit_sha": head,
        "changed_files": ["change.txt"],
    }
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        return_value={"runs": [{"status": "succeeded", "metadata": {"tpo_result": result}}]},
    )
    complete = mocker.patch(
        "hermes_pipeline.kanban_tasks.complete_todo_kanban_task", return_value=True
    )

    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    complete.assert_called_once_with("demo", "gate")


def test_reconcile_invalid_result_marks_gate_needs_input(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import KanbanTaskInfo, reconcile_plan_task_results

    repo, worktree, state, _parent = _registered_repo(tmp_path)
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        return_value={
            "plan:task-1": KanbanTaskInfo("worker", "plan:task-1", "done", "TODO-42"),
            "validate:task-1": KanbanTaskInfo("gate", "validate:task-1", "blocked", "TODO-42"),
        },
    )
    mocker.patch("hermes_pipeline.kanban_tasks._show_task_payload", return_value={"runs": []})
    blocked = mocker.patch("hermes_pipeline.kanban_tasks._mark_gate_needs_input")
    complete = mocker.patch("hermes_pipeline.kanban_tasks.complete_todo_kanban_task")

    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="demo", tick_id="01TICK"
    )
    blocked.assert_called_once()
    complete.assert_not_called()
