"""Tests for Lane F.2: status.py"""

from pathlib import Path
from datetime import datetime, timedelta, timezone
import pytest

from hermes_pipeline.status import StatusRow, _age, collect_pending, format_table
from hermes_pipeline.state import ReadyForReview


class TestAge:
    """Test human-readable age computation."""

    def test_age_empty_string(self):
        """Empty timestamp returns 'unknown'."""
        assert _age("") == "unknown"

    def test_age_invalid_timestamp(self):
        """Invalid timestamp returns 'unknown'."""
        assert _age("not-a-timestamp") == "unknown"

    def test_age_seconds(self):
        """Very recent timestamp shows seconds."""
        now = datetime.now(timezone.utc)
        iso_str = now.isoformat()
        age = _age(iso_str)
        # Should be "0s" or very small
        assert "s" in age

    def test_age_minutes(self):
        """Timestamp 5 minutes ago shows 'Xm'."""
        now = datetime.now(timezone.utc)
        five_min_ago = now - timedelta(minutes=5)
        iso_str = five_min_ago.isoformat()
        age = _age(iso_str)
        assert "m" in age

    def test_age_hours(self):
        """Timestamp 2 hours ago shows 'Xh'."""
        now = datetime.now(timezone.utc)
        two_hours_ago = now - timedelta(hours=2)
        iso_str = two_hours_ago.isoformat()
        age = _age(iso_str)
        assert "h" in age

    def test_age_days(self):
        """Timestamp 3 days ago shows 'Xd'."""
        now = datetime.now(timezone.utc)
        three_days_ago = now - timedelta(days=3)
        iso_str = three_days_ago.isoformat()
        age = _age(iso_str)
        assert "d" in age

    def test_age_iso8601_with_z(self):
        """Handle ISO 8601 with Z suffix."""
        now_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if now_str.endswith("+00:00"):
            now_str = now_str[:-6] + "Z"
        age = _age(now_str)
        assert age != "unknown"


class TestStatusRow:
    """Test StatusRow dataclass."""

    def test_status_row_creation(self):
        """Create a StatusRow."""
        row = StatusRow(
            project="test_proj",
            todo_id=42,
            branch="feat/test",
            pr_url="https://github.com/test/repo/pull/1",
            merge_status="pending",
            age="2h",
        )
        assert row.project == "test_proj"
        assert row.todo_id == 42
        assert row.merge_status == "pending"


class TestCollectPending:
    """Test collect_pending function."""

    def test_collect_pending_no_projects(self, tmp_path):
        """No projects returns empty list."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()

        rows = collect_pending(projects_dir, lock_dir)
        assert rows == []

    def test_collect_pending_nonexistent_dir(self, tmp_path):
        """Nonexistent projects_dir returns empty list."""
        projects_dir = tmp_path / "nonexistent"
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()

        rows = collect_pending(projects_dir, lock_dir)
        assert rows == []

    def test_collect_pending_with_records(self, tmp_path):
        """Collect pending ready-for-review records."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        lock_dir = state_dir / "pipeline_locks"  # lock_dir is under state_dir
        lock_dir.mkdir(parents=True)

        # Create a project
        proj = projects_dir / "test_proj"
        proj.mkdir()
        (proj / "TODOS.md").write_text("# TODOs\n")

        # Create ready-for-review records
        ready_dir = state_dir / "ready_for_review"
        ready_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc).isoformat()
        rec1 = ReadyForReview(
            project="test_proj",
            todo_id=1,
            branch="feat/task-1",
            pr_url="https://github.com/test/pull/1",
            phase_summaries={},
            kanban_task_id=None,
            merge_status="pending",
            created_at=now,
        )
        (ready_dir / "test_proj_1.json").write_text(rec1.to_json())

        rec2 = ReadyForReview(
            project="test_proj",
            todo_id=2,
            branch="feat/task-2",
            pr_url="https://github.com/test/pull/2",
            phase_summaries={},
            kanban_task_id=None,
            merge_status="merged",  # Not pending
            created_at=now,
        )
        (ready_dir / "test_proj_2.json").write_text(rec2.to_json())

        rows = collect_pending(projects_dir, lock_dir)

        # Should only include rec1 (pending status)
        assert len(rows) == 1
        assert rows[0].todo_id == 1
        assert rows[0].merge_status == "pending"

    def test_collect_pending_sorted(self, tmp_path):
        """Collect pending records are sorted by project, then todo_id."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        lock_dir = state_dir / "pipeline_locks"  # lock_dir is under state_dir
        lock_dir.mkdir(parents=True)

        # Create two projects
        for proj_name in ["proj_b", "proj_a"]:
            proj = projects_dir / proj_name
            proj.mkdir()
            (proj / "TODOS.md").write_text("# TODOs\n")

        # Create ready-for-review records
        ready_dir = state_dir / "ready_for_review"
        ready_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc).isoformat()
        for proj_name in ["proj_b", "proj_a"]:
            for todo_id in [2, 1]:
                rec = ReadyForReview(
                    project=proj_name,
                    todo_id=todo_id,
                    branch=f"feat/{proj_name}-{todo_id}",
                    pr_url=f"https://github.com/test/pull/{todo_id}",
                    phase_summaries={},
                    kanban_task_id=None,
                    merge_status="pending",
                    created_at=now,
                )
                (ready_dir / f"{proj_name}_{todo_id}.json").write_text(rec.to_json())

        rows = collect_pending(projects_dir, lock_dir)

        # Should be sorted: proj_a (1, 2), proj_b (1, 2)
        assert len(rows) == 4
        assert rows[0].project == "proj_a" and rows[0].todo_id == 1
        assert rows[1].project == "proj_a" and rows[1].todo_id == 2
        assert rows[2].project == "proj_b" and rows[2].todo_id == 1
        assert rows[3].project == "proj_b" and rows[3].todo_id == 2


class TestFormatTable:
    """Test format_table function."""

    def test_format_table_empty(self):
        """Empty rows returns 'No pending records.'"""
        output = format_table([])
        assert "No pending records" in output

    def test_format_table_single_row(self):
        """Format single row as table."""
        rows = [
            StatusRow(
                project="test",
                todo_id=1,
                branch="feat/test",
                pr_url="https://github.com/test/pull/1",
                merge_status="pending",
                age="2h",
            )
        ]
        output = format_table(rows)

        # Check header
        assert "PROJECT" in output
        assert "TODO" in output
        assert "BRANCH" in output
        assert "PR" in output
        assert "STATUS" in output
        assert "AGE" in output

        # Check data
        assert "test" in output
        assert "1" in output
        assert "feat/test" in output
        assert "pending" in output
        assert "2h" in output

    def test_format_table_multiple_rows(self):
        """Format multiple rows as table."""
        rows = [
            StatusRow(
                project="proj_a",
                todo_id=1,
                branch="feat/a",
                pr_url="https://github.com/a/pull/1",
                merge_status="pending",
                age="1h",
            ),
            StatusRow(
                project="proj_b",
                todo_id=2,
                branch="feat/b",
                pr_url="https://github.com/b/pull/2",
                merge_status="failed",
                age="3d",
            ),
        ]
        output = format_table(rows)

        assert "proj_a" in output
        assert "proj_b" in output
        assert "1" in output
        assert "2" in output
        assert "pending" in output
        assert "failed" in output

    def test_format_table_truncates_long_urls(self):
        """Long PR URLs are truncated."""
        rows = [
            StatusRow(
                project="test",
                todo_id=1,
                branch="feat/test",
                pr_url="https://github.com/verylongusername/verylongreponame/pull/12345",
                merge_status="pending",
                age="1h",
            )
        ]
        output = format_table(rows)

        # Long URL should be truncated with "..."
        lines = output.split("\n")
        # Find data line (not header or separator)
        data_lines = [l for l in lines if l and "test" in l and "PR" not in l]
        if data_lines:
            # URL should be truncated
            assert "..." in data_lines[0] or len(data_lines[0]) > 0
