"""Shared helpers for test registration and repository setup."""

from pathlib import Path

from hermes_pipeline.plan_manifest import render_embedded_plan
from hermes_pipeline.run_registration import register_pinned_run
from tests.gh_fakes import REPO, make_issue
from tests.support.git import init_repo

BODY = (
    "### What\n\nShip it.\n\n### Plan\n\ndocs/plan.md\n\n"
    "### Branch\n\nfeat/todo-42\n"
)
EMBEDDED_DOCUMENT = """# Implementation Plan

Ship it safely.

```json tpo-plan
{"schema_version":1,"todo_id":"TODO-42","tasks":[{"id":"task-1","title":"Ship","instructions":"Do it","acceptance_criteria":["Works"],"verification":["pytest"],"commit_message":"feat: ship"}]}
```
"""


def _issue(number: int = 42, *, body: str = BODY, repo: str = REPO, **extra):
    """Helper to create an issue with defaults."""
    return make_issue(number, repo=repo, title="Ship the feature", body=body, **extra)


def embedded_issue():
    """Create an embedded issue with an embedded plan document."""
    body = (
        "### What\n\nShip it.\n\n### Branch\n\nfeat/todo-42\n\n"
        + render_embedded_plan(EMBEDDED_DOCUMENT, expected_todo_id="TODO-42")
    )
    return _issue(body=body)


def seeded_repo(tmp_path: Path) -> tuple[Path, str]:
    """Initialize a test repository with a plan file.

    Returns (repository_path, base_sha).
    """
    return init_repo(
        tmp_path / "project",
        branch="main",
        files={"docs/plan.md": "# Plan\n"},
        origin=f"https://github.com/{REPO}.git",
    )


def register(
    project: Path,
    *,
    tick_id: str = "01TICK",
    step_keys=("task-1", "gate-1"),
    plan_path: str = "docs/plan.md",
    issue=None,
    **kwargs,
):
    """Register a pinned run with default parameters."""
    return register_pinned_run(
        project_dir=project,
        state_dir=project / ".hermes",
        tick_id=tick_id,
        selected_issue=issue if issue is not None else _issue(),
        plan_path=plan_path,
        profile=kwargs.pop("profile", "native-sdd"),
        prompt_client="codex",
        assignee="implementer",
        review_assignee="reviewer",
        step_keys=step_keys,
        **kwargs,
    )
