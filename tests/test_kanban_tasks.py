"""Tests for hermes_pipeline.kanban_tasks — kanban task registration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_pipeline.phases import load_phases


class TestRegisterTodoPhases:
    """Tests for register_todo_phases()."""

    def test_creates_tasks_with_parent_chain(self, tmp_path, mocker):
        """Phases are registered as kanban tasks with --parent deps."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=0, stdout="task-001")

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
        assert "--board" in first_call_args
        assert "demo" in first_call_args
        assert "--parent" not in first_call_args

        # Second call: --parent with first task id
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "--parent" in second_call_args

    def test_task_body_has_json_header(self, tmp_path, mocker):
        """Task body starts with a JSON header line containing tick_id, phase_key, todo_id."""
        from hermes_pipeline.kanban_tasks import register_todo_phases

        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(returncode=0, stdout="task-001")

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
        mock_run.return_value = mocker.MagicMock(returncode=0, stdout="task-001")

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
            mocker.MagicMock(returncode=0, stdout="task-001"),
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
            mocker.MagicMock(returncode=0, stdout="task-001"),
            mocker.MagicMock(returncode=0, stdout="task-002"),
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
