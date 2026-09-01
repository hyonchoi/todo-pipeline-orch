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


def _worker_result(number: int, parent: str, head: str) -> dict[str, object]:
    criterion = f"Change {number} is observable."
    command = f"uv run pytest tests/test_change_{number}.py"
    return {
        "schema_version": 1,
        "tick_id": TICK_ID,
        "todo_id": TODO_ID,
        "step_key": f"plan:task-{number}",
        "verdict": "success",
        "external_session_id": f"session-{number}",
        "git": {
            "expected_parent_sha": parent,
            "resulting_head_sha": head,
            "task_commit_sha": head,
            "changed_files": [f"change-{number}.txt"],
        },
        "tdd": {
            "red": {"command": command, "exit_code": 1},
            "green": {"command": command, "exit_code": 0},
            "refactor": {"command": command, "exit_code": 0},
        },
        "acceptance": [{"criterion": criterion, "status": "passed"}],
    }


def test_fifty_task_manifest_registration_and_reconciliation_are_bounded_and_idempotent(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        KanbanTaskInfo,
        create_prepared_todo_phases,
        prepare_todo_phases,
        reconcile_plan_task_results,
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
        "  - phase_key: development\n"
        "    name: Development\n"
        "    prompt: legacy fallback\n"
        "    tools: Read,Write,Edit,Bash\n"
        "    turns: 20\n"
        "    compile_plan_tasks: true\n"
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
    expected_keys = [
        key
        for number in range(1, TASK_COUNT + 1)
        for key in (f"plan:task-{number}", f"validate:task-{number}")
    ]
    assert len(prepared) == 2 * TASK_COUNT
    assert [task.phase_key for task in prepared] == expected_keys
    assert [task.kind for task in prepared] == [
        kind
        for _ in range(TASK_COUNT)
        for kind in ("worker", "controller_gate")
    ]

    task_ids: dict[str, str] = {}
    create_commands: list[list[str]] = []

    def run(cmd, **_kwargs):
        if cmd[:3] == ["hermes", "kanban", "create"]:
            key = cmd[cmd.index("--idempotency-key") + 1]
            task_ids.setdefault(key, f"t_{len(task_ids) + 1:08x}")
            create_commands.append(cmd)
            return mocker.Mock(
                returncode=0, stdout=json.dumps({"id": task_ids[key]}), stderr=""
            )
        return mocker.Mock(returncode=0, stdout="", stderr="")

    run_mock = mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run", side_effect=run
    )
    first_ids = create_prepared_todo_phases(
        prepared=prepared,
        tick_id=TICK_ID,
        board_slug="stress",
        project_dir=repo,
    )
    second_ids = create_prepared_todo_phases(
        prepared=prepared,
        tick_id=TICK_ID,
        board_slug="stress",
        project_dir=repo,
    )
    mocker.stop(run_mock)
    assert first_ids == second_ids
    assert len(first_ids) == 2 * TASK_COUNT
    first_commands = create_commands[: 2 * TASK_COUNT + 1]
    for index, command in enumerate(first_commands[1:]):
        parent = command[command.index("--parent") + 1]
        assert parent == task_ids[
            f"{TICK_ID}:__registration_barrier__"
            if index == 0
            else f"{TICK_ID}:{expected_keys[index - 1]}"
        ]

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
        step_keys=tuple(expected_keys),
    )
    cards: dict[str, KanbanTaskInfo] = {}
    results: dict[str, dict[str, object]] = {}
    completed_gates: set[str] = set()
    for number in range(1, TASK_COUNT + 1):
        worker_key = f"plan:task-{number}"
        gate_key = f"validate:task-{number}"
        cards[worker_key] = KanbanTaskInfo(
            f"worker-{number}", worker_key, "queued", TODO_ID
        )
        cards[gate_key] = KanbanTaskInfo(
            f"gate-{number}", gate_key, "blocked", TODO_ID
        )
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_tasks",
        side_effect=lambda *_args: cards,
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._show_task_payload",
        side_effect=lambda task_id: results[task_id],
    )

    def complete(_tenant, task_id):
        completed_gates.add(task_id)
        return True

    mocker.patch(
        "hermes_pipeline.kanban_tasks.complete_todo_kanban_task",
        side_effect=complete,
    )
    parent = _git(registration.worktree, "rev-parse", "HEAD")
    for number in range(1, TASK_COUNT + 1):
        path = registration.worktree / f"change-{number}.txt"
        path.write_text(str(number))
        _git(registration.worktree, "add", path.name)
        _git(registration.worktree, "commit", "-qm", f"change {number}")
        head = _git(registration.worktree, "rev-parse", "HEAD")
        worker_key = f"plan:task-{number}"
        gate_key = f"validate:task-{number}"
        worker_id = f"worker-{number}"
        gate_id = f"gate-{number}"
        cards[worker_key] = KanbanTaskInfo(worker_id, worker_key, "done", TODO_ID)
        results[worker_id] = {
            "runs": [
                {
                    "status": "succeeded",
                    "metadata": {"tpo_result": _worker_result(number, parent, head)},
                }
            ]
        }
        assert len(json.dumps(results[worker_id]["runs"][0]["metadata"])) < 64 * 1024
        assert reconcile_plan_task_results(
            project_dir=repo,
            state_dir=state,
            tenant="stress",
            tick_id=TICK_ID,
        )
        cards[gate_key] = KanbanTaskInfo(gate_id, gate_key, "done", TODO_ID)
        parent = head

    assert completed_gates == {f"gate-{number}" for number in range(1, 51)}
    assert reconcile_plan_task_results(
        project_dir=repo, state_dir=state, tenant="stress", tick_id=TICK_ID
    )
    assert completed_gates == {f"gate-{number}" for number in range(1, 51)}
