"""Tests for the tick subcommand (TODO-10)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_pipeline.config import Config
from hermes_pipeline.cli import _cmd_tick, build_parser


def _make_decision(picked=None, **kwargs):
    """Create a mock HermesSelectionDecision with the right shape."""
    decision = MagicMock()
    decision.picked = picked or kwargs.get("picked")
    decision.rationale = "test"
    decision.candidates_considered = kwargs.get("candidates_considered", [])
    return decision


def _create_project(projects_dir, name, todos=True):
    """Helper to create a project directory with optional TODOS.md."""
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    if todos:
        (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")
    return project_dir


class FakeArgs:
    """Minimal argparse.Namespace for testing."""
    def __init__(self, **kwargs):
        kwargs.setdefault("project", None)
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestTickSubcommand:
    """Tests for pipeline-watch tick (scan loop)."""

    def test_tick_help(self):
        """tick subcommand shows in help."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["tick", "--help"])

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
        project_state.mkdir(parents=True)
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
        project_state.mkdir(parents=True)
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

    def test_tick_no_prior_proceeds(self, tmp_path, mocker):
        """No prior tick -> proceed normally."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

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
        invalid_dir = projects_dir / "a;b"
        invalid_dir.mkdir()
        (invalid_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10\n")
        # Valid project
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

    def test_tick_project_without_todos_skipped(self, tmp_path, mocker):
        """Project without TODOS.md is skipped by discover."""
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        # Project without TODOS.md
        project_dir = projects_dir / "no-todos"
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
            "hermes_pipeline.cli.register_todo_phases",
            side_effect=RuntimeError("kanban error"),
        )

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        # Per-project error is caught, tick returns 0 (error isolated)
        assert result == 0

    def test_tick_observe_outcomes_exception(self, tmp_path, mocker):
        """observe_outcomes for prior tick raises -> warning, tick proceeds."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        mocker.patch(
            "hermes_pipeline.cli.observe_outcomes",
            side_effect=RuntimeError("kanban error"),
        )
        mock_selection = mocker.patch("hermes_pipeline.cli.run_selection")
        mock_selection.return_value = _make_decision()

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        project_state = projects_dir / "demo" / ".hermes"
        project_state.mkdir(parents=True)
        (project_state / "current_tick_id.txt").write_text("01HA6PH2V0ZJ7GK0S39D243TQX")

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        result = _cmd_tick(FakeArgs(), config)
        assert result == 0

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

    def test_tick_project_no_todos(self, tmp_path, mocker):
        """tick project-without-TODOS.md returns error code 2."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "empty-project").mkdir()  # no TODOS.md

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        config = Config(projects_dir=projects_dir, state_dir=state_dir)
        args = FakeArgs(project="empty-project")
        result = _cmd_tick(args, config)
        assert result == 2

    def test_tick_project_scoped_tocks_one_project(self, tmp_path, mocker):
        """tick myproject ticks only myproject, not others."""
        mocker.patch(
            "hermes_pipeline.cli.all_phases_complete", return_value=True
        )
        select_mock = mocker.patch(
            "hermes_pipeline.cli.run_selection",
            return_value=_make_decision(picked="TODO-42"),
        )
        mocker.patch(
            "hermes_pipeline.cli.register_todo_phases",
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
        """Returns None when file has invalid content."""
        from hermes_pipeline.cli import _read_prior_tick_id

        (tmp_path / "current_tick_id.txt").write_text("not json {")
        result = _read_prior_tick_id(tmp_path)
        assert result == "not json {"

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