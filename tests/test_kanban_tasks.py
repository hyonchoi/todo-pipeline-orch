"""Tests for hermes_pipeline.kanban_tasks — kanban task registration."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


class FakeGatePhase:
    def __init__(self, phase_key, name="P", prompt="", tools="", turns=0, gate=False):
        self.phase_key = phase_key
        self.name = name
        self.prompt = prompt
        self.tools = tools
        self.turns = turns
        self.gate = gate


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
    archive.assert_called_once_with(["t_deadbeef"])
    assert find.call_count == 2
    assert not marker.exists()


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
    archive.assert_called_once_with(["t_deadbeef"])


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
        "    prompt: 'Use {skill_prefix}review in {agent_product}.'\n"
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
    assert "Use $review in Codex." in prepared[0].body


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
        ),
        PreparedPhaseTask(
            phase_key="phase_2",
            name="Two",
            body="second body",
            turns=10,
            gate=False,
        ),
    ]
    mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    mock_run.side_effect = [
        mocker.Mock(returncode=0, stdout='{"id": "t_00000001"}', stderr=""),
        mocker.Mock(returncode=0, stdout='{"id": "t_00000002"}', stderr=""),
    ]
    mocker.patch("hermes_pipeline.kanban_tasks._promote_task")

    task_ids = create_prepared_todo_phases(
        prepared=prepared,
        tick_id="01CLIENT",
        board_slug="demo",
        project_dir=tmp_path,
    )

    assert task_ids == ["t_00000001", "t_00000002"]
    assert mock_run.call_count == 2
    assert "--parent" not in mock_run.call_args_list[0].args[0]
    assert "--parent" in mock_run.call_args_list[1].args[0]
    assert "--body" in mock_run.call_args_list[0].args[0]
    assert "already rendered $body" in mock_run.call_args_list[0].args[0]


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
    mocker.patch(
        "hermes_pipeline.kanban_tasks._clear_pending_task_create",
        create=True,
        side_effect=lambda _project_dir, pending: events.append(
            f"clear:{pending.phase_key}"
        )
        or True,
    )

    def run_create(cmd, **_kwargs):
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
    mocker.patch(
        "hermes_pipeline.kanban_tasks._promote_task",
        create=True,
        side_effect=lambda task_id: events.append(f"promote:{task_id}"),
    )

    assert create_prepared_todo_phases(
        prepared=prepared,
        tick_id="01CLIENT",
        board_slug="demo",
        project_dir=tmp_path,
    ) == ["t_00000001", "t_00000002"]

    assert events == [
        "pending:phase_1",
        "create:phase_1",
        "clear:phase_1",
        "pending:phase_2",
        "create:phase_2",
        "clear:phase_2",
        "persist-expected",
        "promote:t_00000001",
    ]
    assert all(
        command[command.index("--initial-status") + 1] == "blocked"
        for command in create_commands
    )
    assert "--goal" in create_commands[0]
    assert "--parent" not in create_commands[0]
    assert create_commands[1][create_commands[1].index("--parent") + 1] == (
        "t_00000001"
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


def test_pending_marker_survives_two_timed_out_creates_and_empty_snapshot(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=60),
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=60),
        mocker.Mock(returncode=0, stdout="[]", stderr=""),
    ]

    with pytest.raises(RuntimeError, match=r"phase_1.*Hermes process"):
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
        "phase_key": "phase_1",
        "known_task_ids": [],
    }


@pytest.mark.parametrize(
    ("cleanup_succeeded", "error_suffix"),
    [
        (True, ""),
        (False, "cleanup could not be confirmed"),
    ],
)
def test_pending_marker_second_write_failure_archives_prior_tasks(
    tmp_path, mocker, cleanup_succeeded, error_suffix
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False),
        PreparedPhaseTask("phase_2", "Two", "body", 5, False),
    ]
    persist_pending = mocker.patch(
        "hermes_pipeline.kanban_tasks._persist_pending_task_create",
        side_effect=[None, OSError("disk full")],
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks._clear_pending_task_create",
        return_value=True,
    )
    run = mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        return_value=mocker.Mock(
            returncode=0,
            stdout='{"id": "t_00000001"}',
            stderr="",
        ),
    )
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks",
        return_value=cleanup_succeeded,
    )
    promote = mocker.patch("hermes_pipeline.kanban_tasks._promote_task")

    error_pattern = r"phase_2"
    if error_suffix:
        error_pattern += rf".*{error_suffix}"
    with pytest.raises(RuntimeError, match=error_pattern):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert persist_pending.call_count == 2
    run.assert_called_once()
    archive.assert_called_once_with(["t_00000001"])
    promote.assert_not_called()
    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    if cleanup_succeeded:
        assert not marker.exists()
    else:
        assert json.loads(marker.read_text(encoding="utf-8")) == {
            "tenant": "demo",
            "tick_id": "01CLIENT",
            "cleanup_task_ids": ["t_00000001"],
        }


@pytest.mark.parametrize("failure_stage", ["persist", "promote"])
def test_activation_order_failure_archives_all_tasks_and_never_continues(
    tmp_path, mocker, failure_stage
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False),
        PreparedPhaseTask("phase_2", "Two", "body", 5, False),
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        mocker.Mock(returncode=0, stdout='{"id": "t_00000001"}', stderr=""),
        mocker.Mock(returncode=0, stdout='{"id": "t_00000002"}', stderr=""),
    ]
    mocker.patch(
        "hermes_pipeline.kanban_tasks._clear_pending_task_create",
        create=True,
        return_value=True,
    )
    persist_expected = mocker.patch(
        "hermes_pipeline.kanban_tasks._persist_expected_phases"
    )
    promote = mocker.patch(
        "hermes_pipeline.kanban_tasks._promote_task",
        create=True,
    )
    if failure_stage == "persist":
        persist_expected.side_effect = RuntimeError("sentinel write failed")
    else:
        promote.side_effect = RuntimeError("promotion failed")
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks",
        return_value=True,
    )

    with pytest.raises(RuntimeError):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    archive.assert_called_once_with(["t_00000001", "t_00000002"])
    if failure_stage == "persist":
        promote.assert_not_called()
    else:
        promote.assert_called_once_with("t_00000001")


@pytest.mark.parametrize("failure_stage", ["persist", "promote"])
def test_pending_marker_retains_all_tasks_when_activation_cleanup_fails(
    tmp_path, mocker, failure_stage
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
        reconcile_pending_task_create,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False),
        PreparedPhaseTask("phase_2", "Two", "body", 5, False),
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        mocker.Mock(returncode=0, stdout='{"id": "t_00000001"}', stderr=""),
        mocker.Mock(returncode=0, stdout='{"id": "t_00000002"}', stderr=""),
    ]
    persist_expected = mocker.patch(
        "hermes_pipeline.kanban_tasks._persist_expected_phases"
    )
    promote = mocker.patch("hermes_pipeline.kanban_tasks._promote_task")
    if failure_stage == "persist":
        persist_expected.side_effect = RuntimeError("sentinel write failed")
    else:
        promote.side_effect = RuntimeError("promotion failed")
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks",
        return_value=False,
    )

    with pytest.raises(RuntimeError, match="cleanup remains pending"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "tenant": "demo",
        "tick_id": "01CLIENT",
        "cleanup_task_ids": ["t_00000001", "t_00000002"],
    }
    archive.assert_called_once_with(["t_00000001", "t_00000002"])
    if failure_stage == "persist":
        promote.assert_not_called()
    else:
        promote.assert_called_once_with("t_00000001")

    archive.reset_mock()
    archive.return_value = True
    assert reconcile_pending_task_create(tmp_path)
    archive.assert_called_once_with(["t_00000001", "t_00000002"])
    assert not marker.exists()


def test_activation_order_promote_task_invokes_hermes(tmp_path, mocker):
    import hermes_pipeline.kanban_tasks as kanban_tasks

    run = mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout="", stderr=""),
    )
    promote = getattr(kanban_tasks, "_promote_task", None)

    assert promote is not None
    promote("t_00000001")

    run.assert_called_once_with(
        ["hermes", "kanban", "promote", "t_00000001"],
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
    "failure",
    [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        OSError("hermes unavailable"),
    ],
)
def test_create_prepared_archives_prior_tasks_on_process_failure(
    tmp_path, mocker, failure
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask(f"phase_{index}", str(index), "body", 5, False)
        for index in (1, 2)
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    side_effects = [
        mocker.Mock(returncode=0, stdout='{"id": "t_00000001"}', stderr=""),
        failure,
    ]
    if isinstance(failure, subprocess.TimeoutExpired):
        side_effects.append(mocker.Mock(returncode=1, stdout="", stderr="failed"))
    side_effects.append(mocker.Mock(returncode=0, stdout="", stderr=""))
    run.side_effect = side_effects

    with pytest.raises(RuntimeError, match=r"phase_2.*Hermes process"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert run.call_args_list[-1].args[0][-1] == "t_00000001"


def test_create_prepared_recovers_and_archives_task_after_timeout(tmp_path, mocker):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False)
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        mocker.Mock(returncode=0, stdout='{"id": "t_deadbeef"}', stderr=""),
        mocker.Mock(returncode=0, stdout="", stderr=""),
    ]

    with pytest.raises(RuntimeError, match=r"phase_1.*Hermes process"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert run.call_args_list[1].args[0] == run.call_args_list[0].args[0]
    assert run.call_args_list[2].args[0][-1] == "t_deadbeef"


@pytest.mark.parametrize(
    "recovery_failure",
    [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        OSError("hermes unavailable"),
    ],
)
def test_create_prepared_resolves_task_from_snapshot_when_recovery_is_uncertain(
    tmp_path, mocker, recovery_failure
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask(
            "phase_1",
            "One",
            '{"phase_key": "phase_1", "tick_id": "01CLIENT"}\nbody',
            5,
            False,
        )
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        recovery_failure,
        mocker.Mock(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "id": "t_deadbeef",
                        "body": prepared[0].body,
                        "status": "ready",
                    }
                ]
            ),
            stderr="",
        ),
        mocker.Mock(returncode=0, stdout="", stderr=""),
    ]

    with pytest.raises(RuntimeError, match=r"phase_1.*Hermes process"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert run.call_args_list[2].args[0] == [
        "hermes",
        "kanban",
        "list",
        "--tenant",
        "demo",
        "--json",
    ]
    assert run.call_args_list[3].args[0][-1] == "t_deadbeef"


@pytest.mark.parametrize(
    "snapshot_stdout",
    [
        "null",
        '"unexpected"',
        "42",
        '{"tasks": null}',
        '{"tasks": "unexpected"}',
    ],
)
def test_create_prepared_rejects_malformed_snapshot_and_archives_known_tasks(
    tmp_path, mocker, snapshot_stdout
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False),
        PreparedPhaseTask("phase_2", "Two", "body", 5, False),
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        mocker.Mock(returncode=0, stdout='{"id": "t_00000001"}', stderr=""),
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        mocker.Mock(returncode=0, stdout=snapshot_stdout, stderr=""),
        mocker.Mock(returncode=0, stdout="", stderr=""),
    ]

    with pytest.raises(RuntimeError, match=r"phase_2.*Hermes process"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert run.call_args_list[4].args[0][-1] == "t_00000001"


@pytest.mark.parametrize(
    "snapshot_result",
    [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=10),
        OSError("hermes unavailable"),
        None,
        "not-json",
        "[null]",
        '[{"body": null}]',
    ],
)
def test_create_prepared_snapshot_failures_archive_only_known_tasks(
    tmp_path, mocker, snapshot_result
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False),
        PreparedPhaseTask("phase_2", "Two", "body", 5, False),
    ]
    if isinstance(snapshot_result, BaseException):
        snapshot_effect = snapshot_result
    elif snapshot_result is None:
        snapshot_effect = mocker.Mock(returncode=1, stdout="", stderr="failed")
    else:
        snapshot_effect = mocker.Mock(
            returncode=0,
            stdout=snapshot_result,
            stderr="",
        )
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        mocker.Mock(returncode=0, stdout='{"id": "t_00000001"}', stderr=""),
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        snapshot_effect,
        mocker.Mock(returncode=0, stdout="", stderr=""),
    ]

    with pytest.raises(RuntimeError, match=r"phase_2.*Hermes process"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert run.call_args_list[-1].args[0][-1] == "t_00000001"


@pytest.mark.parametrize("task_body", ["null\nbody", "[]\nbody", '"unexpected"\nbody'])
def test_create_prepared_rejects_nonmapping_snapshot_headers(
    tmp_path, mocker, task_body
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False)
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        subprocess.TimeoutExpired(cmd=["hermes"], timeout=30),
        mocker.Mock(
            returncode=0,
            stdout=json.dumps([{"id": "t_deadbeef", "body": task_body}]),
            stderr="",
        ),
    ]

    with pytest.raises(RuntimeError, match=r"phase_1.*Hermes process"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert len(run.call_args_list) == 3


def test_create_prepared_recovers_and_archives_task_after_nonzero_result(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False)
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        mocker.Mock(returncode=1, stdout="", stderr="transport failed"),
        mocker.Mock(returncode=0, stdout='{"id": "t_deadbeef"}', stderr=""),
        mocker.Mock(returncode=0, stdout="", stderr=""),
    ]

    with pytest.raises(RuntimeError, match=r"phase_1.*rc=1"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert run.call_args_list[1].args[0] == run.call_args_list[0].args[0]
    assert run.call_args_list[2].args[0][-1] == "t_deadbeef"


def test_create_prepared_recovers_and_archives_task_after_invalid_output(
    tmp_path, mocker
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask("phase_1", "One", "body", 5, False)
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        mocker.Mock(returncode=0, stdout='{"id": null}', stderr=""),
        mocker.Mock(returncode=0, stdout='{"id": "t_deadbeef"}', stderr=""),
        mocker.Mock(returncode=0, stdout="", stderr=""),
    ]

    with pytest.raises(RuntimeError, match=r"phase_1.*task ID"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert run.call_args_list[1].args[0] == run.call_args_list[0].args[0]
    assert run.call_args_list[2].args[0][-1] == "t_deadbeef"


@pytest.mark.parametrize(
    "malformed_output",
    [
        '{"id": null}',
        '{"id": ""}',
        '{"id": "task 002"}',
        '{"missing": "id"}',
        "Created",
    ],
)
def test_create_prepared_rejects_malformed_task_id_and_archives_prior_tasks(
    tmp_path, mocker, malformed_output
):
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    prepared = [
        PreparedPhaseTask(f"phase_{index}", str(index), "body", 5, False)
        for index in (1, 2)
    ]
    run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    run.side_effect = [
        mocker.Mock(returncode=0, stdout='{"id": "t_00000001"}', stderr=""),
        mocker.Mock(returncode=0, stdout=malformed_output, stderr=""),
        mocker.Mock(returncode=1, stdout="", stderr="failed"),
        mocker.Mock(returncode=0, stdout="", stderr=""),
    ]

    with pytest.raises(RuntimeError, match=r"phase_2.*task ID"):
        create_prepared_todo_phases(
            prepared=prepared,
            tick_id="01CLIENT",
            board_slug="demo",
            project_dir=tmp_path,
        )

    assert run.call_args_list[-1].args[0][-1] == "t_00000001"


class TestRegisterTodoPhases:
    """Tests for register_todo_phases()."""

    def test_creates_tasks_with_parent_chain(self, tmp_path, mocker):
        """Phases are registered as kanban tasks with --parent deps."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(
            returncode=0, stdout=json.dumps({"id": "t_00000001"})
        )
        mocker.patch("hermes_pipeline.kanban_tasks._promote_task")

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

        # Should have been called twice (2 phases)
        assert mock_run.call_count == 2

        # First call: no --parent
        first_call_args = mock_run.call_args_list[0][0][0]
        assert "hermes" in first_call_args
        assert "kanban" in first_call_args
        assert "create" in first_call_args
        assert "--tenant" in first_call_args
        assert "demo" in first_call_args
        assert "--parent" not in first_call_args

        # Second call: --parent with first task id
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "--parent" in second_call_args

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

        # Extract the --body argument from the call
        call_args = mock_run.call_args_list[0][0][0]
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

        call_args = mock_run.call_args_list[0][0][0]
        key_idx = call_args.index("--idempotency-key")
        key_value = call_args[key_idx + 1]

        assert key_value == "01HA6PH2V0ZJ7GK0S39D243TQX:phase_2_autoplan"

    def test_mid_registration_failure_archives_created_tasks(self, tmp_path, mocker):
        """If the 2nd task fails, the 1st is archived via hermes kanban archive."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        # First call succeeds, second call fails
        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            mocker.MagicMock(
                returncode=0, stdout=json.dumps({"id": "t_00000001"})
            ),
            mocker.MagicMock(returncode=1, stdout="", stderr="error"),
            # Idempotent recovery retry also fails
            mocker.MagicMock(returncode=1, stdout="", stderr="error"),
            # Snapshot lookup confirms no second task was created
            mocker.MagicMock(returncode=0, stdout="[]", stderr=""),
            # Archive call
            mocker.MagicMock(returncode=0, stdout=""),
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

        with pytest.raises(RuntimeError, match="failed to register"):
            register_todo_phases(
                todo_id="TODO-10",
                tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
                board_slug="demo",
                project_dir=str(tmp_path),
                phases_path=str(phases_cfg),
            )

        # Verify archive was called for t_00000001
        assert mock_run.call_args_list[3].args[0][:3] == [
            "hermes",
            "kanban",
            "list",
        ]
        archive_call = mock_run.call_args_list[4]
        archive_args = archive_call[0][0]
        assert "kanban" in archive_args
        assert "archive" in archive_args
        assert "t_00000001" in archive_args

    def test_returns_task_ids(self, tmp_path, mocker):
        """register_todo_phases returns a list of created task IDs."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            mocker.MagicMock(
                returncode=0, stdout=json.dumps({"id": "t_00000001"})
            ),
            mocker.MagicMock(
                returncode=0, stdout=json.dumps({"id": "t_00000002"})
            ),
        ]
        mocker.patch("hermes_pipeline.kanban_tasks._promote_task")

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

    def test_gate_phase_registered_blocked_without_goal(self, tmp_path, mocker):
        """Gate phases get --initial-status blocked, no --goal flags."""
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

        # Gate phase (index 1) should have --initial-status blocked, no --goal
        gate_cmd = mock_run.call_args_list[1][0][0]
        assert "--initial-status" in gate_cmd
        assert gate_cmd[gate_cmd.index("--initial-status") + 1] == "blocked"
        assert "--goal" not in gate_cmd
        assert "--parent" not in gate_cmd

        # Executable phases keep goal mode but remain blocked until activation.
        phase8_cmd = mock_run.call_args_list[0][0][0]
        assert "--goal" in phase8_cmd
        assert phase8_cmd[phase8_cmd.index("--initial-status") + 1] == "blocked"

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

        phase8_cmd = mock_run.call_args_list[0][0][0]
        gate_cmd = mock_run.call_args_list[1][0][0]
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
