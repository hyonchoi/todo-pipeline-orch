"""Shared helpers for test result contracts and registration verification."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_pipeline.phases import IMPLEMENTATION_KEY
from tests.gh_fakes import REPO, make_issue
from tests.support.git import run_git as _git

ISSUE_BODY = "### What\n\nResult contract.\n\n### Plan\n\nplan.md\n\n### Branch\n\ntodo-42\n"

PLAN = '''# Plan

```json tpo-plan
{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-1","title":"Do it","instructions":"Implement it.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it"}]}
```
'''

PLAN_TWO_TASKS = '''# Plan

```json tpo-plan
{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-1","title":"Do it","instructions":"Implement it.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it"},{"id":"task-2","title":"Do it again","instructions":"Implement it again.","acceptance_criteria":["Observable criterion"],"verification":["uv run pytest"],"commit_message":"feat: do it again"}]}
```
'''


def _result(**updates):
    """Create a structurally valid result dict, mutable one field at a time."""
    value = {
        "schema_version": 1,
        "tick_id": "01TICK",
        "todo_id": "TODO-42",
        "step_key": "plan:task-1",
        "verdict": "success",
        "git": {
            "expected_parent_sha": "a" * 40,
            "resulting_head_sha": "b" * 40,
            "task_commit_sha": "b" * 40,
            "changed_files": ["src/example.py"],
        },
        "acceptance": [{"criterion": "Observable criterion", "status": "passed"}],
    }
    value.update(updates)
    return value


def _delivery(*, command="uv run pytest", **check_extra):
    """A structurally valid ``delivery`` block, mutable one field at a time."""
    return {
        "pr_url": "https://github.com/acme/repo/pull/7",
        "branch": "todo-42",
        "head_sha": "b" * 40,
        "checks": [{"command": command, "exit_code": 0, **check_extra}],
    }


def _commit(worktree: Path, name: str) -> str:
    """Create a commit in the worktree and return the commit SHA."""
    (worktree / name).write_text(name)
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", name)
    return _git(worktree, "rev-parse", "HEAD")


def _rewrite_registration(state: Path, mutate):
    """Rewrite the registration JSON by applying a mutation function."""
    path = state / "runs" / "01TICK" / "registration.json"
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload))
    return payload


def _registered_repo(
    tmp_path: Path,
    *,
    issue_body: str = ISSUE_BODY,
    plan_path: str | None = "plan.md",
    embedded: bool = False,
    plan: str = PLAN,
    legacy: bool = False,
    step_keys: tuple[str, ...] = (IMPLEMENTATION_KEY,),
) -> tuple[Path, Path, Path, str]:
    """Initialize a registered repository for testing.

    Returns (repo_path, worktree_path, state_dir, parent_sha).
    """
    from hermes_pipeline.plan_manifest import render_embedded_plan
    from hermes_pipeline.run_registration import register_pinned_run

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", f"git@github.com:{REPO}.git")
    (repo / "plan.md").write_text(plan)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    parent = _git(repo, "rev-parse", "HEAD")
    state = repo / ".hermes"
    if embedded:
        issue_body = ISSUE_BODY.replace("### Plan\n\nplan.md\n\n", "")
        issue_body += render_embedded_plan(plan, expected_todo_id="TODO-42")
        plan_path = None
    registration = register_pinned_run(
        project_dir=repo,
        state_dir=state,
        tick_id="01TICK",
        selected_issue=make_issue(42, repo=REPO, title="Do it", body=issue_body),
        plan_path=plan_path,
        profile="native-sdd",
        prompt_client="claude",
        assignee="pipeline",
        review_assignee=None,
        step_keys=step_keys,
    )
    if legacy:
        # Explicitly model a pre-supervisor Hermes-only registration.
        _rewrite_registration(state, lambda payload: (
            payload.update(schema_version=3), payload.pop("agent_policy_mode")
        ))
    return repo, registration.worktree, state, parent


def worker_payload(
    *,
    step_key: str,
    parent: str,
    head: str,
    changed: list[str],
) -> dict:
    """Create a worker payload with the given parameters."""
    result = _result(step_key=step_key)
    result["git"] = {
        "expected_parent_sha": parent,
        "resulting_head_sha": head,
        "task_commit_sha": head,
        "changed_files": changed,
    }
    # Hermes stamps ``worker_session_id`` on its own tool path only; live runs
    # also carry worker-authored siblings. The reconciler ignores them all.
    return {
        "runs": [
            {
                "status": "succeeded",
                "metadata": {
                    "tpo_result": result,
                    "worker_session_id": "20260904_154528_b0f01d",
                },
            }
        ]
    }
