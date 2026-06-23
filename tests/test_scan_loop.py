import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from hermes_pipeline.config import Config
from hermes_pipeline.cli import _cmd_tick, build_parser


class FakeArgs:
    """Minimal argparse.Namespace for testing."""
    def __init__(self, **kwargs):
        kwargs.setdefault("project", None)
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_decision(picked=None):
    """Create a mock HermesSelectionDecision with the right shape."""
    decision = MagicMock()
    decision.picked = picked
    decision.rationale = "test rationale"
    decision.candidates_considered = []
    return decision


def test_tick_scans_multiple_projects(tmp_path: Path):
    """tick should iterate over discovered projects and run selection for each."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    pb = projects_dir / "project-b"
    pb.mkdir()
    (pb / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(*, tick_id, ctx, cfg):
        selection_calls.append(ctx.project_slug)
        return _make_decision()

    args = FakeArgs()

    with patch("hermes_pipeline.cli.run_selection", mock_selection):
        exit_code = _cmd_tick(args, config)

    assert exit_code == 0
    assert "project-a" in selection_calls
    assert "project-b" in selection_calls


def test_tick_skips_disabled_projects(tmp_path: Path):
    """tick should skip projects with enabled=false in project.toml."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    pb = projects_dir / "project-b"
    pb.mkdir()
    (pb / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")
    pb_hermes = pb / ".hermes"
    pb_hermes.mkdir()
    (pb_hermes / "project.toml").write_text("[active]\nenabled = false\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(*, tick_id, ctx, cfg):
        selection_calls.append(ctx.project_slug)
        return _make_decision()

    args = FakeArgs()

    with patch("hermes_pipeline.cli.run_selection", mock_selection):
        exit_code = _cmd_tick(args, config)

    assert exit_code == 0
    assert "project-a" in selection_calls
    assert "project-b" not in selection_calls


def test_tick_error_isolation(tmp_path: Path):
    """A project error should be logged and not block other projects."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    pb = projects_dir / "project-b"
    pb.mkdir()
    (pb / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(*, tick_id, ctx, cfg):
        if ctx.project_slug == "project-a":
            raise RuntimeError("simulated error in project-a")
        selection_calls.append(ctx.project_slug)
        return _make_decision()

    args = FakeArgs()

    with patch("hermes_pipeline.cli.run_selection", mock_selection):
        exit_code = _cmd_tick(args, config)

    assert exit_code == 0
    assert "project-b" in selection_calls


def test_tick_uses_per_project_state_dir(tmp_path: Path):
    """tick should use <project>/.hermes/ for per-project state files."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    state_dirs_seen = []

    def mock_context(*, state_dir, **kwargs):
        state_dirs_seen.append(state_dir)
        ctx = MagicMock()
        ctx.project_slug = "project-a"
        return ctx

    def mock_selection(*, tick_id, ctx, cfg):
        return _make_decision()

    args = FakeArgs()

    with (
        patch("hermes_pipeline.cli.build_context", mock_context),
        patch("hermes_pipeline.cli.run_selection", mock_selection),
    ):
        _cmd_tick(args, config)

    assert any("project-a" in str(sd) for sd in state_dirs_seen)


def test_tick_performs_state_migration(tmp_path: Path):
    """tick should migrate global state to per-project dirs before scanning."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    pa = projects_dir / "project-a"
    pa.mkdir()
    (pa / "TODOS.md").write_text("# TODOS\n\nTODO-1 — First task\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "current_tick_id.txt").write_text("old-tick-123\n")

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    def mock_selection(*, tick_id, ctx, cfg):
        return _make_decision()

    args = FakeArgs()

    with patch("hermes_pipeline.cli.run_selection", mock_selection):
        _cmd_tick(args, config)

    # Copy (not move) — global state remains after migration
    assert (state_dir / "current_tick_id.txt").exists()
    assert (pa / ".hermes" / "current_tick_id.txt").exists()
    assert (pa / ".hermes" / "current_tick_id.txt").read_text().strip() == "old-tick-123"
