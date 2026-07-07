"""Unit tests for the code-owned phase_5_review lifecycle.

These tests operate on a REAL temporary git repo (there is no prior helper
for this in the suite) so that git reset/clean/merge-base behave honestly.
The hermes subprocess and pytest run are injected, never real.
"""
from __future__ import annotations
import json
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


def test_restore_worktree_reverts_tracked_and_removes_untracked(repo: Path):
    head = _run(repo, "git", "rev-parse", "HEAD")
    # Simulate a review that edited a tracked file and added a new one.
    (repo / "feature.py").write_text("y = 2  # bad edit\n")
    (repo / "junk.txt").write_text("scratch\n")
    _run(repo, "git", "add", "feature.py")  # stage the bad edit
    rp.restore_worktree(project_dir=repo, head_sha=head)
    assert _run(repo, "git", "rev-parse", "HEAD") == head
    assert (repo / "feature.py").read_text() == "y = 2\n"
    assert not (repo / "junk.txt").exists()
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert porcelain == "", porcelain


def test_restore_worktree_moves_head_back_after_extra_commit(repo: Path):
    head = _run(repo, "git", "rev-parse", "HEAD")
    (repo / "feature.py").write_text("y = 3\n")
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-q", "-m", "review fix")
    assert _run(repo, "git", "rev-parse", "HEAD") != head
    rp.restore_worktree(project_dir=repo, head_sha=head)
    assert _run(repo, "git", "rev-parse", "HEAD") == head
    assert (repo / "feature.py").read_text() == "y = 2\n"


def test_run_pytest_invokes_uv_run_pytest(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")

        class R:
            returncode = 0
            stdout = "1 passed"
            stderr = ""
        return R()

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    res = rp.run_pytest(project_dir=tmp_path)
    assert seen["cmd"] == ["uv", "run", "pytest"]
    assert str(tmp_path) == str(seen["cwd"])
    assert res.returncode == 0
    assert res.stdout == "1 passed"


def test_write_review_artifacts_clean_includes_post_diff(repo: Path):
    rp.write_review_artifacts(
        project_dir=repo, todo_id="TODO-7", outcome=rp.OUTCOME_CLEAN,
        findings_text="- fixed off-by-one in feature.py", include_post_diff=True,
    )
    docs = repo / "docs" / "pipeline"
    assert (docs / "TODO-7-review-findings.md").read_text().startswith("- fixed")
    assert (docs / "TODO-7-post-review.diff").exists()
    outcome = json.loads((docs / "TODO-7-review-outcome.json").read_text())
    assert outcome["outcome"] == rp.OUTCOME_CLEAN
    assert outcome["todo_id"] == "TODO-7"


def test_write_review_artifacts_reverted_omits_post_diff(repo: Path):
    rp.write_review_artifacts(
        project_dir=repo, todo_id="TODO-7", outcome=rp.OUTCOME_REVERTED,
        findings_text="tests failed; changes reverted", include_post_diff=False,
    )
    docs = repo / "docs" / "pipeline"
    assert not (docs / "TODO-7-post-review.diff").exists()
    assert json.loads((docs / "TODO-7-review-outcome.json").read_text())["outcome"] == rp.OUTCOME_REVERTED


def test_commit_all_creates_commit(repo: Path):
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "note.txt").write_text("hi\n")
    before = _run(repo, "git", "rev-parse", "HEAD")
    rp.commit_all(project_dir=repo, todo_id="TODO-7", message="review: address findings for TODO-7")
    after = _run(repo, "git", "rev-parse", "HEAD")
    assert before != after
    assert "review: address findings for TODO-7" in _run(repo, "git", "log", "-1", "--pretty=%s")
