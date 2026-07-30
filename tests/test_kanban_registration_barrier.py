"""Registration-barrier and ordered-cleanup regression tests."""

from __future__ import annotations

import json

import pytest


def _prepared_phases():
    from hermes_pipeline.kanban_tasks import PreparedPhaseTask

    return [
        PreparedPhaseTask("phase_1", "One", "body one", 5, False, 2400),
        PreparedPhaseTask("phase_gate", "Gate", "gate body", 0, True, 9999),
        PreparedPhaseTask("phase_2", "Two", "body two", 10, False, 7200),
    ]


def test_registration_barrier_owns_executable_chain_and_commits_last(
    tmp_path, mocker
):
    """A missing barrier or early release can dispatch a partial phase chain."""
    from hermes_pipeline.kanban_tasks import create_prepared_todo_phases

    ids_by_key = {
        "__registration_barrier__": "t_0000000b",
        "phase_1": "t_00000001",
        "phase_gate": "t_0000000a",
        "phase_2": "t_00000002",
    }
    events: list[str] = []
    create_commands: dict[str, list[str]] = {}

    def run(cmd, **_kwargs):
        if cmd[:3] == ["hermes", "kanban", "create"]:
            key = cmd[cmd.index("--idempotency-key") + 1].split(":", 1)[1]
            create_commands[key] = cmd
            events.append(f"create:{key}")
            return mocker.Mock(
                returncode=0,
                stdout=json.dumps({"id": ids_by_key[key]}),
                stderr="",
            )
        if cmd[:3] == ["hermes", "kanban", "block"]:
            events.append(f"block:{cmd[-1]}")
            return mocker.Mock(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["hermes", "kanban", "complete"]:
            events.append(f"complete:{cmd[-1]}")
            return mocker.Mock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected Hermes command: {cmd}")

    mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run", side_effect=run)
    mocker.patch(
        "hermes_pipeline.kanban_tasks._persist_expected_phases",
        side_effect=lambda *_args, **_kwargs: events.append("persist-expected"),
    )

    assert create_prepared_todo_phases(
        prepared=_prepared_phases(),
        tick_id="01CLIENT",
        board_slug="demo",
        project_dir=tmp_path,
        assignee="pipeline",
    ) == ["t_00000001", "t_0000000a", "t_00000002"]

    assert events == [
        "create:__registration_barrier__",
        "create:phase_1",
        "create:phase_gate",
        "block:t_0000000a",
        "create:phase_2",
        "persist-expected",
        "complete:t_0000000b",
    ]

    barrier = create_commands["__registration_barrier__"]
    barrier_body = barrier[barrier.index("--body") + 1]
    barrier_header = json.loads(barrier_body.splitlines()[0])
    assert barrier_header == {
        "infrastructure": "registration_barrier",
        "phase_key": "__registration_barrier__",
        "project_slug": "demo",
        "tick_id": "01CLIENT",
    }
    assert barrier[barrier.index("--assignee") + 1] == "-"
    assert "--goal" not in barrier
    assert "--max-runtime" not in barrier
    assert "--parent" not in barrier
    assert "--initial-status" not in barrier

    first = create_commands["phase_1"]
    assert first[first.index("--parent") + 1] == "t_0000000b"
    assert first[first.index("--assignee") + 1] == "pipeline"
    assert "--goal" in first
    assert (
        first[first.index("--max-runtime") + 1]
        == "2400"
    )
    assert "--initial-status" not in first

    gate = create_commands["phase_gate"]
    assert gate[gate.index("--assignee") + 1] == "-"
    assert "--goal" not in gate
    assert "--max-runtime" not in gate
    assert gate[gate.index("--parent") + 1] == "t_00000001"
    assert "--initial-status" not in gate

    second = create_commands["phase_2"]
    assert second[second.index("--parent") + 1] == "t_0000000a"
    assert (
        second[second.index("--max-runtime") + 1]
        == "7200"
    )
    assert "--initial-status" not in second


def test_phase_two_late_visibility_preserves_parents_then_cleans_child_first(
    tmp_path, mocker
):
    """An invisible current child must not be released by archiving its parents."""
    from hermes_pipeline.kanban_tasks import (
        PendingTaskCreate,
        _persist_pending_task_create,
        reconcile_pending_task_create,
    )

    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    pending = PendingTaskCreate(
        "demo",
        "01CLIENT",
        "phase_2",
        ("t_0000000b", "t_00000001"),
    )
    _persist_pending_task_create(tmp_path, pending)
    mocker.patch(
        "hermes_pipeline.kanban_tasks._find_task_id_in_snapshot",
        side_effect=[None, "t_00000002"],
    )
    archive_calls: list[tuple[list[str], str, dict[str, object]]] = []

    def archive(task_ids, *, tenant):
        archive_calls.append(
            (
                task_ids,
                tenant,
                json.loads(marker.read_text(encoding="utf-8")),
            )
        )
        return True

    mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks",
        side_effect=archive,
    )

    assert reconcile_pending_task_create(tmp_path) is False
    assert archive_calls == []
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "tenant": "demo",
        "tick_id": "01CLIENT",
        "phase_key": "phase_2",
        "known_task_ids": ["t_0000000b", "t_00000001"],
    }

    assert reconcile_pending_task_create(tmp_path) is True
    assert archive_calls == [
        (
            ["t_00000002", "t_00000001", "t_0000000b"],
            "demo",
            {
                "tenant": "demo",
                "tick_id": "01CLIENT",
                "cleanup_task_ids": [
                    "t_00000002",
                    "t_00000001",
                    "t_0000000b",
                ],
            },
        )
    ]
    assert not marker.exists()


def test_already_archived_snapshot_is_cleanup_success(mocker):
    """Hermes archive text is not truth when every recorded task is archived."""
    from hermes_pipeline.kanban_tasks import _archive_tasks

    snapshot = [
        {"id": "t_00000002", "status": "archived"},
        {"id": "t_00000001", "status": "archived"},
    ]
    run = mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        return_value=mocker.Mock(
            returncode=0,
            stdout=json.dumps(snapshot),
            stderr="",
        ),
    )

    assert _archive_tasks(
        ["t_00000002", "t_00000001"],
        tenant="demo",
    )
    run.assert_called_once_with(
        [
            "hermes",
            "kanban",
            "list",
            "--tenant",
            "demo",
            "--archived",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_cleanup_does_not_archive_parent_until_child_is_confirmed_archived(
    mocker,
):
    """A failed child archive must not release it by archiving its parent."""
    from hermes_pipeline.kanban_tasks import _archive_tasks

    snapshots = mocker.patch(
        "hermes_pipeline.kanban_tasks._list_task_snapshot",
        side_effect=[
            [
                {"id": "t_00000002", "status": "ready"},
                {"id": "t_00000001", "status": "todo"},
            ],
            [
                {"id": "t_00000002", "status": "ready"},
                {"id": "t_00000001", "status": "todo"},
            ],
        ],
    )
    run = mocker.patch(
        "hermes_pipeline.kanban_tasks.subprocess.run",
        return_value=mocker.Mock(returncode=0, stdout="cannot archive", stderr=""),
    )

    assert not _archive_tasks(
        ["t_00000002", "t_00000001"],
        tenant="demo",
    )
    run.assert_called_once()
    assert run.call_args.args[0] == [
        "hermes",
        "kanban",
        "archive",
        "t_00000002",
    ]
    assert snapshots.call_count == 2


def test_local_phase_two_oserror_becomes_cleanup_only_and_later_clears(
    tmp_path, mocker
):
    """A conclusive local spawn error must retain known IDs without a phantom child."""
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
        reconcile_pending_task_create,
    )

    created = iter(
        [
            ("__registration_barrier__", "t_0000000b"),
            ("phase_1", "t_00000001"),
        ]
    )

    def run(cmd, **_kwargs):
        if cmd[:3] != ["hermes", "kanban", "create"]:
            raise AssertionError(f"unexpected Hermes command: {cmd}")
        key = cmd[cmd.index("--idempotency-key") + 1].split(":", 1)[1]
        if key == "phase_2":
            raise OSError("local exec failed")
        expected_key, task_id = next(created)
        assert key == expected_key
        return mocker.Mock(
            returncode=0,
            stdout=json.dumps({"id": task_id}),
            stderr="",
        )

    mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run", side_effect=run)
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks",
        return_value=False,
    )

    with pytest.raises(RuntimeError, match=r"phase_2.*local exec failed"):
        create_prepared_todo_phases(
            prepared=[
                PreparedPhaseTask("phase_1", "One", "body", 5, False),
                PreparedPhaseTask("phase_2", "Two", "body", 5, False),
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

    archive.reset_mock()
    archive.return_value = True
    assert reconcile_pending_task_create(tmp_path)
    archive.assert_called_once_with(
        ["t_00000001", "t_0000000b"],
        tenant="demo",
    )
    assert not marker.exists()


def test_barrier_completion_failure_retains_commit_pending(tmp_path, mocker):
    """An inconclusive commit call must remain retryable without cleanup."""
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    ids_by_key = {
        "__registration_barrier__": "t_0000000b",
        "phase_1": "t_00000001",
    }

    def run(cmd, **_kwargs):
        if cmd[:3] == ["hermes", "kanban", "create"]:
            key = cmd[cmd.index("--idempotency-key") + 1].split(":", 1)[1]
            return mocker.Mock(
                returncode=0,
                stdout=json.dumps({"id": ids_by_key[key]}),
                stderr="",
            )
        if cmd[:3] == ["hermes", "kanban", "complete"]:
            return mocker.Mock(returncode=1, stdout="", stderr="commit failed")
        raise AssertionError(f"unexpected Hermes command: {cmd}")

    mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run", side_effect=run)
    archive = mocker.patch(
        "hermes_pipeline.kanban_tasks._archive_tasks",
        return_value=False,
    )

    with pytest.raises(RuntimeError, match=r"complete registration barrier"):
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
        "barrier_task_id": "t_0000000b",
        "cleanup_task_ids": ["t_00000001", "t_0000000b"],
    }
    archive.assert_not_called()


def test_registration_persists_commit_pending_until_barrier_completes(
    tmp_path, mocker
):
    """The crash-recovery marker must span the remote commit mutation."""
    from hermes_pipeline.kanban_tasks import (
        PreparedPhaseTask,
        create_prepared_todo_phases,
    )

    marker = tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    ids_by_key = {
        "__registration_barrier__": "t_0000000b",
        "phase_1": "t_00000001",
    }

    def run(cmd, **_kwargs):
        if cmd[:3] == ["hermes", "kanban", "create"]:
            key = cmd[cmd.index("--idempotency-key") + 1].split(":", 1)[1]
            return mocker.Mock(
                returncode=0,
                stdout=json.dumps({"id": ids_by_key[key]}),
                stderr="",
            )
        if cmd[:3] == ["hermes", "kanban", "complete"]:
            assert json.loads(marker.read_text(encoding="utf-8")) == {
                "tenant": "demo",
                "tick_id": "01CLIENT",
                "barrier_task_id": "t_0000000b",
                "cleanup_task_ids": ["t_00000001", "t_0000000b"],
            }
            return mocker.Mock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected Hermes command: {cmd}")

    mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run", side_effect=run)

    assert create_prepared_todo_phases(
        prepared=[PreparedPhaseTask("phase_1", "One", "body", 5, False)],
        tick_id="01CLIENT",
        board_slug="demo",
        project_dir=tmp_path,
    ) == ["t_00000001"]
    assert not marker.exists()


@pytest.mark.parametrize("barrier_status", ["ready", "todo"])
def test_reconcile_commit_pending_retries_ready_barrier(
    tmp_path, mocker, barrier_status
):
    """A crash before completion is recovered by retrying the commit point."""
    from hermes_pipeline.kanban_tasks import (
        PendingBarrierCommit,
        _persist_pending_barrier_commit,
        reconcile_pending_task_create,
    )

    pending = PendingBarrierCommit(
        "demo",
        "01CLIENT",
        "t_0000000b",
        ("t_00000001", "t_0000000b"),
    )
    _persist_pending_barrier_commit(tmp_path, pending)
    mocker.patch(
        "hermes_pipeline.kanban_tasks._task_status_in_snapshot",
        return_value=barrier_status,
    )
    complete = mocker.patch(
        "hermes_pipeline.kanban_tasks._complete_registration_barrier"
    )

    assert reconcile_pending_task_create(tmp_path)
    complete.assert_called_once_with("t_0000000b")
    assert not (
        tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    ).exists()


def test_reconcile_commit_pending_accepts_already_completed_barrier(
    tmp_path, mocker
):
    """A crash after remote completion clears the durable marker on retry."""
    from hermes_pipeline.kanban_tasks import (
        PendingBarrierCommit,
        _persist_pending_barrier_commit,
        reconcile_pending_task_create,
    )

    pending = PendingBarrierCommit(
        "demo",
        "01CLIENT",
        "t_0000000b",
        ("t_00000001", "t_0000000b"),
    )
    _persist_pending_barrier_commit(tmp_path, pending)
    mocker.patch(
        "hermes_pipeline.kanban_tasks._task_status_in_snapshot",
        return_value="done",
    )
    complete = mocker.patch(
        "hermes_pipeline.kanban_tasks._complete_registration_barrier"
    )

    assert reconcile_pending_task_create(tmp_path)
    complete.assert_not_called()


def test_reconcile_commit_pending_fails_closed_on_uncertain_status(
    tmp_path, mocker
):
    """An unreadable or unexpected barrier state must retain recovery state."""
    from hermes_pipeline.kanban_tasks import (
        PendingBarrierCommit,
        _persist_pending_barrier_commit,
        reconcile_pending_task_create,
    )

    pending = PendingBarrierCommit(
        "demo",
        "01CLIENT",
        "t_0000000b",
        ("t_00000001", "t_0000000b"),
    )
    _persist_pending_barrier_commit(tmp_path, pending)
    mocker.patch(
        "hermes_pipeline.kanban_tasks._task_status_in_snapshot",
        return_value=None,
    )
    complete = mocker.patch(
        "hermes_pipeline.kanban_tasks._complete_registration_barrier"
    )

    assert not reconcile_pending_task_create(tmp_path)
    complete.assert_not_called()
    assert (
        tmp_path / ".hermes" / "outcomes" / "pending-task-create.json"
    ).exists()
