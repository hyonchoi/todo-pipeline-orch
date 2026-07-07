"""Code-owned lifecycle for phase_5_review (TODO-7, Approach C).

The gstack /review skill runs unattended and edits the working tree. This
module owns everything the LLM prompt must NOT: snapshotting HEAD, running
tests, deterministically committing fixes on pass or restoring the tree on
fail/timeout, and machine-verifying the outcome. CSO (phase_6_1) always
inherits a clean, known worktree.
"""
from __future__ import annotations
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

REVIEW_PHASE_KEY = "phase_5_review"

OUTCOME_CLEAN = "review_clean"
OUTCOME_REVERTED = "review_reverted_test_failure"
OUTCOME_TIMEOUT = "review_timeout"
OUTCOME_NO_DIFF = "review_skipped_no_diff"

_PIPELINE_DOCS = ("docs", "pipeline")


@dataclass(frozen=True)
class PreReviewState:
    head_sha: str
    diff_is_empty: bool


def _git(project_dir, *args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a git command in project_dir, capturing text output."""
    return subprocess.run(
        ["git", *args],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _docs_dir(project_dir: Path) -> Path:
    d = Path(project_dir).joinpath(*_PIPELINE_DOCS)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _diff_base(project_dir, base_ref: str = "main") -> str:
    """The merge-base of HEAD and base_ref — the correct diff base on any
    branch topology (never a bare `git diff main`)."""
    return _git(project_dir, "merge-base", "HEAD", base_ref).stdout.strip()


def capture_pre_review_state(*, project_dir, todo_id: str, base_ref: str = "main") -> PreReviewState:
    """Snapshot HEAD and save the pre-review diff. Called BEFORE hermes runs."""
    project_dir = Path(project_dir)
    head_sha = _git(project_dir, "rev-parse", "HEAD").stdout.strip()
    base = _diff_base(project_dir, base_ref)
    diff = _git(project_dir, "diff", base, "HEAD").stdout
    out = _docs_dir(project_dir) / f"{todo_id}-pre-review.diff"
    out.write_text(diff)
    return PreReviewState(head_sha=head_sha, diff_is_empty=(diff.strip() == ""))
