"""Tests for hermes_pipeline.kanban_tasks — kanban task registration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_pipeline.phases import load_phases


class FakeGatePhase:
    def __init__(self, phase_key, name="P", prompt="", tools="", turns=0, gate=False):
        self.phase_key = phase_key
        self.name = name
        self.prompt = prompt
        self.tools = tools
        self.turns = turns
        self.gate = gate

class TestRegisterTodoPhases:
    """Tests for register_todo_phases()."""

    def test_creates_tasks_with_parent_chain(self, tmp_path, mocker):
        """Phases are registered as kanban tasks with --parent deps."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(
            returncode=0, stdout=json.dumps({"id": "task-001"})
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
            returncode=0, stdout=json.dumps({"id": "task-001"})
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
            returncode=0, stdout=json.dumps({"id": "task-001"})
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
                returncode=0, stdout=json.dumps({"id": "task-001"})
            ),
            mocker.MagicMock(returncode=1, stdout="", stderr="error"),
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

        # Verify archive was called for task-001
        archive_call = mock_run.call_args_list[2]
        archive_args = archive_call[0][0]
        assert "kanban" in archive_args
        assert "archive" in archive_args
        assert "task-001" in archive_args

    def test_returns_task_ids(self, tmp_path, mocker):
        """register_todo_phases returns a list of created task IDs."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            mocker.MagicMock(
                returncode=0, stdout=json.dumps({"id": "task-001"})
            ),
            mocker.MagicMock(
                returncode=0, stdout=json.dumps({"id": "task-002"})
            ),
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

        assert task_ids == ["task-001", "task-002"]

    def test_gate_phase_registered_blocked_without_goal(self, tmp_path, mocker):
        """Gate phases get --initial-status blocked, no --goal flags."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        phases = [
            FakeGatePhase("phase_8_finish_branch", name="P8", turns=15),
            FakeGatePhase("phase_9_ship", name="Ship Gate", gate=True),
        ]
        mocker.patch("hermes_pipeline.kanban_tasks.load_phases", return_value=phases)
        mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
        mock_run.return_value = mocker.Mock(returncode=0, stdout='{"id": "t_x"}', stderr="")

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

        # Normal phase (index 0) should have --goal, no --initial-status
        phase8_cmd = mock_run.call_args_list[0][0][0]
        assert "--goal" in phase8_cmd
        assert "--initial-status" not in phase8_cmd


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

        _archive_tasks(["task-001", "task-002", "task-003"])

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
        _archive_tasks(["task-001", "task-002", "task-003"])
        assert mock_run.call_count == 3

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
        from hermes_pipeline.kanban_tasks import get_todo_kanban_status

        import subprocess

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
