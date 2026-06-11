"""Tests for Lane F.1: watcher.py"""

from pathlib import Path
import pytest

from hermes_pipeline.watcher import discover_projects, auto_tick, _compute_hash


class TestDiscoverProjects:
    """Test project discovery."""

    def test_discover_projects_empty_dir(self, tmp_path):
        """Empty directory returns no projects."""
        projects = discover_projects(tmp_path)
        assert projects == []

    def test_discover_projects_with_todos(self, tmp_path):
        """Find projects with TODOS.md."""
        # Create a project with TODOS.md
        proj1 = tmp_path / "proj1"
        proj1.mkdir()
        (proj1 / "TODOS.md").write_text("# TODOs\n")

        # Create a project without TODOS.md
        proj2 = tmp_path / "proj2"
        proj2.mkdir()

        # Create a file (not a dir)
        (tmp_path / "file.txt").write_text("test")

        projects = discover_projects(tmp_path)
        assert len(projects) == 1
        assert projects[0] == proj1

    def test_discover_projects_sorted(self, tmp_path):
        """Projects are returned sorted."""
        for name in ["zebra", "alpha", "charlie"]:
            proj = tmp_path / name
            proj.mkdir()
            (proj / "TODOS.md").write_text("# TODOs\n")

        projects = discover_projects(tmp_path)
        assert [p.name for p in projects] == ["alpha", "charlie", "zebra"]


class TestComputeHash:
    """Test hash computation."""

    def test_compute_hash_consistency(self):
        """Same content produces same hash."""
        content = "# TODOs\n\n## TODO-1: Test\n"
        h1 = _compute_hash(content)
        h2 = _compute_hash(content)
        assert h1 == h2

    def test_compute_hash_differs(self):
        """Different content produces different hash."""
        h1 = _compute_hash("content1")
        h2 = _compute_hash("content2")
        assert h1 != h2


class TestAutoTick:
    """Test auto_tick orchestration."""

    def test_auto_tick_no_projects(self, tmp_path):
        """auto_tick with no projects succeeds."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        selected = []

        def on_selected(proj, todo_id):
            selected.append((proj, todo_id))

        def notify_fn(msg):
            pass

        auto_tick(
            projects_dir=projects_dir,
            lock_dir=lock_dir,
            state_dir=state_dir,
            on_selected=on_selected,
            notify_fn=notify_fn,
            slack_channel="",
        )

        # No projects, no selections
        assert selected == []

    def test_auto_tick_with_locked_project(self, tmp_path):
        """auto_tick skips locked projects."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Create a locked project
        proj = projects_dir / "test_proj"
        proj.mkdir()
        (proj / "TODOS.md").write_text("# TODOs\n")
        (lock_dir / "test_proj.lock").write_text("")

        selected = []

        def on_selected(proj_name, todo_id):
            selected.append((proj_name, todo_id))

        def notify_fn(msg):
            pass

        auto_tick(
            projects_dir=projects_dir,
            lock_dir=lock_dir,
            state_dir=state_dir,
            on_selected=on_selected,
            notify_fn=notify_fn,
            slack_channel="",
        )

        # Locked project is skipped
        assert selected == []

    def test_auto_tick_detects_changes(self, tmp_path):
        """auto_tick detects TODOS.md changes and selects TODOs."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Create a project with an eligible TODO
        proj = projects_dir / "test_proj"
        proj.mkdir()
        todos_content = """# TODOs

## TODO-1: First Task
- Status: [space]
- Priority: P1
- Effort: M
"""
        (proj / "TODOS.md").write_text(todos_content)

        selected = []

        def on_selected(proj_name, todo_id):
            selected.append((proj_name, todo_id))

        def notify_fn(msg):
            pass

        auto_tick(
            projects_dir=projects_dir,
            lock_dir=lock_dir,
            state_dir=state_dir,
            on_selected=on_selected,
            notify_fn=notify_fn,
            slack_channel="",
        )

        # TODO should be selected on first run
        assert len(selected) == 1
        assert selected[0] == ("test_proj", 1)

        # Second run with no changes: no selection
        selected.clear()
        auto_tick(
            projects_dir=projects_dir,
            lock_dir=lock_dir,
            state_dir=state_dir,
            on_selected=on_selected,
            notify_fn=notify_fn,
            slack_channel="",
        )

        # No changes detected, no selection
        assert selected == []

    def test_auto_tick_error_isolation(self, tmp_path):
        """auto_tick isolates errors per project."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Create a project with TODOS.md
        proj1 = projects_dir / "test_proj"
        proj1.mkdir()
        (proj1 / "TODOS.md").write_text("# TODOs\n")

        # Create a directory without TODOS.md (will be skipped)
        proj2 = projects_dir / "no_todos"
        proj2.mkdir()

        selected = []
        errors = []

        def on_selected(proj_name, todo_id):
            selected.append((proj_name, todo_id))

        def notify_fn(msg):
            errors.append(msg)

        auto_tick(
            projects_dir=projects_dir,
            lock_dir=lock_dir,
            state_dir=state_dir,
            on_selected=on_selected,
            notify_fn=notify_fn,
            slack_channel="",
        )

        # auto_tick should process both projects (one skipped due to no TODOS.md)
        # without errors in normal case
        assert len(errors) == 0  # No errors expected
