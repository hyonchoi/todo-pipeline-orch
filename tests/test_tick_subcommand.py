"""Tests for the tick subcommand (TODO-10)."""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from hermes_pipeline.cli import _cmd_tick, build_parser
from hermes_pipeline.config import Config
from tests.gh_fakes import seed_project_issues, todo_payload

PIPELINE_TOML = (
    'schema_version = 2\nassignee = "default"\n'
    'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
)


@pytest.fixture(autouse=True)
def _github_todo_10(fake_gh):
    """Every tick reads TODOs from GitHub: serve #10 as the sole candidate."""
    return seed_project_issues(fake_gh, [todo_payload(10, title="test")])


def _make_decision(picked=None, **kwargs):
    """Create a mock HermesSelectionDecision with the right shape."""
    decision = MagicMock()
    decision.picked = picked or kwargs.get("picked")
    decision.rationale = "test"
    decision.candidates_considered = kwargs.get("candidates_considered", [])
    return decision


def _create_project(projects_dir, name, contract=True):
    """Create a project directory, marked as a project by .hermes/pipeline.toml."""
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    if contract:
        (project_dir / ".hermes").mkdir(exist_ok=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(PIPELINE_TOML)
    return project_dir


class FakeArgs:
    """Minimal argparse.Namespace for testing."""
    def __init__(self, **kwargs):
        kwargs.setdefault("project", None)
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestTickSubcommand:
    """Tests for tpo tick (scan loop)."""

    def test_tick_help(self):
        """tick subcommand shows in help."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["tick", "--help"])

    def test_tick_outer_error_boundary_redacts_chained_exception(
        self, tmp_path, mocker, caplog
    ):
        secret = "prompt-token=secret-value"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        def fail_tick(**_kwargs):
            try:
                raise RuntimeError(secret)
            except RuntimeError as cause:
                raise RuntimeError("registration failed") from cause

        mocker.patch("hermes_pipeline.cli._tick_project", side_effect=fail_tick)
        caplog.set_level(logging.ERROR)

        assert _cmd_tick(
            FakeArgs(), Config(projects_dir=projects_dir, state_dir=state_dir)
        ) == 0
        assert secret not in caplog.text
        assert "error_type=RuntimeError" in caplog.text

    def test_tick_prior_in_flight_skips(self, tmp_path, mocker):
        """Prior tick still has in-flight kanban tasks -> skip."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=False
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = projects_dir / "demo" / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

    def test_tick_prior_complete_proceeds(self, tmp_path, mocker):
        """Prior tick complete -> proceed with new selection."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = projects_dir / "demo" / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

    def test_tick_prior_complete_waits_for_open_pr_handoff(self, tmp_path, mocker):
        """A completed Phase 8 handoff with an open PR must block new selection."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_8_finish_branch": "done"},
        )
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.return_value = mocker.Mock(
            returncode=0,
            stdout='{"state": "OPEN", "headRefName": "todo-5-feat"}',
            stderr="",
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        (project_state / "pipeline_branch.txt").write_text("todo-5-feat\n")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_not_called()

    def test_tick_ship_sidecar_waits_for_open_pr_before_completeness(
        self, tmp_path, mocker
    ):
        """A recorded ship handoff must not be masked by a missing gate task."""
        mock_all_complete = mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=False
        )
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.return_value = mocker.Mock(
            returncode=0,
            stdout='{"state": "OPEN", "headRefName": "todo-5-feat"}',
            stderr="",
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        outcomes_dir = project_state / "outcomes"
        outcomes_dir.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        (outcomes_dir / "01HA6PH2V0ZJ7GK0S39D243TQX-ship.json").write_text(
            json.dumps(
                {
                    "tick_id": "01HA6PH2V0ZJ7GK0S39D243TQX",
                    "todo_id": 5,
                    "pr_number": 12,
                    "pr_head_sha": "abc123",
                    "base_branch": "main",
                    "work_branch": "todo-5-feat",
                    "phase_8_task_id": "t_phase8",
                    "bump_version": None,
                }
            )
        )

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_not_called()
        mock_all_complete.assert_not_called()
        mock_run.assert_called_once()

    def test_tick_ship_sidecar_merged_pr_bypasses_missing_gate_task(
        self, tmp_path, mocker
    ):
        """A merged sidecar handoff should release even if phase_9 is absent."""
        mock_all_complete = mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=False
        )
        mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_8_finish_branch": "done"},
        )
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.side_effect = [
            mocker.Mock(
                returncode=0,
                stdout='{"state": "MERGED", "baseRefName": "main", "headRefName": "todo-5-feat"}',
                stderr="",
            ),
            mocker.Mock(returncode=0, stdout="", stderr=""),
            mocker.Mock(returncode=0, stdout="", stderr=""),
            mocker.Mock(returncode=0, stdout="", stderr=""),
            mocker.Mock(returncode=0, stdout="", stderr=""),
            mocker.Mock(returncode=0, stdout="[]", stderr=""),
        ]

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        outcomes_dir = project_state / "outcomes"
        outcomes_dir.mkdir()
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        (outcomes_dir / "01HA6PH2V0ZJ7GK0S39D243TQX-ship.json").write_text(
            json.dumps(
                {
                    "tick_id": "01HA6PH2V0ZJ7GK0S39D243TQX",
                    "todo_id": 5,
                    "pr_number": 12,
                    "pr_head_sha": "abc123",
                    "base_branch": "main",
                    "work_branch": "todo-5-feat",
                    "phase_8_task_id": "t_phase8",
                    "bump_version": None,
                }
            )
        )

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_called_once()
        mock_all_complete.assert_not_called()

    def test_tick_prior_complete_proceeds_after_merged_pr_handoff(self, tmp_path, mocker):
        """Once the handoff PR is merged, the next tick may select new work."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_8_finish_branch": "done"},
        )
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.side_effect = [
            mocker.Mock(
                returncode=0,
                stdout='{"state": "MERGED", "baseRefName": "main", "headRefName": "todo-5-feat"}',
                stderr="",
            ),
            mocker.Mock(returncode=0, stdout="", stderr=""),
            mocker.Mock(returncode=0, stdout="", stderr=""),
            mocker.Mock(returncode=0, stdout="", stderr=""),
            mocker.Mock(returncode=0, stdout="", stderr=""),
            mocker.Mock(returncode=0, stdout="[]", stderr=""),
        ]

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        (project_state / "pipeline_branch.txt").write_text("todo-5-feat\n")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_called_once()
        commands = [call.args[0] for call in mock_run.call_args_list]
        assert ["git", "status", "--porcelain"] in commands
        assert ["git", "fetch", "origin", "main"] in commands
        assert ["git", "checkout", "main"] in commands
        assert ["git", "merge", "--ff-only", "origin/main"] in commands
        assert not (project_state / "pipeline_branch.txt").exists()
        assert (project_state / "current_tick_id.txt").read_text() != "01HA6PH2V0ZJ7GK0S39D243TQX"

    def test_tick_merged_pr_handoff_dirty_checkout_counts_as_no_progress(
        self, tmp_path, mocker
    ):
        """After merge, a dirty checkout must not receive new TODO work."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_8_finish_branch": "done"},
        )
        mock_cb = mocker.MagicMock()
        mocker.patch("hermes_pipeline.cli._make_circuit_breaker", return_value=mock_cb)
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.side_effect = [
            mocker.Mock(
                returncode=0,
                stdout='{"state": "MERGED", "baseRefName": "main", "headRefName": "todo-5-feat"}',
                stderr="",
            ),
            mocker.Mock(returncode=0, stdout=" M local.txt\n", stderr=""),
        ]

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        (project_state / "pipeline_branch.txt").write_text("todo-5-feat\n")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_not_called()
        mock_cb.observe.assert_called_once_with(
            picked=None,
            counts_as_no_progress=True,
        )

    def test_tick_failed_prior_phase_does_not_wait_for_pr_handoff(self, tmp_path, mocker):
        """An early pipeline failure must not be mistaken for a PR handoff."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={
                "phase_2_autoplan": "done",
                "phase_4_development": "failed",
            },
        )
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        (project_state / "pipeline_branch.txt").write_text("todo-5-feat\n")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        gh_pr_view_calls = [
            call for call in mock_run.call_args_list
            if call.args and call.args[0][:3] == ["gh", "pr", "view"]
        ]
        assert gh_pr_view_calls == []
        mock_selection.assert_called_once()

    def test_tick_closed_pr_handoff_counts_as_no_progress(self, tmp_path, mocker):
        """A closed-unmerged handoff should alert through the circuit breaker."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_8_finish_branch": "done"},
        )
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
        mock_cb = mocker.MagicMock()
        mocker.patch("hermes_pipeline.cli._make_circuit_breaker", return_value=mock_cb)
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.return_value = mocker.Mock(
            returncode=0,
            stdout='{"state": "CLOSED", "headRefName": "todo-5-feat"}',
            stderr="",
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        (project_state / "pipeline_branch.txt").write_text("todo-5-feat\n")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_not_called()
        mock_cb.observe.assert_called_once_with(
            picked=None,
            counts_as_no_progress=True,
        )

    def test_tick_missing_pr_handoff_branch_counts_as_no_progress(self, tmp_path, mocker):
        """A completed Phase 8 handoff without branch state must not select new work."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_8_finish_branch": "done"},
        )
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
        mock_cb = mocker.MagicMock()
        mocker.patch("hermes_pipeline.cli._make_circuit_breaker", return_value=mock_cb)
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_not_called()
        mock_cb.observe.assert_called_once_with(
            picked=None,
            counts_as_no_progress=True,
        )

    def test_tick_pr_handoff_rejects_mismatched_head_branch(self, tmp_path, mocker):
        """A stale branch file must not be satisfied by an unrelated PR selector."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch("hermes_pipeline.cli.observe_outcomes")
        mocker.patch(
            "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
            return_value={"phase_8_finish_branch": "done"},
        )
        mocker.patch("hermes_pipeline.ship.maybe_ship_ready")
        mock_cb = mocker.MagicMock()
        mocker.patch("hermes_pipeline.cli._make_circuit_breaker", return_value=mock_cb)
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_run = mocker.patch("hermes_pipeline.cli._cli_sp.run")
        mock_run.return_value = mocker.Mock(
            returncode=0,
            stdout='{"state": "MERGED", "baseRefName": "main", "headRefName": "other-branch"}',
            stderr="",
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = project_dir / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "pipeline.toml").write_text(
            'schema_version = 2\nassignee = "pipeline"\n'
            'capabilities = ["Read", "Write", "Edit", "Bash"]\nprofile = "gstack"\n'
        )
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        (project_state / "pipeline_branch.txt").write_text("todo-5-feat\n")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_not_called()
        mock_cb.observe.assert_called_once_with(
            picked=None,
            counts_as_no_progress=True,
        )

    def test_tick_no_prior_proceeds(self, tmp_path, mocker, caplog):
        """No prior tick -> proceed normally; undeclared profile warns once."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.cli"):
            result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        deprecations = [
            r.getMessage() for r in caplog.records
            if r.levelname == "WARNING" and "deprecated" in r.getMessage()
        ]
        # Exactly one notice: the undeclared case already carries the migration.
        assert deprecations == [
            "project demo: pipeline.toml does not declare a profile; the implicit "
            "default 'gstack' is deprecated. Migrate with: "
            "tpo init demo --force --profile native-sdd"
        ]

    def test_tick_explicit_gstack_profile_warns_deprecated(self, tmp_path, mocker, caplog):
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision())
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            PIPELINE_TOML + 'profile = "gstack"\n'
        )
        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")

        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.cli"):
            assert _cmd_tick(FakeArgs(project="demo"), config) == 0

        deprecations = [
            r.getMessage() for r in caplog.records
            if r.levelname == "WARNING" and "deprecated" in r.getMessage()
        ]
        assert deprecations == [
            "project demo: profile 'gstack' is deprecated; migrate with: "
            "tpo init demo --force --profile native-sdd"
        ]

    def test_tick_native_sdd_profile_emits_no_deprecation(self, tmp_path, mocker, caplog):
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision())
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            PIPELINE_TOML + 'profile = "native-sdd"\n'
        )
        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")

        with caplog.at_level(logging.WARNING, logger="hermes_pipeline.cli"):
            assert _cmd_tick(FakeArgs(project="demo"), config) == 0

        assert not any("deprecated" in r.getMessage() for r in caplog.records)

    def test_tick_without_a_contract_prepares_legacy_implicit_phases(
        self, tmp_path, mocker
    ):
        """No pipeline.toml -> the tick still prepares the legacy implicit
        profile's phases, independent of what new projects now default to."""
        import hermes_pipeline.contract as contract_mod
        from hermes_pipeline.contract import DEFAULT_PROFILE, LEGACY_IMPLICIT_PROFILE
        from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

        # The spy relies on _tick_project importing PipelineContract inside the
        # function: hoisting that import to module scope would leave built empty.
        real_contract = contract_mod.PipelineContract
        built: list = []
        mocker.patch.object(
            contract_mod,
            "PipelineContract",
            side_effect=lambda **kw: built.append(real_contract(**kw)) or built[-1],
        )
        mocker.patch(
            "hermes_pipeline.cli.run_selection",
            return_value=_make_decision(picked="TODO-10"),
        )
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["t_1"],
        )
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        )
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo", contract=False)
        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")

        assert _cmd_tick(FakeArgs(project="demo"), config) == 0

        legacy_keys = [
            phase.phase_key
            for phase in load_phases(resolve_profile_phases_path(LEGACY_IMPLICIT_PROFILE))
        ]
        default_keys = [
            phase.phase_key
            for phase in load_phases(resolve_profile_phases_path(DEFAULT_PROFILE))
        ]
        assert legacy_keys != default_keys  # the regression would be invisible otherwise
        prepared = create.call_args.kwargs["prepared"]
        assert [task.phase_key for task in prepared] == legacy_keys
        # The synthesized contract must name the legacy profile too, so the run
        # is never recorded as DEFAULT_PROFILE while running legacy phases.
        assert built, "the spy saw no PipelineContract construction"
        assert built[0].profile == LEGACY_IMPLICIT_PROFILE

    def test_tick_selection_uses_project_state_dir(self, tmp_path, mocker):
        """Selection decisions are persisted under the project, not global state."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(project="demo"), config)

        assert result == 0
        cfg = mock_selection.call_args.kwargs["cfg"]
        assert cfg.base.state_dir == project_dir / ".hermes"

    def test_tick_lock_held_skips_that_project(self, tmp_path, mocker):
        """A held per-project lock skips that project; scan still returns 0.

        Under the per-project lock model there is no single global lock, so a
        lock held on one project must not abort the whole scan — that project
        is simply skipped (its selection never runs) and the loop continues.
        """
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Hold the per-project lock for "demo" with a fresh (non-stale) holder.
        project_state = projects_dir / "demo" / ".hermes"
        lock_dir = project_state / "tick.lock"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "holder.json").write_text(
            json.dumps({
                "tick_id": "other",
                "acquired_at": "2026-06-16T00:00:00Z",
                "pid": 12345,
            })
        )

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)

        # Scan succeeds overall, but the locked project's selection is skipped.
        assert result == 0
        mock_selection.assert_not_called()

    def test_tick_no_projects(self, tmp_path):
        """No projects found -> return 0."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

    def test_tick_invalid_slug_skipped(self, tmp_path, mocker):
        """Invalid project slug is skipped by discover, tick proceeds."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        # Invalid slug directory
        _create_project(projects_dir, "a;b")
        # Valid project
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

    def test_tick_project_without_contract_skipped(self, tmp_path, mocker):
        """Project without .hermes/pipeline.toml is skipped by discover."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        # Directory without a pipeline contract
        project_dir = projects_dir / "no-contract"
        project_dir.mkdir()
        # Valid project
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

    def test_tick_kanban_registration_failure_project_error(self, tmp_path, mocker):
        """Kanban registration raises RuntimeError -> project error logged, tick returns 0."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        from hermes_pipeline.decision.schema import HermesSelectionDecision
        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=["TODO-10"],
            picked="TODO-10",
            rationale="Selected",
            blocked_reasons={},
            in_flight=[],
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            side_effect=RuntimeError("kanban error"),
        )

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        # Per-project error is caught, tick returns 0 (error isolated)
        assert result == 0

    def test_tick_observe_outcomes_exception(self, tmp_path, mocker):
        """observe_outcomes for prior tick raises -> warning, tick skips."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch(
            "hermes_pipeline.cli.observe_outcomes",
            side_effect=RuntimeError("kanban error"),
        )
        mock_cb = mocker.MagicMock()
        mocker.patch("hermes_pipeline.cli._make_circuit_breaker", return_value=mock_cb)
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = projects_dir / "demo" / ".hermes"
        project_state.mkdir(parents=True, exist_ok=True)
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0
        mock_selection.assert_not_called()
        mock_cb.observe.assert_called_once_with(
            picked=None,
            counts_as_no_progress=True,
        )

    def test_tick_picked_none_writes_sentinel(self, tmp_path, mocker):
        """picked=None -> writes picked_none sentinel in per-project state dir."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        from hermes_pipeline.decision.schema import HermesSelectionDecision
        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=["TODO-10"],
            picked=None,
            rationale="All done",
            blocked_reasons={},
            in_flight=[],
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

        # Check sentinel in per-project state dir
        project_state = projects_dir / "demo" / ".hermes"
        outcomes_dir = project_state / "outcomes"
        assert outcomes_dir.exists()
        sentinel_files = list(outcomes_dir.glob("*-phases.json"))
        assert len(sentinel_files) > 0

        content = sentinel_files[0].read_text().strip()
        data = json.loads(content)
        assert data.get("outcome") == "picked_none"

    def test_tick_picked_none_logs_rationale(self, tmp_path, mocker, caplog):
        """picked=None log includes the decision rationale for CLI debugging."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")

        from hermes_pipeline.decision.schema import HermesSelectionDecision
        mock_selection.return_value = HermesSelectionDecision(
            tick_id="01HB",
            timestamp="2026-01-01T00:00:00Z",
            model="claude-opus-4-7",
            prompt_sha="abc123",
            candidates_considered=[],
            picked=None,
            rationale="No eligible issues are open.",
            blocked_reasons={},
            in_flight=[],
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        with caplog.at_level(logging.INFO, logger="hermes_pipeline.cli"):
            result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        assert "No eligible issues are open." in caplog.text

    def test_tick_project_arg_help(self):
        """tick --help shows the optional project argument."""
        parser = build_parser()
        args = parser.parse_args(["tick"])
        assert args.project is None
        args = parser.parse_args(["tick", "myproject"])
        assert args.project == "myproject"

    def test_tick_invalid_slug_rejected(self, tmp_path, mocker):
        """tick with an invalid slug (e.g., path traversal) returns error code 2."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        args = FakeArgs(project="../etc")
        result = _cmd_tick(args, config)
        assert result == 2

    def test_tick_project_not_found(self, tmp_path, mocker):
        """tick nonexistent-project returns error code 2."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        args = FakeArgs(project="nonexistent")
        result = _cmd_tick(args, config)
        assert result == 2

    def test_tick_project_without_github_origin(self, tmp_path, fake_gh, caplog):
        """tick <project> whose origin is not a github.com remote returns 2."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "empty-project").mkdir()
        fake_gh.on("git", "remote", "get-url", "origin", stdout="git@gitlab.com:a/b.git\n")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        args = FakeArgs(project="empty-project")
        with caplog.at_level("ERROR", logger="hermes_pipeline.cli"):
            result = _cmd_tick(args, config)
        assert result == 2
        assert "origin is not a github.com remote" in caplog.text
        assert "origin_identity_invalid" in caplog.text
        assert not fake_gh.gh_calls()

    def test_tick_project_scoped_tocks_one_project(self, tmp_path, mocker):
        """tick myproject ticks only myproject, not others."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        select_mock = mocker.patch(
            "hermes_pipeline.cli.run_selection",
            return_value=_make_decision(picked="TODO-10"),
        )
        mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["task-001"],
        )

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "alpha")
        _create_project(projects_dir, "beta")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        args = FakeArgs(project="alpha")
        result = _cmd_tick(args, config)
        assert result == 0

        # Only alpha was ticked (selection called once with alpha's project_dir)
        assert select_mock.call_count == 1
        called_ctx = select_mock.call_args.kwargs["ctx"]
        assert called_ctx.project_slug == "alpha"


class TestCliHelpers:
    """Tests for _cmd_tick helper functions."""

    def test_read_prior_tick_id_existing(self, tmp_path):
        """Reads prior tick_id when file exists."""
        from hermes_pipeline.cli import _read_prior_tick_id

        (tmp_path / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")
        result = _read_prior_tick_id(tmp_path)
        assert result == "01HA6PH2V0ZJ7GK0S39D243TQX"

    def test_read_prior_tick_id_missing(self, tmp_path):
        """Returns None when file doesn't exist."""
        from hermes_pipeline.cli import _read_prior_tick_id

        assert not (tmp_path / "current_tick_id.txt").exists()
        result = _read_prior_tick_id(tmp_path)
        assert result is None

    def test_read_prior_tick_id_invalid_json(self, tmp_path):
        """A malformed id names no run directory: treated as a cold start."""
        from hermes_pipeline.cli import _read_prior_tick_id

        (tmp_path / "current_tick_id.txt").write_text("not json {")
        assert _read_prior_tick_id(tmp_path) is None

    def test_generate_tick_id_format(self):
        """_generate_tick_id returns a non-empty string."""
        from hermes_pipeline.cli import _generate_tick_id

        result = _generate_tick_id()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_tick_id_unique(self):
        """_generate_tick_id returns unique values."""
        from hermes_pipeline.cli import _generate_tick_id

        ids = [_generate_tick_id() for _ in range(5)]
        assert len(set(ids)) == 5

    def test_generate_tick_id_fallback(self, mocker):
        """_generate_tick_id falls back to datetime+random if ULID fails."""
        from hermes_pipeline.cli import _generate_tick_id

        mocker.patch("hermes_pipeline.cli._new_tick_id", side_effect=ImportError("no ulid"))

        result = _generate_tick_id()
        assert isinstance(result, str)
        assert len(result) > 10

    def test_persist_tick_id_writes(self, tmp_path):
        """_persist_tick_id writes the tick_id file."""
        from hermes_pipeline.cli import _persist_tick_id

        _persist_tick_id(tmp_path, "01HA6PH2V0ZJ7GK0S39D243TQX")
        content = (tmp_path / "current_tick_id.txt").read_text()
        assert content == "01HA6PH2V0ZJ7GK0S39D243TQX"

    def test_persist_tick_id_oserror(self, tmp_path, mocker):
        """_persist_tick_id raises OSError on write failure."""
        from hermes_pipeline.cli import _persist_tick_id

        mocker.patch("pathlib.Path.write_text", side_effect=OSError("disk full"))

        with pytest.raises(OSError, match="disk full"):
            _persist_tick_id(tmp_path, "01HA6PH2V0ZJ7GK0S39D243TQX")


class TestTickProjectContractWarning:
    def test_tick_project_without_contract_warns_but_runs(self, tmp_path, mocker, caplog):
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision())
        mocker.patch("hermes_pipeline.cli._cli_sp.run", return_value=MagicMock(returncode=0))
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo", contract=False)
        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")

        with caplog.at_level("WARNING", logger="hermes_pipeline.cli"):
            assert _cmd_tick(FakeArgs(project="demo"), config) == 0

        assert any(
            "demo" in r.getMessage() and ".hermes/pipeline.toml" in r.getMessage()
            and "tpo init" in r.getMessage() and r.levelname == "WARNING"
            for r in caplog.records
        )
        # A contract-less project resolves to the same deprecated implicit
        # profile as a contract that omits `profile`, and it is the population
        # that most needs the migration hint (ADR-0004), so it gets the same
        # single notice.
        deprecations = [
            r.getMessage() for r in caplog.records
            if r.levelname == "WARNING" and "deprecated" in r.getMessage()
        ]
        assert deprecations == [
            "project demo: pipeline.toml does not declare a profile; the implicit "
            "default 'gstack' is deprecated. Migrate with: "
            "tpo init demo --force --profile native-sdd"
        ]


class TestTickLegacyPathPlan:
    """A repository-path Plan must spawn cards under a ``requires_plan`` profile."""

    @staticmethod
    def _git(cwd, *args):
        import subprocess

        return subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
        )

    def test_tick_legacy_path_plan_prepares_cards_under_native_sdd(
        self, tmp_path, mocker, fake_gh, caplog
    ):
        """A manifest-free ``### Plan`` repository path is a valid plan source.

        The tick must build its ``PlanReference`` from the compiled plan source
        and prepare cards; it must not record ``failed_to_spawn``.
        """
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            PIPELINE_TOML + 'profile = "native-sdd"\n'
        )
        docs = project_dir / "docs"
        docs.mkdir()
        (docs / "legacy.md").write_text("# Legacy plan\n\nDo the bounded thing.\n")
        self._git(project_dir, "init", "-q", "-b", "main")
        self._git(project_dir, "config", "user.email", "test@example.com")
        self._git(project_dir, "config", "user.name", "Test")
        self._git(project_dir, "add", "docs")
        self._git(project_dir, "commit", "-qm", "base")
        self._git(
            project_dir, "remote", "add", "origin", "https://github.com/acme/repo.git"
        )

        body = (
            "### What\n\nWidget\n\n### Plan\n\ndocs/legacy.md\n\n### Branch\n\nfeat/todo\n"
        )
        seed_project_issues(fake_gh, [todo_payload(10, title="test", body=body)])
        mocker.patch(
            "hermes_pipeline.cli.run_selection",
            return_value=_make_decision(picked="TODO-10"),
        )
        create = mocker.patch(
            "hermes_pipeline.kanban_tasks.create_prepared_todo_phases",
            return_value=["t_1"],
        )
        # ``cli._cli_sp`` IS the ``subprocess`` module, so patching its ``run``
        # would also blind ``run_registration``'s git calls. Only stub ``hermes``.
        import subprocess

        real_run = subprocess.run

        def run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "hermes":
                return MagicMock(returncode=0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=run)
        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")

        with caplog.at_level(logging.ERROR, logger="hermes_pipeline.cli"):
            assert _cmd_tick(FakeArgs(project="demo"), config) == 0

        outcomes = project_dir / ".hermes" / "outcomes"
        recorded = "".join(
            path.read_text() for path in sorted(outcomes.glob("*")) if path.is_file()
        )
        assert "failed_to_spawn" not in recorded, caplog.text
        assert create.called, caplog.text
        prepared = create.call_args.kwargs["prepared"]
        assert prepared, "the tick prepared no phase cards"
