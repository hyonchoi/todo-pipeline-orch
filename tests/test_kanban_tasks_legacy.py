"""Additional coverage for kanban_tasks legacy paths and edge cases."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_pipeline.kanban_tasks import (
    register_todo_phases,
    get_todo_kanban_status,
    all_phases_complete,
    _archive_tasks,
)


class TestRegisterTodoPhasesLegacy:
    """Tests for legacy CLI fallback and edge cases in register_todo_phases."""

    def test_legacy_cli_output_format(self, tmp_path, mocker):
        """Old CLI returns 'Created t_xxx  (ready, assignee=-)' — fallback parsing."""
        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(
            returncode=0,
            stdout="Created t_legacy123  (ready, assignee=-)",
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

        task_ids = register_todo_phases(
            todo_id="TODO-10",
            tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            board_slug="demo",
            project_dir=str(tmp_path),
            phases_path=str(phases_cfg),
        )

        assert task_ids == ["t_legacy123"]

    def test_unparseable_task_id_raises(self, tmp_path, mocker):
        """Output that is neither JSON nor 'Created t_xxx' raises RuntimeError."""
        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = mocker.MagicMock(
            returncode=0,
            stdout="Some unexpected output",
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

    def test_invalid_todo_id_format(self, tmp_path):
        """Invalid todo_id format raises ValueError before any subprocess calls."""
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

        with pytest.raises(ValueError, match="invalid todo_id"):
            register_todo_phases(
                todo_id="INVALID",
                tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
                board_slug="demo",
                project_dir=str(tmp_path),
                phases_path=str(phases_cfg),
            )

    def test_invalid_todo_id_shell_injection(self, tmp_path):
        """todo_id with shell metacharacters is rejected."""
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

        with pytest.raises(ValueError, match="invalid todo_id"):
            register_todo_phases(
                todo_id="TODO-10; rm -rf /",
                tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
                board_slug="demo",
                project_dir=str(tmp_path),
                phases_path=str(phases_cfg),
            )

    def test_goal_flags_present(self, tmp_path, mocker):
        """--goal and --goal-max-turns flags are included in the command."""
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
        assert "--goal" in call_args
        assert "--goal-max-turns" in call_args
        idx = call_args.index("--goal-max-turns")
        assert call_args[idx + 1] == "20"

    def test_archive_tasks_failure_best_effort(self, mocker):
        """_archive_tasks does not raise when archive fails — best-effort."""
        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            mocker.MagicMock(returncode=1, stdout="", stderr="error"),
        ]

        # Should not raise
        _archive_tasks(["task-001"])
        assert mock_run.call_count == 1

    def test_archive_tasks_exception_best_effort(self, mocker):
        """_archive_tasks does not raise when subprocess throws — best-effort."""
        mock_run = mocker.patch("subprocess.run")
        mock_run.side_effect = [
            FileNotFoundError("hermes not found"),
        ]

        # Should not raise
        _archive_tasks(["task-001"])
        assert mock_run.call_count == 1


class TestAllPhasesCompleteEdgeCases:
    """Edge cases for all_phases_complete."""

    def test_all_done_and_failed_is_complete(self, mocker):
        """Mixed done and failed tasks -> all complete (both are completion statuses)."""
        mock_data = [
            {"status": "done", "body": '{"tick_id":"01HA","phase_key":"phase_2_autoplan","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "failed", "body": '{"tick_id":"01HA","phase_key":"phase_4_development","todo_id":"TODO-10","project_slug":"demo"}\n...'},
            {"status": "failed", "body": '{"tick_id":"01HA","phase_key":"phase_6_1_cso","todo_id":"TODO-10","project_slug":"demo"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        assert all_phases_complete("demo", "01HA") is True

    def test_tick_started_sentinel_returns_true(self, mocker, tmp_path):
        """tick_started sentinel + no kanban tasks -> False (stall, not complete).

        When the prior tick crashed after writing tick_started but before
        registering any kanban tasks, it's a stall — not a completion.
        The circuit breaker should detect this as no-progress.
        """
        mock_data = []
        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        # Create the tick_started sentinel
        outcomes_dir = tmp_path / "outcomes"
        outcomes_dir.mkdir()
        sentinel = outcomes_dir / "01HA-phases.json"
        sentinel.write_text('{"outcome": "tick_started"}\n')

        assert all_phases_complete("demo", "01HA", state_dir=str(tmp_path)) is False

    def test_no_state_dir_no_tasks_returns_false(self, mocker):
        """No state_dir and no tasks -> conservative False."""
        mock_data = []
        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        # No state_dir — conservative: don't release lock
        assert all_phases_complete("demo", "01HA") is False

    def test_json_parse_error_in_sentinel(self, mocker, tmp_path):
        """Sentinel file with invalid JSON — treated as not found, return False."""
        mock_data = []
        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        outcomes_dir = tmp_path / "outcomes"
        outcomes_dir.mkdir()
        sentinel = outcomes_dir / "01HA-phases.json"
        sentinel.write_text("not valid json\n")

        # Should not raise — JSONDecodeError is caught
        assert all_phases_complete("demo", "01HA", state_dir=str(tmp_path)) is False


class TestGetTodoKanbanStatusEdgeCases:
    """Edge cases for get_todo_kanban_status."""

    def test_returncode_nonzero_returns_empty(self, mocker):
        """Kanban list returns non-zero -> empty dict."""
        mock_result = mocker.MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"
        mocker.patch("subprocess.run", return_value=mock_result)

        from hermes_pipeline.kanban_tasks import get_todo_kanban_status
        result = get_todo_kanban_status("demo", "01HA")
        assert result == {}

    def test_skips_task_without_tick_id(self, mocker):
        """Tasks without tick_id in header are skipped."""
        mock_data = [
            {"status": "done", "body": '{"phase_key":"phase_2_autoplan"}\n...'},
        ]

        mock_result = mocker.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mocker.patch("subprocess.run", return_value=mock_result)

        from hermes_pipeline.kanban_tasks import get_todo_kanban_status
        result = get_todo_kanban_status("demo", "01HA")
        assert result == {}

    def test_file_not_found_returns_empty(self, mocker):
        """hermes not found -> empty dict."""
        mocker.patch("subprocess.run", side_effect=FileNotFoundError("hermes not found"))

        from hermes_pipeline.kanban_tasks import get_todo_kanban_status
        result = get_todo_kanban_status("demo", "01HA")
        assert result == {}
