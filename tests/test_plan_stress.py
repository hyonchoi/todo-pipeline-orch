"""A 50-task Plan at the new bound: one card, one report, fifty commits.

``MAX_PLAN_TASKS`` used to be a card-count stress: fifty ``plan:<task-id>``
cards, fifty creates, a fifty-deep ``--parent`` chain. The phase registers one
card now, so what is left to stress is the single report the profile obliges
that card to make: fifty commits measured in one hop from ``base_sha``, and
fifty acceptance criteria echoed inside the ``MAX_METADATA_BYTES`` cap.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

TASK_COUNT = 50
TICK_ID = "01STRESS"
TODO_ID = "TODO-50"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _manifest() -> str:
    tasks = [
        {
            "id": f"task-{number}",
            "title": f"Stress task {number}",
            "instructions": f"Implement bounded change {number}.",
            "acceptance_criteria": [f"Change {number} is observable."],
            "verification": [f"uv run pytest tests/test_change_{number}.py"],
            "commit_message": f"feat(stress): change {number}",
        }
        for number in range(1, TASK_COUNT + 1)
    ]
    payload = {"schema_version": 1, "todo_id": TODO_ID, "tasks": tasks}
    return f"# Stress plan\n\n```json tpo-plan\n{json.dumps(payload)}\n```\n"


def test_fifty_task_manifest_is_one_card_one_report_and_fifty_commits(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        KanbanTaskInfo,
        prepare_todo_phases,
        reconcile_plan_task_results,
    )
    from hermes_pipeline.phases import IMPLEMENTATION_KEY
    from hermes_pipeline.plan_manifest import legacy_plan_source
    from hermes_pipeline.result_contract import (
        MAX_METADATA_BYTES,
        manifest_acceptance_criteria,
        render_result_template,
    )
    from hermes_pipeline.run_registration import register_pinned_run
    from tests.gh_fakes import make_issue

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "plan.md").write_text(_manifest())
    phases = repo / "phases.yaml"
    phases.write_text(
        "requires_plan: true\n"
        "phases:\n"
        "  - phase_key: phase_4_development\n"
        "    name: Development\n"
        "    prompt: implement the plan\n"
        "    tools: Read,Write,Edit,Bash\n"
        "    turns: 100\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")

    prepared = prepare_todo_phases(
        todo_id=TODO_ID,
        tick_id=TICK_ID,
        board_slug="stress",
        phases_path=phases,
        plan_path="plan.md",
        project_dir=repo,
    )
    # Fifty tasks, one card. The board no longer scales with the Plan.
    assert [task.phase_key for task in prepared] == [IMPLEMENTATION_KEY]

    state = repo / ".hermes"
    registration = register_pinned_run(
        project_dir=repo,
        state_dir=state,
        tick_id=TICK_ID,
        selected_issue=make_issue(
            int(TODO_ID[5:]),
            repo="acme/repo",
            title="Stress compilation",
            body="### Plan\n\nplan.md\n\n### Branch\n\ntodo-50-stress\n",
        ),
        plan_path="plan.md",
        profile="native-sdd",
        prompt_client="claude",
        assignee="pipeline",
        review_assignee=None,
        step_keys=(IMPLEMENTATION_KEY,),
    )
    manifest = legacy_plan_source(
        repo, "plan.md", expected_todo_id=TODO_ID
    ).manifest
    criteria = manifest_acceptance_criteria(manifest)
    assert len(criteria) == TASK_COUNT
    # One report now answers for every task's criteria, so the whole Plan's
    # criteria must fit the cap that used to apply one task at a time. Fifty
    # ordinary criteria leave an order of magnitude of headroom; a Plan whose
    # combined criteria exceed the cap is unreportable, which is why the
    # template is measured here and not merely rendered.
    template = render_result_template(
        tick_id=TICK_ID, todo_id=TODO_ID, step_key=IMPLEMENTATION_KEY,
        acceptance_criteria=criteria,
    )
    assert len(template.encode()) < MAX_METADATA_BYTES

    base = _git(registration.worktree, "rev-parse", "HEAD")
    changed = []
    for number in range(1, TASK_COUNT + 1):
        path = registration.worktree / f"change-{number}.txt"
        path.write_text(str(number))
        changed.append(path.name)
        _git(registration.worktree, "add", path.name)
        _git(registration.worktree, "commit", "-qm", f"change {number}")
    head = _git(registration.worktree, "rev-parse", "HEAD")

    payload = {
        "runs": [
            {
                "status": "succeeded",
                "metadata": {
                    "tpo_result": {
                        "schema_version": 1,
                        "tick_id": TICK_ID,
                        "todo_id": TODO_ID,
                        "step_key": IMPLEMENTATION_KEY,
                        "verdict": "success",
                        "git": {
                            "expected_parent_sha": base,
                            "resulting_head_sha": head,
                            "task_commit_sha": head,
                            "changed_files": changed,
                        },
                        "acceptance": [
                            {"criterion": criterion, "status": "passed"}
                            for criterion in criteria
                        ],
                    }
                },
            }
        ]
    }
    assert len(json.dumps(payload["runs"][0]["metadata"])) < MAX_METADATA_BYTES

    cards = {
        IMPLEMENTATION_KEY: KanbanTaskInfo(
            "worker", IMPLEMENTATION_KEY, "done", TODO_ID
        )
    }
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: cards,
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload", return_value=payload
    )
    complete = mocker.patch(
        "hermes_pipeline.kanban_tasks.complete_todo_kanban_task", return_value=True
    )

    for _ in range(2):  # Reconciliation is idempotent across ticks.
        assert reconcile_plan_task_results(
            project_dir=repo, state_dir=state, tenant="stress", tick_id=TICK_ID
        )
    # Nothing is completed and nothing is blocked: the card is a pure worker.
    complete.assert_not_called()

    # The card is still the chain tip, so it is verified against the live
    # worktree.
    stray = registration.worktree / "stray.txt"
    stray.write_text("uncommitted")
    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="stress", tick_id=TICK_ID
    )
    stray.unlink()

    # Forty-nine commits is short of what fifty tasks owe, whatever the count.
    payload["runs"][0]["metadata"]["tpo_result"]["git"].update(
        resulting_head_sha=_git(registration.worktree, "rev-parse", "HEAD~1"),
        task_commit_sha=_git(registration.worktree, "rev-parse", "HEAD~1"),
        changed_files=changed[:-1],
    )
    assert not reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="stress", tick_id=TICK_ID
    )
    marker = state / "runs" / TICK_ID / "result-validation-blocked"
    assert json.loads(marker.read_text())["code"] == "commit_count_mismatch"
