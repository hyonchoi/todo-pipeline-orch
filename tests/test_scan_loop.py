from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_pipeline.cli import _cmd_tick
from hermes_pipeline.config import Config
from tests.gh_fakes import seed_project_issues, todo_payload

PIPELINE_TOML = (
    'schema_version = 2\nassignee = "default"\n'
    'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
)


@pytest.fixture(autouse=True)
def _github_todo_1(fake_gh):
    """Every tick reads TODOs from GitHub: serve #1 for every scanned project."""
    return seed_project_issues(fake_gh, [todo_payload(1, title="First task")])


def _project(projects_dir: Path, name: str) -> Path:
    project_dir = projects_dir / name
    (project_dir / ".hermes").mkdir(parents=True)
    (project_dir / ".hermes" / "pipeline.toml").write_text(PIPELINE_TOML)
    return project_dir


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

    pa = _project(projects_dir, "project-a")

    pb = _project(projects_dir, "project-b")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(*, tick_id, ctx, cfg, timeout=None, eligible_todo_ids=None):
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

    pa = _project(projects_dir, "project-a")

    pb = _project(projects_dir, "project-b")
    pb_hermes = pb / ".hermes"
    (pb_hermes / "project.toml").write_text("[active]\nenabled = false\n")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(*, tick_id, ctx, cfg, timeout=None, eligible_todo_ids=None):
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

    pa = _project(projects_dir, "project-a")

    pb = _project(projects_dir, "project-b")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    selection_calls = []

    def mock_selection(*, tick_id, ctx, cfg, timeout=None, eligible_todo_ids=None):
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

    pa = _project(projects_dir, "project-a")

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    state_dirs_seen = []

    def mock_context(*, state_dir, **kwargs):
        state_dirs_seen.append(state_dir)
        ctx = MagicMock()
        ctx.project_slug = "project-a"
        return ctx

    def mock_selection(*, tick_id, ctx, cfg, timeout=None, eligible_todo_ids=None):
        return _make_decision()

    args = FakeArgs()

    with (
        patch("hermes_pipeline.cli.build_context", mock_context),
        patch("hermes_pipeline.cli.run_selection", mock_selection),
    ):
        _cmd_tick(args, config)

    assert any("project-a" in str(sd) for sd in state_dirs_seen)


def test_tick_never_copies_global_state_into_a_project(tmp_path: Path):
    """Hard cutover: the legacy global-state migration is gone."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    pa = _project(projects_dir, "project-a")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "current_tick_id.txt").write_text("old-tick-123\n")
    config = Config(projects_dir=projects_dir, state_dir=state_dir)

    with patch("hermes_pipeline.cli.run_selection", lambda **kwargs: _make_decision()):
        _cmd_tick(FakeArgs(), config)

    assert (pa / ".hermes" / "current_tick_id.txt").read_text().strip() != "old-tick-123"
    import importlib.util

    assert importlib.util.find_spec("hermes_pipeline.state_migration") is None
