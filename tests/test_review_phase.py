"""Unit tests for the code-owned phase_5_review lifecycle.

These tests operate on a REAL temporary git repo (there is no prior helper
for this in the suite) so that git reset/clean/merge-base behave honestly.
The hermes subprocess and pytest run are injected, never real.
"""
from __future__ import annotations
import subprocess
from pathlib import Path
import pytest
from hermes_pipeline import review_phase as rp


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with `main` holding one commit, checked out on a feature
    branch with one extra commit that adds `feature.py`."""
    d = tmp_path / "proj"
    d.mkdir()
    _run(d, "git", "init", "-q", "-b", "main")
    _run(d, "git", "config", "user.email", "test@example.com")
    _run(d, "git", "config", "user.name", "Test")
    (d / "base.py").write_text("x = 1\n")
    _run(d, "git", "add", ".")
    _run(d, "git", "commit", "-q", "-m", "base")
    _run(d, "git", "checkout", "-q", "-b", "feature")
    (d / "feature.py").write_text("y = 2\n")
    _run(d, "git", "add", ".")
    _run(d, "git", "commit", "-q", "-m", "feature")
    return d


def test_capture_pre_review_state_records_head_and_writes_diff(repo: Path):
    head = _run(repo, "git", "rev-parse", "HEAD")
    pre = rp.capture_pre_review_state(project_dir=repo, todo_id="TODO-7")
    assert pre.head_sha == head
    assert pre.diff_is_empty is False
    diff_path = repo / "docs" / "pipeline" / "TODO-7-pre-review.diff"
    assert diff_path.exists()
    assert "feature.py" in diff_path.read_text()


def test_capture_pre_review_state_empty_diff_when_no_changes_vs_main(repo: Path):
    # Reset feature branch to main so merge-base..HEAD is empty.
    _run(repo, "git", "checkout", "-q", "main")
    _run(repo, "git", "checkout", "-q", "-b", "empty-feature")
    pre = rp.capture_pre_review_state(project_dir=repo, todo_id="TODO-9")
    assert pre.diff_is_empty is True
