"""Tests for hermes_pipeline.kanban_tasks — kanban task registration."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


class FakeGatePhase:
    def __init__(
        self,
        phase_key,
        name="P",
        prompt="",
        tools="",
        turns=0,
        gate=False,
        timeout=1800,
    ):
        self.phase_key = phase_key
        self.name = name
        self.prompt = prompt
        self.tools = tools
        self.turns = turns
        self.gate = gate
        self.timeout = timeout


@pytest.mark.parametrize(
    "stdout",
    [
        '{"id": "--help"}',
        '{"id": "task-001"}',
        "Created --help",
        "Created task-001",
    ],
)
def test_parse_task_id_rejects_values_outside_hermes_id_contract(stdout):
    from hermes_pipeline.kanban_tasks import _parse_task_id

    assert _parse_task_id(stdout) is None


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('{"id": "t_0123abcd"}', "t_0123abcd"),
        ("Created t_deadbeef (ready, assignee=-)", "t_deadbeef"),
    ],
)
def test_parse_task_id_accepts_current_and_legacy_hermes_output(stdout, expected):
    from hermes_pipeline.kanban_tasks import _parse_task_id

    assert _parse_task_id(stdout) == expected


def test_reconcile_pending_create_waits_for_late_visible_task(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import (
        PendingTaskCreate,
        _persist_pending_task_create,
        reconcile_pending_task_create,
    )

    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    _persist_pending_task_create(
        tmp_path,
        PendingTaskCreate("demo", "01CLIENT", "phase_1", ()),
    )
    find = mocker.patch(
        "hermes_pipeline.kanban_tasks._find_task_id_in_snapshot",
        side_effect=[None, "t_deadbeef"],
    )
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks", return_value=True
    )

    assert reconcile_pending_task_create(tmp_path) is False
    assert marker.exists()
    assert reconcile_pending_task_create(tmp_path) is True
    archive.assert_called_once_with(["t_deadbeef"], tenant="demo")
    assert find.call_count == 2
    assert not marker.exists()


def test_reconcile_pending_create_without_marker_allows_tick(tmp_path):
    from hermes_pipeline.kanban_tasks import reconcile_pending_task_create

    assert reconcile_pending_task_create(tmp_path) is True


def test_persist_pending_create_delegates_to_shared_atomic_writer(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import (
        PendingTaskCreate,
        _persist_pending_task_create,
    )

    atomic_write = mocker.patch("hermes_pipeline.kanban_tasks._atomic_write_text")
    _persist_pending_task_create(
        tmp_path,
        PendingTaskCreate("demo", "01CLIENT", "phase_1", ("t_00000001",)),
    )

    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    atomic_write.assert_called_once()
    assert atomic_write.call_args.args[0] == marker
    assert json.loads(atomic_write.call_args.args[1]) == {
        "tenant": "demo",
        "tick_id": "01CLIENT",
        "phase_key": "phase_1",
        "known_task_ids": ["t_00000001"],
    }


def test_reconcile_pending_create_retains_marker_when_archive_fails(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import (
        PendingTaskCreate,
        _persist_pending_task_create,
        reconcile_pending_task_create,
    )

    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    _persist_pending_task_create(
        tmp_path,
        PendingTaskCreate("demo", "01CLIENT", "phase_1", ()),
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._find_task_id_in_snapshot",
        return_value="t_deadbeef",
    )
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks", return_value=False
    )

    assert reconcile_pending_task_create(tmp_path) is False
    assert marker.exists()
    archive.assert_called_once_with(["t_deadbeef"], tenant="demo")


def test_reconcile_pending_create_leaves_malformed_marker(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import reconcile_pending_task_create

    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("not valid JSON")
    find = mocker.patch("hermes_pipeline.kanban_tasks._find_task_id_in_snapshot")

    assert reconcile_pending_task_create(tmp_path) is False
    assert marker.exists()
    find.assert_not_called()


def test_prepare_todo_phases_renders_all_without_external_calls(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
        "    prompt: 'Use {skill_prefix}review.'\n"
        "    tools: Read\n"
        "    turns: 5\n"
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    prepared = prepare_todo_phases(
        todo_id="TODO-41",
        tick_id="01CLIENT",
        board_slug="demo",
        phases_path=phases_path,
        prompt_client="codex",
    )
    run.assert_not_called()
    assert len(prepared) == 1
    assert "selected external client (Codex)" in prepared[0].body
    assert "Use $review." in prepared[0].body


@pytest.mark.parametrize(
    ("prompt_client", "command", "forbidden"),
    [
        ("codex", "codex exec --sandbox danger-full-access", "claude -p"),
        (
            "claude",
            "claude -p --permission-mode bypassPermissions",
            "codex exec",
        ),
    ],
)
def test_prepare_todo_phases_wraps_executable_phases_with_client_delegation(
    tmp_path, mocker, prompt_client, command, forbidden
):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
        "    prompt: 'Use {skill_prefix}review.'\n"
        "    tools: Read,Bash\n"
        "    turns: 5\n"
        "    timeout: 2400\n"
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")

    prepared = prepare_todo_phases(
        todo_id="TODO-41",
        tick_id="01CLIENT",
        board_slug="demo",
        phases_path=phases_path,
        prompt_client=prompt_client,
    )

    run.assert_not_called()
    assert "You are the Hermes dispatcher" in prepared[0].body
    assert "Build the external-agent prompt" in prepared[0].body
    assert "pass only that prompt to the external client" in prepared[0].body
    assert command in prepared[0].body
    assert forbidden not in prepared[0].body
    assert "Do not implement this phase directly with Hermes tools" in prepared[0].body
    assert "external_agent_command" in prepared[0].body
    assert prepared[0].timeout == 2400
    assert "External agent timeout: 2400 seconds" in prepared[0].body
    assert "tracked background execution" in prepared[0].body
    assert "monitor the background process" in prepared[0].body
    assert "60-second cleanup grace" in prepared[0].body
    assert "terminate the external process tree" in prepared[0].body
    assert "confirm that it is no longer running" in prepared[0].body
    assert "external_agent_timeout_seconds" in prepared[0].body
    assert "external_agent_exit_code" in prepared[0].body
    assert "kanban_comment" in prepared[0].body
    assert 'kanban_block(kind="needs_input"' in prepared[0].body
    assert "must not inspect partial changes" in prepared[0].body
    assert "must not implement or commit the phase yourself" in prepared[0].body


def test_prepare_todo_phases_wraps_rendered_prompt_for_external_agent(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
        "    prompt: 'Use {skill_prefix}review.'\n"
        "    tools: Read,Bash\n"
        "    turns: 5\n"
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")

    prepared = prepare_todo_phases(
        todo_id="TODO-41",
        tick_id="01CLIENT",
        board_slug="demo",
        phases_path=phases_path,
        prompt_client="codex",
    )

    run.assert_not_called()
    body = prepared[0].body
    assert "Hermes phase instructions:" not in body
    assert body.count("BEGIN EXTERNAL AGENT PROMPT") == 1
    assert body.count("END EXTERNAL AGENT PROMPT") == 1
    assert (
        "BEGIN EXTERNAL AGENT PROMPT\n"
        "Pipeline context:\n"
        "- todo_id: TODO-41\n"
        "- tick_id: 01CLIENT\n"
        "- project_slug: demo\n"
        "Work on TODO-41 ONLY. Do not pick a different TODO.\n\n"
        "Use $review.\n"
        "END EXTERNAL AGENT PROMPT"
    ) in body


def test_prepare_todo_phases_does_not_wrap_gate_phase_with_client_delegation(
    tmp_path,
):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: gate\n"
        "    name: Gate\n"
        "    gate: true\n"
    )

    prepared = prepare_todo_phases(
        todo_id="TODO-41",
        tick_id="01CLIENT",
        board_slug="demo",
        phases_path=phases_path,
        prompt_client="codex",
    )

    assert "You are the Hermes dispatcher" not in prepared[0].body
    assert "codex exec" not in prepared[0].body
    assert "BEGIN EXTERNAL AGENT PROMPT" not in prepared[0].body
    assert prepared[0].timeout == 1800
    assert "External agent timeout" not in prepared[0].body
    assert "tracked background execution" not in prepared[0].body


def test_prepare_todo_phases_rejects_invalid_todo_before_loading_phases(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases

    load_phases = mocker.patch("hermes_pipeline.kanban_tasks.load_phases")
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")

    with pytest.raises(ValueError, match=r"invalid todo_id format"):
        prepare_todo_phases(
            todo_id="not-a-todo",
            tick_id="01CLIENT",
            board_slug="demo",
            phases_path=tmp_path / "missing.yaml",
        )

    load_phases.assert_not_called()
    run.assert_not_called()


def test_late_render_failure_creates_zero_tasks(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    from hermes_pipeline.phases import PhasePromptRenderError

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
        "    prompt: valid\n"
        "    tools: Read\n"
        "    turns: 5\n"
        "  - phase_key: phase_2\n"
        "    name: Two\n"
        "    prompt: '{unknown}'\n"
        "    tools: Read\n"
        "    turns: 5\n"
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    with pytest.raises(PhasePromptRenderError, match=r"phase_2.*unknown"):
        prepare_todo_phases(
            todo_id="TODO-41",
            tick_id="01CLIENT",
            board_slug="demo",
            phases_path=phases_path,
        )
    run.assert_not_called()


def test_register_todo_phases_late_render_failure_is_atomic(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import register_todo_phases
    from hermes_pipeline.phases import PhasePromptRenderError

    phases_path = tmp_path / "phases.yaml"
    phases_path.write_text(
        "phases:\n"
        "  - phase_key: phase_1\n"
        "    name: One\n"
        "    prompt: valid\n"
        "    tools: Read\n"
        "    turns: 5\n"
        "  - phase_key: phase_2\n"
        "    name: Two\n"
        "    prompt: '{unknown}'\n"
        "    tools: Read\n"
        "    turns: 5\n"
    )
    create_prepared = mocker.patch(
        "hermes_pipeline.kanban_tasks.create_prepared_todo_phases"
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")

    with pytest.raises(PhasePromptRenderError, match=r"phase_2.*unknown"):
        register_todo_phases(
            todo_id="TODO-41",
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
            phases_path=phases_path,
        )

    create_prepared.assert_not_called()
    run.assert_not_called()


def test_create_prepared_todo_phases_preserves_command_chain(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask(
            phase_key="phase_1",
            name="One",
            body="already rendered $body",
            turns=5,
            gate=False,
            timeout=2400,
        ),
        PreparedPhaseTask(
            phase_key="phase_2",
            name="Two",
            body="second body",
            turns=10,
            gate=False,
            timeout=7200,
        ),
    ]
    mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    mock_run.side_effect = [
        mocker.Mock(returncode=0, stdout='{"id": "t_0000000b"}', stderr=""),
        mocker.Mock(returncode=0, stdout='{"id": "t_00000001"}', stderr=""),
        mocker.Mock(returncode=0, stdout='{"id": "t_00000002"}', stderr=""),
        mocker.Mock(returncode=0, stdout="", stderr=""),
    ]

    task_ids = create_prepared_todo_phases(
        prepared=prepared,
        tick_id="01CLIENT",
        board_slug="demo",
        project_dir=tmp_path,
    )

    assert task_ids == ["t_00000001", "t_00000002"]
    create_commands = [
        call.args[0]
        for call in mock_run.call_args_list
        if call.args[0][:3] == ["hermes", "kanban", "create"]
    ]
    assert len(create_commands) == 3
    assert "--parent" not in create_commands[0]
    assert create_commands[1][create_commands[1].index("--parent") + 1] == (
        "t_0000000b"
    )
    assert create_commands[2][create_commands[2].index("--parent") + 1] == (
        "t_00000001"
    )
    assert "already rendered $body" in create_commands[1]
    assert "--max-runtime" not in create_commands[0]
    assert "--max-retries" not in create_commands[0]
    assert create_commands[1][create_commands[1].index("--max-runtime") + 1] == "2460"
    assert create_commands[1][create_commands[1].index("--max-retries") + 1] == "1"
    assert create_commands[2][create_commands[2].index("--max-runtime") + 1] == "7260"
    assert create_commands[2][create_commands[2].index("--max-retries") + 1] == "1"


def test_durable_create_failure_does_not_expose_hermes_stderr(
    tmp_path, mocker, caplog
):
    from hermes_pipeline.kanban_tasks import (
        PendingTaskCreate,
        _run_durable_task_create,
    )

    secret = "Authorization: Bearer provider-secret"
    mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        return_value=mocker.Mock(returncode=1, stdout="", stderr=secret),
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._recover_and_archive_uncertain_task",
        return_value=True,
    )

    with pytest.raises(RuntimeError) as exc_info:
        _run_durable_task_create(
            cmd=["hermes", "kanban", "create"],
            project_dir=tmp_path,
            pending=PendingTaskCreate("demo", "01CLIENT", "phase_1", ()),
        )

    assert secret not in str(exc_info.value)
    assert secret not in caplog.text
    assert "rc=1" in str(exc_info.value)


def test_create_prepared_blocks_until_registered_and_preserves_activation_order(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False),
        PreparedPhaseTask("phase_2", "Two", "body", 10, False),
    ]
    events: list[str] = []
    create_commands: list[list[str]] = []

    mocker.patch(
        "hermes_pipeline.kanban_tasks._persist_pending_task_create",
        side_effect=lambda _project_dir, pending: events.append(
            f"pending:{pending.phase_key}"
        ),
    )
    def run_create(cmd, **_kwargs):
        if cmd[:3] == ["hermes", "kanban", "complete"]:
            events.append(f"complete:{cmd[-1]}")
            return mocker.Mock(returncode=0, stdout="", stderr="")
        phase_key = cmd[cmd.index("--idempotency-key") + 1].split(":", 1)[1]
        create_commands.append(cmd)
        events.append(f"create:{phase_key}")
        task_id = f"t_{len(create_commands):08x}"
        return mocker.Mock(
            returncode=0,
            stdout=json.dumps({"id": task_id}),
            stderr="",
        )

    mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        side_effect=run_create,
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._persist_expected_phases",
        side_effect=lambda *_args, **_kwargs: events.append("persist-expected"),
    )
    assert create_prepared_todo_phases(
        prepared=prepared,
        tick_id="01CLIENT",
        board_slug="demo",
        project_dir=tmp_path,
    ) == ["t_00000002", "t_00000003"]

    assert events == [
        "pending:__registration_barrier__",
        "create:__registration_barrier__",
        "pending:phase_1",
        "create:phase_1",
        "pending:phase_2",
        "create:phase_2",
        "persist-expected",
        "complete:t_00000001",
    ]
    assert all("--initial-status" not in command for command in create_commands)
    assert "--parent" not in create_commands[0]
    assert "--goal" in create_commands[1]
    assert create_commands[1][create_commands[1].index("--parent") + 1] == (
        "t_00000001"
    )
    assert create_commands[2][create_commands[2].index("--parent") + 1] == (
        "t_00000002"
    )


def test_pending_marker_clear_only_removes_matching_create(tmp_path):
    import hermes_pipeline.kanban_tasks as kanban_tasks

    pending = kanban_tasks.PendingTaskCreate(
        "demo", "01CLIENT", "phase_1", ("t_00000001",)
    )
    kanban_tasks._persist_pending_task_create(tmp_path, pending)
    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    clear_pending = getattr(kanban_tasks, "_clear_pending_task_create", None)

    assert clear_pending is not None
    assert not clear_pending(
        tmp_path,
        kanban_tasks.PendingTaskCreate(
            "demo", "01CLIENT", "phase_2", ("t_00000001",)
        ),
    )
    assert marker.exists()
    assert clear_pending(tmp_path, pending)
    assert not marker.exists()


def test_uncertain_create_timeout_retains_child_and_known_parents(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        PendingTaskCreate,
        _run_durable_task_create,
    )

    pending = PendingTaskCreate(
        "demo",
        "01CLIENT",
        "phase_2",
        ("t_0000000b", "t_00000001"),
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=60),
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=60),
        mocker.Mock(returncode=0, stdout="[]", stderr=""),
    ]
    archive = mocker.patch("hermes_pipeline.kanban_tasks._archive_tasks")

    with pytest.raises(RuntimeError, match=r"phase_2.*Hermes process"):
        _run_durable_task_create(
            cmd=[
                "hermes",
                "kanban",
                "create",
                "--idempotency-key",
                "01CLIENT:phase_2",
            ],
            project_dir=tmp_path,
            pending=pending,
        )

    archive.assert_not_called()
    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "tenant": "demo",
        "tick_id": "01CLIENT",
        "phase_key": "phase_2",
        "known_task_ids": ["t_0000000b", "t_00000001"],
    }


def test_timeout_recovery_persists_child_first_cleanup(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import (
        PendingTaskCreate,
        _run_durable_task_create,
    )

    pending = PendingTaskCreate(
        "demo",
        "01CLIENT",
        "phase_2",
        ("t_0000000b", "t_00000001"),
    )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=60),
        mocker.Mock(
            returncode=0,
            stdout='{"id": "t_00000002"}',
            stderr="",
        ),
    ]
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks",
        return_value=False,
    )

    with pytest.raises(RuntimeError, match=r"phase_2.*Hermes process"):
        _run_durable_task_create(
            cmd=[
                "hermes",
                "kanban",
                "create",
                "--idempotency-key",
                "01CLIENT:phase_2",
            ],
            project_dir=tmp_path,
            pending=pending,
        )

    archive.assert_called_once_with(
        ["t_00000002", "t_00000001", "t_0000000b"],
        tenant="demo",
    )
    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "tenant": "demo",
        "tick_id": "01CLIENT",
        "cleanup_task_ids": [
            "t_00000002",
            "t_00000001",
            "t_0000000b",
        ],
    }


def test_sentinel_failure_retains_child_first_cleanup(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    ids_by_key = {
        "__registration_barrier__": "t_0000000b",
        "phase_1": "t_00000001",
    }

    def run(cmd, **_kwargs):
        key = cmd[cmd.index("--idempotency-key") + 1].split(":", 1)[1]
        return mocker.Mock(
            returncode=0,
            stdout=json.dumps({"id": ids_by_key[key]}),
            stderr="",
        )

    mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run", side_effect=run)
    mocker.patch(
        "hermes_pipeline.kanban_tasks._persist_expected_phases",
        side_effect=RuntimeError("sentinel write failed"),
    )
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks",
        return_value=False,
    )

    with pytest.raises(RuntimeError, match="cleanup remains pending"):
        create_prepared_todo_phases(
            prepared=[
                PreparedPhaseTask("phase_1", "One", "body", 5, False),
            ],
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "tenant": "demo",
        "tick_id": "01CLIENT",
        "cleanup_task_ids": ["t_00000001", "t_0000000b"],
    }
    archive.assert_called_once_with(
        ["t_00000001", "t_0000000b"],
        tenant="demo",
    )


def test_registration_barrier_complete_invokes_hermes(mocker):
    import hermes_pipeline.kanban_tasks as kanban_tasks

    run = mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout="", stderr=""),
    )
    kanban_tasks._complete_registration_barrier("t_0000000b")

    run.assert_called_once_with(
        ["hermes", "kanban", "complete", "t_0000000b"],
        capture_output=True,
        text=True,
        timeout=kanban_tasks.HERMES_COMMAND_TIMEOUT,
        check=False,
    )


def test_activation_order_expected_phase_write_failure_is_reported(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        _persist_expected_phases,
    )

    mocker.patch(
        "hermes_pipeline.kanban_tasks._atomic_write_text",
        side_effect=OSError("disk full"),
    )

    with pytest.raises(RuntimeError, match="expected phases"):
        _persist_expected_phases(
            [PreparedPhaseTask("phase_1", "One", "body", 5, False)],
            project_dir=tmp_path,
        )


@pytest.mark.parametrize(
    "recovery_failure",
    [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        OSError("hermes unavailable"),
    ],
)
def test_create_prepared_resolves_task_from_snapshot_when_recovery_is_uncertain(
    mocker, recovery_failure
):
    from hermes_pipeline.kanban_tasks import _recover_uncertain_task_id

    body = '{"phase_key": "phase_1", "tick_id": "01CLIENT"}\nbody'
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        recovery_failure,
        mocker.Mock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "id": "t_deadbeef",
                        "body": body,
                        "status": "ready",
                    }
                ]
            ),
            stderr="",
        ),
    ]

    cmd = ["hermes", "kanban", "create", "--idempotency-key", "01CLIENT:phase_1"]
    assert (
        _recover_uncertain_task_id(
            cmd,
            tenant="demo",
            tick_id="01CLIENT",
            phase_key="phase_1",
        )
        == "t_deadbeef"
    )

    assert run.call_args_list[1].args[0] == [
        "hermes",
        "kanban",
        "list",
        "--tenant",
        "demo",
        "--archived",
        "--json",
    ]


@pytest.mark.parametrize(
    "snapshot_stdout",
    [
        "null",
        '"unexpected"',
        "42",
        '{"tasks": null}',
        '{"tasks": "unexpected"}',
        "[null]",
        '[{"body": null}]',
        json.dumps([{"id": "t_deadbeef", "body": "null\nbody"}]),
        json.dumps([{"id": "t_deadbeef", "body": "[]\nbody"}]),
        json.dumps([{"id": "t_deadbeef", "body": '"unexpected"\nbody'}]),
    ],
)
def test_find_task_rejects_malformed_snapshot(mocker, snapshot_stdout):
    from hermes_pipeline.kanban_tasks import _find_task_id_in_snapshot

    mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        return_value=mocker.Mock(
            returncode=0,
            stdout=snapshot_stdout,
            stderr="",
        ),
    )

    assert (
        _find_task_id_in_snapshot(
            tenant="demo",
            tick_id="01CLIENT",
            phase_key="phase_2",
        )
        is None
    )


def test_find_task_rejects_mixed_valid_and_invalid_snapshot(mocker):
    """Recovery must not trust a matching task from a malformed snapshot."""
    from hermes_pipeline.kanban_tasks import _find_task_id_in_snapshot

    matching_task = {
        "id": "t_deadbeef",
        "body": json.dumps(
            {"tick_id": "01CLIENT", "phase_key": "phase_2"}
        ),
    }
    mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        return_value=mocker.Mock(
            returncode=0,
            stdout=json.dumps([matching_task, None]),
            stderr="",
        ),
    )

    assert (
        _find_task_id_in_snapshot(
            tenant="demo",
            tick_id="01CLIENT",
            phase_key="phase_2",
        )
        is None
    )


class TestRegisterTodoPhases:
    """Tests for register_todo_phases()."""

    def test_creates_tasks_with_parent_chain(self, tmp_path, mocker):
        """Phases are registered as kanban tasks with --parent deps."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(
            returncode=0, stdout=json.dumps({"id": "t_00000001"})
        )

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Do the plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
            '  - phase_key: "phase_4_development"\n'
            '    name: "Phase 4: Development"\n'
            '    prompt: "Implement"\n'
            '    tools: "Read,Write,Edit,Bash"\n'
            "    turns: 60\n"
            "    timeout: 3600\n"
        )

        register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        create_commands = [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "create"]
        ]
        assert len(create_commands) == 3
        assert "--parent" not in create_commands[0]
        assert create_commands[1][create_commands[1].index("--parent") + 1] == (
            "t_00000001"
        )
        assert create_commands[2][create_commands[2].index("--parent") + 1] == (
            "t_00000001"
        )

    def test_task_body_has_json_header(self, tmp_path, mocker):
        """Task body starts with a JSON header line containing tick_id, phase_key, todo_id."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(
            returncode=0, stdout=json.dumps({"id": "t_00000001"})
        )

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Do the plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
        )

        register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        create_commands = [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "create"]
        ]
        call_args = create_commands[1]
        body_idx = call_args.index("--body")
        body_value = call_args[body_idx + 1]

        first_line = body_value.split("\n")[0]
        header = json.loads(first_line)

        assert header["tick_id"] == "01HA6PH2V0ZJ7GK0S39D243TQX"
        assert header["phase_key"] == "phase_2_autoplan"
        assert header["todo_id"] == "TODO-10"
        assert header["project_slug"] == "demo"

    def test_idempotency_key_format(self, tmp_path, mocker):
        """Idempotency key is <tick_id>:<phase_key>."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(
            returncode=0, stdout=json.dumps({"id": "t_00000001"})
        )

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Do the plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
        )

        register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        create_commands = [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "create"]
        ]
        call_args = create_commands[1]
        key_idx = call_args.index("--idempotency-key")
        key_value = call_args[key_idx + 1]

        assert key_value == "01HA6PH2V0ZJ7GK0S39D243TQX:phase_2_autoplan"

    def test_returns_task_ids(self, tmp_path, mocker):
        """register_todo_phases returns a list of created task IDs."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            mocker.MagicMock(
                returncode=0, stdout=json.dumps({"id": "t_0000000b"})
            ),
            mocker.MagicMock(
                returncode=0, stdout=json.dumps({"id": "t_00000001"})
            ),
            mocker.MagicMock(
                returncode=0, stdout=json.dumps({"id": "t_00000002"})
            ),
            mocker.MagicMock(returncode=0, stdout="", stderr=""),
        ]

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
            '  - phase_key: "phase_4_development"\n'
            '    name: "Phase 4: Dev"\n'
            '    prompt: "Dev"\n'
            '    tools: "Read,Write,Edit,Bash"\n'
            "    turns: 60\n"
            "    timeout: 3600\n"
        )

        task_ids = register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        assert task_ids == ["t_00000001", "t_00000002"]

    def test_gate_phase_registered_with_sticky_block_without_goal(
        self, tmp_path, mocker
    ):
        """Gate phases are nonspawnable and receive an explicit sticky block."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        phases = [
            FakeGatePhase("phase_8_finish_branch", name="P8", turns=15),
            FakeGatePhase("phase_9_ship", name="Ship Gate", gate=True),
        ]
        mocker.patch("hermes_pipeline.kanban_tasks.load_phases", return_value=phases)
        mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout='{"id": "t_0000000a"}', stderr="")

        register_todo_phases(
            todo_id="TODO-5",
            tick_id="01TICK",
            board_slug="demo",
            project_dir=tmp_path,
        )

        create_commands = [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "create"]
        ]
        gate_cmd = create_commands[2]
        assert "--initial-status" not in gate_cmd
        assert "--goal" not in gate_cmd
        assert gate_cmd[gate_cmd.index("--parent") + 1] == "t_0000000a"
        assert gate_cmd[gate_cmd.index("--assignee") + 1] == "-"

        block_commands = [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "block"]
        ]
        assert block_commands == [
            [
                "hermes",
                "kanban",
                "block",
                "--kind",
                "needs_input",
                "t_0000000a",
            ]
        ]

        phase8_cmd = create_commands[1]
        assert "--goal" in phase8_cmd
        assert "--initial-status" not in phase8_cmd

    def test_gate_phase_is_not_assigned_to_pipeline_worker(self, tmp_path, mocker):
        """Gate phases are human checkpoints and must not be worker-dispatchable."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        phases = [
            FakeGatePhase("phase_8_finish_branch", name="P8", turns=15),
            FakeGatePhase("phase_9_ship", name="Ship Gate", gate=True),
        ]
        mocker.patch("hermes_pipeline.kanban_tasks.load_phases", return_value=phases)
        mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout='{"id": "t_0000000a"}', stderr="")

        register_todo_phases(
            todo_id="TODO-5",
            tick_id="01TICK",
            board_slug="demo",
            project_dir=tmp_path,
            assignee="pipeline",
        )

        create_commands = [
            call.args[0]
            for call in mock_run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "create"]
        ]
        phase8_cmd = create_commands[1]
        gate_cmd = create_commands[2]
        assert phase8_cmd[phase8_cmd.index("--assignee") + 1] == "pipeline"
        assert gate_cmd[gate_cmd.index("--assignee") + 1] == "-"


class TestAllPhasesComplete:
    """Tests for all_phases_complete() and get_todo_kanban_status()."""

    def test_all_done_is_complete(self, mocker):
        """All tasks done -> all_phases_complete returns True."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = [
            {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is True

    def test_running_task_not_complete(self, mocker):
        """At least one running task -> not complete."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = [
            {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "running", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is False

    def test_no_tasks_for_tick(self, mocker):
        """No tasks for the tick -> False (nothing to complete)."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = []

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is False

    def test_no_tasks_with_picked_none_sentinel(self, mocker, tmp_path):
        """No tasks + picked=None sentinel + state_dir -> True (tick done)."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = []
        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        # Create the picked=None sentinel
        outcomes_dir = tmp_path / "outcomes"
        outcomes_dir.mkdir()
        sentinel = outcomes_dir / "01HA-phases.json"
        sentinel.write_text('{"outcome": "picked_none"}\n')

        assert all_phases_complete("demo", "01HA", state_dir=str(tmp_path)) is True

    def test_failed_task_is_terminal(self, mocker):
        """A failed task is terminal — all tasks terminal -> True."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = [
            {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "failed", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is True

    def test_cli_failure_returns_false(self, mocker):
        """Kanban CLI failure -> False (conservative: don't release lock)."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mocker.patch("subprocess.run", side_effect=FileNotFoundError)

        assert all_phases_complete("demo", "01HA") is False

    def test_archived_task_not_complete(self, mocker):
        """Archived tasks (mid-registration cleanup) are not completion status."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = [
            {"status": "archived", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "archived", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is False

    def test_all_phases_complete_reads_expected_from_state_dir(self, tmp_path, mocker):
        """expected-phases.json should be read from state_dir, not .hermes/."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        state_dir = tmp_path / "myproject" / ".hermes"
        state_dir.mkdir(parents=True)
        outcomes_dir = state_dir / "outcomes"
        outcomes_dir.mkdir()

        expected_file = outcomes_dir / "expected-phases.json"
        expected_file.write_text(json.dumps(["P1_research", "P2_implementation"]))

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([])  # No kanban CLI call needed; patch instead
        mocker.patch("subprocess.run", return_value=mock_result)

        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"P1_research": "done", "P2_implementation": "done"},
        )
        result = all_phases_complete(
            tenant="myproject",
            tick_id="abc123",
            state_dir=state_dir,
        )
        assert result is True

    def test_all_phases_complete_partial_reg_from_state_dir(self, tmp_path, mocker):
        """Missing phase in status map should be detected using state_dir sentinel."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        state_dir = tmp_path / "myproject" / ".hermes"
        state_dir.mkdir(parents=True)
        outcomes_dir = state_dir / "outcomes"
        outcomes_dir.mkdir()

        expected_file = outcomes_dir / "expected-phases.json"
        expected_file.write_text(json.dumps(["P1_research", "P2_implementation", "P3_review"]))

        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"P1_research": "done", "P2_implementation": "done"},
        )
        result = all_phases_complete(
            tenant="myproject",
            tick_id="abc123",
            state_dir=state_dir,
        )
        assert result is False  # Partial registration detected


class TestGetTodoKanbanStatus:
    """Tests for get_todo_kanban_status()."""

    def test_returns_status_map(self, mocker):
        """Returns {phase_key: status} for the tick."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        mock_data = [
            {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "running", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "ready", "body": '{"tick_id":"01HA","phase_key":"phase_6_1_cso","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            # Different tick — should be filtered out
            {"status": "done", "body": '{"tick_id":"01H9","phase_key":"phase_2_autoplan","todo_id":"TODO-9","project_slug":"demo"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = get_todo_kanban_status("demo", "01HA")
        assert result == {
            "phase_2_autoplan": "done",
            "phase_4_development": "running",
            "phase_6_1_cso": "ready",
        }

    def test_returns_empty_for_no_matching_tick(self, mocker):
        """No tasks for the tick -> empty map."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        mock_data = []

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = get_todo_kanban_status("demo", "01HA")
        assert result == {}


class TestPersistExpectedPhases:
    """Tests for _persist_expected_phases()."""

    def test_writes_to_project_hermes_dir(self, tmp_path: Path):
        """_persist_expected_phases writes to project_dir/.hermes/outcomes/."""
        from hermes_pipeline.kanban_tasks import _persist_expected_phases

        class FakePhase:
            def __init__(self, key):
                self.phase_key = key
                self.name = key
                self.prompt = ""
                self.turns = 1

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()

        phases = [FakePhase("P1_research"), FakePhase("P2_implementation")]

        _persist_expected_phases(phases, project_dir=project_dir)

        expected = project_dir / ".hermes" / "outcomes" / "expected-phases.json"
        assert expected.exists()
        data = json.loads(expected.read_text())
        assert data == ["P1_research", "P2_implementation"]

    def test_backward_compat_defaults_to_dot_hermes(self, tmp_path: Path, monkeypatch):
        """Without project_dir, falls back to .hermes/outcomes/ (cwd-relative)."""
        from hermes_pipeline.kanban_tasks import _persist_expected_phases

        class FakePhase:
            def __init__(self, key):
                self.phase_key = key
                self.name = key
                self.prompt = ""
                self.turns = 1

        monkeypatch.chdir(tmp_path)

        phases = [FakePhase("P1")]
        _persist_expected_phases(phases)

        expected = tmp_path / ".hermes" / "outcomes" / "expected-phases.json"
        assert expected.exists()
        data = json.loads(expected.read_text())
        assert data == ["P1"]

        # Cleanup: remove the sentinel so it doesn't pollute other tests
        import shutil
        shutil.rmtree(tmp_path / ".hermes")


class TestObserveOutcomes:
    """Tests for observe_outcomes() — kanban -> decision store sync."""

    def test_writes_phase_complete_outcomes(self, state_dir):
        """Done phases get phase_complete written to JSONL."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "done",
            "phase_4_development": "done",
            "phase_6_1_cso": "done",
        }

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]
        outcomes = [json.loads(l) for l in lines]

        # Should have 3 phase_complete + 1 all_phases_complete
        assert len(outcomes) == 4

        phase_completes = [o for o in outcomes if o["outcome"] == "phase_complete"]
        assert len(phase_completes) == 3

        all_complete = [o for o in outcomes if o["outcome"] == "all_phases_complete"]
        assert len(all_complete) == 1

    def test_writes_failed_outcome(self, state_dir):
        """Failed phase gets failed_at_phase_* written."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "done",
            "phase_4_development": "failed",
            "phase_6_1_cso": "ready",  # Blocked by parent
        }

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]
        outcomes = [json.loads(l) for l in lines]

        phase_completes = [o for o in outcomes if o["outcome"] == "phase_complete"]
        assert len(phase_completes) == 1  # Only phase_2_autoplan

        failed = [o for o in outcomes if o["outcome"] == "failed_at_phase_phase_4_development"]
        assert len(failed) == 1

        # No all_phases_complete because phase_6_1_cso is still ready (non-terminal)

    def test_creates_outcomes_dir(self, state_dir):
        """Outcomes directory is created if it doesn't exist."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {"phase_2_autoplan": "done"}

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        assert phases_file.exists()

    def test_high_watermark_no_duplicate(self, state_dir):
        """Phase outcomes already in file are not duplicated on re-observe."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "done",
            "phase_4_development": "done",
        }

        # First observe
        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        # Second observe with same status_map
        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]

        # Should still be 3 (2 phase_complete + 1 all_phases_complete), not 6
        assert len(lines) == 3

    def test_archived_phases_write_failed_outcome(self, state_dir):
        """Archived phases (mid-registration cleanup) write failed_at_phase_*."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "archived",
            "phase_4_development": "archived",
        }

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]
        outcomes = [json.loads(l) for l in lines]

        failed = [o for o in outcomes if o["outcome"].startswith("failed_at_phase_")]
        assert len(failed) == 2

        # No all_phases_complete because archived is not a completion status
        all_complete = [o for o in outcomes if o["outcome"] == "all_phases_complete"]
        assert len(all_complete) == 0

    def test_phase_complete_written_in_flight_skipped(self, state_dir):
        """Done phases written; running/ready phases skipped (not written as outcomes)."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "done",
            "phase_4_development": "running",
            "phase_6_1_cso": "ready",
        }

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        lines = [l for l in phases_file.read_text().strip().split("\n") if l.strip()]
        outcomes = [json.loads(l) for l in lines]

        # Only 1 phase_complete (phase_2_autoplan), no all_phases_complete
        phase_completes = [o for o in outcomes if o["outcome"] == "phase_complete"]
        assert len(phase_completes) == 1
        assert phase_completes[0]["phase_key"] == "phase_2_autoplan"

        all_complete = [o for o in outcomes if o["outcome"] == "all_phases_complete"]
        assert len(all_complete) == 0

    def test_skips_in_flight_phases(self, state_dir):
        """In-flight phases (running, ready) are skipped."""
        from hermes_pipeline.kanban_tasks import observe_outcomes

        status_map = {
            "phase_2_autoplan": "running",
            "phase_4_development": "ready",
        }

        observe_outcomes(
            state_dir=state_dir,
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            status_map=status_map,
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        # No outcomes should be written for in-flight phases
        content = phases_file.read_text().strip() if phases_file.exists() else ""
        assert content == ""

    def test_json_parse_error_in_kanban_create(self, tmp_path, mocker):
        """If kanban create returns non-JSON, RuntimeError is raised."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(
            returncode=0, stdout="not json", stderr=""
        )

        phases_cfg = tmp_path / "phases.yaml"
        phases_cfg.write_text(
            "phases:\n"
            '  - phase_key: "phase_2_autoplan"\n'
            '    name: "Phase 2: Autoplan"\n'
            '    prompt: "Do the plan"\n'
            '    tools: "Read,Write"\n'
            "    turns: 20\n"
            "    timeout: 1800\n"
        )

        with pytest.raises(RuntimeError, match="failed to parse"):
            register_todo_phases(
                todo_id="TODO-10",
                tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
                board_slug="demo",
                project_dir=str(tmp_path),
                phases_path=str(phases_cfg),
            )

    def test_load_phases_file_not_found(self, tmp_path, mocker):
        """If phases.yaml doesn't exist, the error propagates."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        with pytest.raises(FileNotFoundError):
            register_todo_phases(
                todo_id="TODO-10",
                tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
                board_slug="demo",
                project_dir=str(tmp_path),
                phases_path=str(tmp_path / "nonexistent.yaml"),
            )

    def test_archive_tasks_multiple(self, tmp_path, mocker):
        """_archive_tasks archives multiple tasks."""
        from hermes_pipeline.kanban_tasks import _archive_tasks

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=0, stdout="", stderr="")

        assert _archive_tasks(["t_00000001", "t_00000002", "t_00000003"])

        assert mock_run.call_count == 3
        for i, call in enumerate(mock_run.call_args_list):
            args = call[0][0]
            assert "kanban" in args
            assert "archive" in args

    def test_archive_task_failure_is_best_effort(self, tmp_path, mocker):
        """_archive_tasks continues if one archive fails."""
        from hermes_pipeline.kanban_tasks import _archive_tasks

        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            mocker.MagicMock(returncode=0, stdout="", stderr=""),
            mocker.MagicMock(returncode=1, stdout="", stderr="error"),
            mocker.MagicMock(returncode=0, stdout="", stderr=""),
        ]

        # Should not raise — best-effort
        assert not _archive_tasks(["t_00000001", "t_00000002", "t_00000003"])
        assert mock_run.call_count == 3

    def test_archive_task_exception_returns_failure(self, mocker):
        """_archive_tasks reports an exception after attempting the archive."""
        from hermes_pipeline.kanban_tasks import _archive_tasks

        mock_run = mocker.patch("subprocess.run", side_effect=OSError("unavailable"))

        assert not _archive_tasks(["t_00000001"])
        assert mock_run.call_count == 1

    def test_all_phases_complete_dict_format(self, mocker):
        """all_phases_complete handles dict format {'tasks': [...]}."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = {
            "tasks": [
                {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
                {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is True

    def test_all_phases_complete_mixed_done_ready(self, mocker):
        """Some done, some ready -> not all complete."""
        from hermes_pipeline.kanban_tasks import all_phases_complete

        mock_data = [
            {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "ready", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is False

    def test_get_todo_kanban_status_dict_format(self, mocker):
        """get_todo_kanban_status handles dict format {'tasks': [...]}."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        mock_data = {
            "tasks": [
                {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
                {"status": "running", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            ]
        }

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = get_todo_kanban_status("demo", "01HA")
        assert result == {
            "phase_2_autoplan": "done",
            "phase_4_development": "running",
        }

    def test_get_todo_kanban_status_timeout(self, mocker):
        """get_todo_kanban_status handles subprocess timeout."""
        import subprocess

        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        mocker.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("hermes", 10))

        result = get_todo_kanban_status("demo", "01HA")
        assert result == {}

    def test_get_todo_kanban_status_malformed_header(self, mocker):
        """get_todo_kanban_status skips tasks with malformed JSON header."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        mock_data = [
            {"status": "done", "body": "No JSON header — just text"},
            {"status": "running", "body": '{"tick_id":"01HA","phase_key":"phase_4_dev"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        result = get_todo_kanban_status("demo", "01HA")
        assert result == {"phase_4_dev": "running"}

    @pytest.mark.parametrize(
        "snapshot",
        (
            None,
            42,
            {"tasks": None},
            [None],
            [{"body": None, "status": "done"}],
            [{"body": "null\nbody", "status": "done"}],
        ),
    )
    def test_get_todo_kanban_status_returns_empty_for_malformed_shapes(
        self, mocker, snapshot
    ):
        """Malformed snapshots, task entries, and headers are conservatively empty."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        mocker.patch(
            "subprocess.run",
            return_value=mocker.Mock(
                returncode=0,
                stdout=json.dumps(snapshot),
                stderr="",
            ),
        )

        assert get_todo_kanban_status("demo", "01HA") == {}

    def test_get_todo_kanban_status_rejects_mixed_valid_and_invalid_snapshot(
        self, mocker
    ):
        """A malformed entry invalidates the whole status snapshot."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        valid_task = {
            "status": "done",
            "body": json.dumps(
                {
                    "tick_id": "01HA",
                    "phase_key": "phase_2_autoplan",
                    "todo_id": "TODO-10",
                    "project_slug": "demo",
                }
            ),
        }
        mocker.patch(
            "subprocess.run",
            return_value=mocker.Mock(
                returncode=0,
                stdout=json.dumps([valid_task, None]),
                stderr="",
            ),
        )

        assert get_todo_kanban_status("demo", "01HA") == {}


class TestCompleteTodoKanbanTask:
    """Tests for complete_todo_kanban_task()."""

    def test_calls_hermes_kanban_complete(self, mocker):
        from hermes_pipeline.kanban_tasks import complete_todo_kanban_task

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=0, stdout="", stderr="")

        result = complete_todo_kanban_task("demo", "t_00000001")

        assert result is True
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert args == ["hermes", "kanban", "complete", "t_00000001"]

    def test_swallows_nonzero_returncode(self, mocker):
        from hermes_pipeline.kanban_tasks import complete_todo_kanban_task

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=1, stdout="", stderr="boom")

        result = complete_todo_kanban_task("demo", "t_00000001")  # Should not raise

        assert result is False

    def test_swallows_exceptions(self, mocker):
        from hermes_pipeline.kanban_tasks import complete_todo_kanban_task

        mocker.patch("subprocess.run", side_effect=FileNotFoundError)

        result = complete_todo_kanban_task("demo", "t_00000001")  # Should not raise

        assert result is False


class TestGetTodoKanbanTasks:
    """Tests for get_todo_kanban_tasks() — task id + status per phase."""

    def test_get_todo_kanban_tasks_returns_ids_and_status(self, mocker):
        import json as _json
        tasks = [
            {
                "id": "t_8",
                "status": "done",
                "body": _json.dumps(
                    {"tick_id": "01TICK", "phase_key": "phase_8_finish_branch",
                     "todo_id": "TODO-5", "project_slug": "demo"},
                    sort_keys=True,
                ) + "\nbody text",
            },
            {
                "id": "t_9",
                "status": "blocked",
                "body": _json.dumps(
                    {"tick_id": "01TICK", "phase_key": "phase_9_ship",
                     "todo_id": "TODO-5", "project_slug": "demo"},
                    sort_keys=True,
                ) + "\ngate",
            },
            {
                "id": "t_other",
                "status": "done",
                "body": _json.dumps(
                    {"tick_id": "OTHER", "phase_key": "phase_9_ship",
                     "todo_id": "TODO-9", "project_slug": "demo"},
                    sort_keys=True,
                ),
            },
        ]
        mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(tasks), stderr="")

        from hermes_pipeline.kanban_tasks import get_todo_kanban_tasks
        out = get_todo_kanban_tasks("demo", "01TICK")

        assert set(out) == {"phase_8_finish_branch", "phase_9_ship"}
        assert out["phase_9_ship"].task_id == "t_9"
        assert out["phase_9_ship"].status == "blocked"
        assert out["phase_9_ship"].todo_id == "TODO-5"
        assert out["phase_8_finish_branch"].task_id == "t_8"

    def test_get_todo_kanban_tasks_empty_on_cli_failure(self, mocker):
        mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=1, stdout="", stderr="boom")
        from hermes_pipeline.kanban_tasks import get_todo_kanban_tasks
        assert get_todo_kanban_tasks("demo", "01TICK") == {}

    @pytest.mark.parametrize(
        "snapshot",
        (
            None,
            42,
            {"tasks": None},
            [None],
            [{"body": None, "status": "done"}],
            [{"body": "[]\nbody", "status": "done"}],
        ),
    )
    def test_get_todo_kanban_tasks_returns_empty_for_malformed_shapes(
        self, mocker, snapshot
    ):
        """Malformed snapshots, task entries, and headers are conservatively empty."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_tasks

        mocker.patch(
            "hermes_pipeline.kanban_tasks.subprocess.run",
            return_value=mocker.Mock(
                returncode=0,
                stdout=json.dumps(snapshot),
                stderr="",
            ),
        )

        assert get_todo_kanban_tasks("demo", "01TICK") == {}

    def test_get_todo_kanban_tasks_rejects_mixed_valid_and_invalid_snapshot(
        self, mocker
    ):
        """A malformed entry invalidates the whole task-info snapshot."""
        from hermes_pipeline.kanban_tasks import get_todo_kanban_tasks

        valid_task = {
            "id": "t_00000001",
            "status": "done",
            "body": json.dumps(
                {
                    "tick_id": "01TICK",
                    "phase_key": "phase_2_autoplan",
                    "todo_id": "TODO-10",
                    "project_slug": "demo",
                }
            ),
        }
        mocker.patch(
            "hermes_pipeline.kanban_tasks.subprocess.run",
            return_value=mocker.Mock(
                returncode=0,
                stdout=json.dumps([valid_task, None]),
                stderr="",
            ),
        )

        assert get_todo_kanban_tasks("demo", "01TICK") == {}


class TestCancelTodoKanbanTasks:
    def test_reclaims_running_worker_archives_chain_and_confirms_runs(
        self, mocker
    ):
        from hermes_pipeline.kanban_tasks import cancel_todo_kanban_tasks

        running = {
            "id": "t_00000001",
            "status": "running",
            "body": json.dumps(
                {"tick_id": "01CANCEL", "phase_key": "phase_2"}
            ),
        }
        archived = {**running, "status": "archived"}
        mocker.patch(
            "hermes_pipeline.kanban_tasks._list_task_snapshot",
            side_effect=[[running], [archived]],
        )

        def _run(command, **_kwargs):
            if command[:3] == ["hermes", "kanban", "show"]:
                return mocker.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "task": {
                                "id": "t_00000001",
                                "status": "ready",
                                "worker_pid": None,
                                "claim_lock": None,
                            },
                            "parents": [],
                            "runs": [
                                {
                                    "id": 1,
                                    "status": "reclaimed",
                                    "outcome": "reclaimed",
                                    "metadata": {"terminated": True},
                                    "ended_at": 123,
                                }
                            ],
                        }
                    ),
                    stderr="",
                )
            return mocker.Mock(returncode=0, stdout="", stderr="")

        run = mocker.patch(
            "hermes_pipeline.kanban_tasks.subprocess.run",
            side_effect=_run,
        )

        assert cancel_todo_kanban_tasks("demo", "01CANCEL") is True
        commands = [call.args[0] for call in run.call_args_list]
        assert commands == [
            ["hermes", "kanban", "show", "t_00000001", "--json"],
            [
                "hermes",
                "kanban",
                "reclaim",
                "--reason",
                "tpo harness overall timeout",
                "t_00000001",
            ],
            ["hermes", "kanban", "show", "t_00000001", "--json"],
            ["hermes", "kanban", "archive", "t_00000001"],
            ["hermes", "kanban", "show", "t_00000001", "--json"],
        ]

    def test_refuses_cleanup_when_worker_run_is_still_active(self, mocker):
        from hermes_pipeline.kanban_tasks import cancel_todo_kanban_tasks

        running = {
            "id": "t_00000001",
            "status": "running",
            "body": json.dumps(
                {"tick_id": "01CANCEL", "phase_key": "phase_2"}
            ),
        }
        mocker.patch(
            "hermes_pipeline.kanban_tasks._list_task_snapshot",
            return_value=[running],
        )

        def _run(command, **_kwargs):
            if command[:3] == ["hermes", "kanban", "show"]:
                return mocker.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "task": {
                                "id": "t_00000001",
                                "status": "running",
                                "worker_pid": 4321,
                                "claim_lock": "host:claim",
                            },
                            "parents": [],
                            "runs": [
                                {
                                    "id": 1,
                                    "status": "running",
                                    "outcome": None,
                                    "ended_at": None,
                                }
                            ],
                        }
                    ),
                    stderr="",
                )
            return mocker.Mock(returncode=0, stdout="", stderr="")

        run = mocker.patch(
            "hermes_pipeline.kanban_tasks.subprocess.run",
            side_effect=_run,
        )

        assert cancel_todo_kanban_tasks("demo", "01CANCEL") is False
        assert not any(
            call.args[0][:3] == ["hermes", "kanban", "archive"]
            for call in run.call_args_list
        )

    def test_archives_child_first_from_topology_regardless_of_snapshot_order(
        self, mocker
    ):
        from hermes_pipeline.kanban_tasks import cancel_todo_kanban_tasks

        task_ids = ("t_00000001", "t_00000002", "t_00000003")
        tasks = [
            {
                "id": task_id,
                "status": "running",
                "body": json.dumps(
                    {"tick_id": "01CANCEL", "phase_key": phase_key}
                ),
            }
            for task_id, phase_key in zip(task_ids, ("parent", "child", "middle"))
        ]
        mocker.patch(
            "hermes_pipeline.kanban_tasks._list_task_snapshot",
            side_effect=[tasks, [{**task, "status": "archived"} for task in tasks]],
        )
        parents = {
            "t_00000001": [],
            "t_00000002": ["t_00000003"],
            "t_00000003": ["t_00000001"],
        }

        def _run(command, **_kwargs):
            if command[:3] == ["hermes", "kanban", "show"]:
                task_id = command[3]
                return mocker.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "task": {
                                "id": task_id,
                                "worker_pid": None,
                                "claim_lock": None,
                            },
                            "parents": parents[task_id],
                            "runs": [
                                {
                                    "metadata": {"terminated": True},
                                    "ended_at": 123,
                                }
                            ],
                        }
                    ),
                    stderr="",
                )
            return mocker.Mock(returncode=0, stdout="", stderr="")

        run = mocker.patch(
            "hermes_pipeline.kanban_tasks.subprocess.run", side_effect=_run
        )

        assert cancel_todo_kanban_tasks("demo", "01CANCEL") is True
        reclaim_commands = [
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "reclaim"]
        ]
        archive_commands = [
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "archive"]
        ]
        assert [command[-1] for command in reclaim_commands] == [
            "t_00000002",
            "t_00000003",
            "t_00000001",
        ]
        assert archive_commands == [
            ["hermes", "kanban", "archive", "t_00000002"],
            ["hermes", "kanban", "archive", "t_00000003"],
            ["hermes", "kanban", "archive", "t_00000001"],
        ]

    def test_archives_sibling_topology_by_task_id_not_snapshot_order(self, mocker):
        from hermes_pipeline.kanban_tasks import cancel_todo_kanban_tasks

        task_ids = ("t_00000001", "t_00000003", "t_00000002")
        tasks = [
            {
                "id": task_id,
                "status": "ready",
                "body": json.dumps(
                    {"tick_id": "01CANCEL", "phase_key": f"phase_{index}"}
                ),
            }
            for index, task_id in enumerate(task_ids)
        ]
        mocker.patch(
            "hermes_pipeline.kanban_tasks._list_task_snapshot",
            side_effect=[tasks, [{**task, "status": "archived"} for task in tasks]],
        )
        parents = {
            "t_00000001": [],
            "t_00000002": ["t_00000001"],
            "t_00000003": ["t_00000001"],
        }

        def _run(command, **_kwargs):
            if command[:3] == ["hermes", "kanban", "show"]:
                task_id = command[3]
                return mocker.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "task": {
                                "id": task_id,
                                "worker_pid": None,
                                "claim_lock": None,
                            },
                            "parents": parents[task_id],
                            "runs": [],
                        }
                    ),
                    stderr="",
                )
            return mocker.Mock(returncode=0, stdout="", stderr="")

        run = mocker.patch(
            "hermes_pipeline.kanban_tasks.subprocess.run", side_effect=_run
        )

        assert cancel_todo_kanban_tasks("demo", "01CANCEL") is True
        archive_commands = [
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:3] == ["hermes", "kanban", "archive"]
        ]
        assert [command[-1] for command in archive_commands] == [
            "t_00000002",
            "t_00000003",
            "t_00000001",
        ]

    @pytest.mark.parametrize(
        ("parents", "task_ids"),
        [
            ({"t_00000001": "t_00000002"}, ("t_00000001",)),
            ({"t_00000001": ["t_00000002"]}, ("t_00000001",)),
            (
                {
                    "t_00000001": ["t_00000002"],
                    "t_00000002": ["t_00000001"],
                },
                ("t_00000001", "t_00000002"),
            ),
        ],
        ids=("malformed_parents", "missing_parent", "cycle"),
    )
    def test_refuses_topology_cleanup_when_parent_graph_is_invalid(
        self, mocker, parents, task_ids
    ):
        from hermes_pipeline.kanban_tasks import cancel_todo_kanban_tasks

        tasks = [
            {
                "id": task_id,
                "status": "ready",
                "body": json.dumps(
                    {"tick_id": "01CANCEL", "phase_key": f"phase_{index}"}
                ),
            }
            for index, task_id in enumerate(task_ids)
        ]
        mocker.patch(
            "hermes_pipeline.kanban_tasks._list_task_snapshot", return_value=tasks
        )

        def _run(command, **_kwargs):
            if command[:3] == ["hermes", "kanban", "show"]:
                task_id = command[3]
                return mocker.Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "task": {
                                "id": task_id,
                                "worker_pid": None,
                                "claim_lock": None,
                            },
                            "parents": parents[task_id],
                            "runs": [],
                        }
                    ),
                    stderr="",
                )
            return mocker.Mock(returncode=0, stdout="", stderr="")

        run = mocker.patch(
            "hermes_pipeline.kanban_tasks.subprocess.run", side_effect=_run
        )

        assert cancel_todo_kanban_tasks("demo", "01CANCEL") is False
        assert not any(
            call.args[0][:3] == ["hermes", "kanban", "archive"]
            for call in run.call_args_list
        )
