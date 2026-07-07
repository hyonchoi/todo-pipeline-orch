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


def restore_worktree(*, project_dir, head_sha: str) -> None:
    """Deterministically restore the tree to head_sha.

    `reset --hard` reverts tracked changes and moves HEAD; `clean -fd` removes
    untracked files/dirs the review may have created. Plain `clean -fd` does
    NOT remove gitignored files (e.g. .hermes/), which is intentional.
    """
    project_dir = Path(project_dir)
    _git(project_dir, "reset", "--hard", head_sha)
    _git(project_dir, "clean", "-fd")


def capture_pre_review_state(*, project_dir, todo_id: str, base_ref: str = "main") -> PreReviewState:
    """Snapshot HEAD and save the pre-review diff. Called BEFORE hermes runs."""
    project_dir = Path(project_dir)
    head_sha = _git(project_dir, "rev-parse", "HEAD").stdout.strip()
    base = _diff_base(project_dir, base_ref)
    diff = _git(project_dir, "diff", base, "HEAD").stdout
    out = _docs_dir(project_dir) / f"{todo_id}-pre-review.diff"
    out.write_text(diff)
    return PreReviewState(head_sha=head_sha, diff_is_empty=(diff.strip() == ""))


@dataclass(frozen=True)
class PytestResult:
    returncode: int
    stdout: str
    stderr: str


def run_pytest(*, project_dir, timeout: int = 1200) -> PytestResult:
    """Run the project's test suite via `uv run pytest`."""
    proc = subprocess.run(
        ["uv", "run", "pytest"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return PytestResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def write_review_artifacts(
    *,
    project_dir,
    todo_id: str,
    outcome: str,
    findings_text: str,
    base_ref: str = "main",
    include_post_diff: bool,
) -> None:
    """Write the committed review artifacts CSO and the human PR read."""
    project_dir = Path(project_dir)
    docs = _docs_dir(project_dir)
    (docs / f"{todo_id}-review-findings.md").write_text(findings_text)
    (docs / f"{todo_id}-review-outcome.json").write_text(
        json.dumps({"todo_id": todo_id, "outcome": outcome}, sort_keys=True)
    )
    if include_post_diff:
        base = _diff_base(project_dir, base_ref)
        post = _git(project_dir, "diff", base, "HEAD").stdout
        (docs / f"{todo_id}-post-review.diff").write_text(post)


def commit_all(*, project_dir, todo_id: str, message: str) -> None:
    """Stage everything and commit. Safe no-op if the tree is already clean."""
    project_dir = Path(project_dir)
    _git(project_dir, "add", "-A")
    status = _git(project_dir, "status", "--porcelain").stdout.strip()
    if not status:
        log.info("commit_all: nothing to commit for %s", todo_id)
        return
    _git(project_dir, "commit", "-m", message)


def _verify_review_success(*, project_dir, todo_id: str, expect_post_diff: bool) -> None:
    """Machine check that success is real: tree clean + artifacts present.

    Success == these checks pass, NOT just that a subprocess exited 0.
    """
    project_dir = Path(project_dir)
    porcelain = _git(project_dir, "status", "--porcelain").stdout.strip()
    if porcelain:
        raise RuntimeError(f"review verify failed: worktree not clean:\n{porcelain}")
    docs = _docs_dir(project_dir)
    required = [f"{todo_id}-review-outcome.json", f"{todo_id}-review-findings.md"]
    if expect_post_diff:
        required.append(f"{todo_id}-post-review.diff")
    missing = [name for name in required if not (docs / name).exists()]
    if missing:
        raise RuntimeError(f"review verify failed: missing artifacts: {missing}")


def finalize_review(
    *,
    project_dir,
    todo_id: str,
    pre_state: PreReviewState,
    hermes_result: dict,
    base_ref: str = "main",
    pytest_runner=run_pytest,
) -> dict:
    """POST decision: commit fixes on pass, restore on fail/timeout, verify."""
    project_dir = Path(project_dir)
    timed_out = hermes_result.get("timed_out", False)
    rc = hermes_result.get("returncode", 0)

    # Path 1: hermes timed out or errored — restore, record, fail cleanly.
    if timed_out or rc != 0:
        restore_worktree(project_dir=project_dir, head_sha=pre_state.head_sha)
        write_review_artifacts(
            project_dir=project_dir, todo_id=todo_id, outcome=OUTCOME_TIMEOUT,
            findings_text=(
                f"Review phase failed (timed_out={timed_out}, rc={rc}); "
                f"worktree restored to {pre_state.head_sha}."
            ),
            include_post_diff=False,
        )
        commit_all(project_dir=project_dir, todo_id=todo_id,
                   message=f"review: rollback (timeout/error) for {todo_id}")
        _verify_review_success(project_dir=project_dir, todo_id=todo_id, expect_post_diff=False)
        raise RuntimeError(
            f"phase_5_review failed: timed_out={timed_out}, rc={rc}; worktree restored."
        )

    # hermes succeeded — run tests to decide keep vs revert.
    pyres = pytest_runner(project_dir=project_dir)
    if pyres.returncode == 0:
        write_review_artifacts(
            project_dir=project_dir, todo_id=todo_id, outcome=OUTCOME_CLEAN,
            findings_text=f"Review applied and tests pass.\n\n{hermes_result.get('stdout', '')[:2000]}",
            base_ref=base_ref, include_post_diff=True,
        )
        commit_all(project_dir=project_dir, todo_id=todo_id,
                   message=f"review: address findings for {todo_id}")
        _verify_review_success(project_dir=project_dir, todo_id=todo_id, expect_post_diff=True)
        return {"status": "success", "phase_key": REVIEW_PHASE_KEY, "outcome": OUTCOME_CLEAN}

    # Tests failed — revert, record, but complete the phase.
    restore_worktree(project_dir=project_dir, head_sha=pre_state.head_sha)
    write_review_artifacts(
        project_dir=project_dir, todo_id=todo_id, outcome=OUTCOME_REVERTED,
        findings_text=(
            f"Review fixes reverted: tests failed after applying them.\n\n"
            f"pytest rc={pyres.returncode}\n{pyres.stdout[-2000:]}"
        ),
        include_post_diff=False,
    )
    commit_all(project_dir=project_dir, todo_id=todo_id,
               message=f"review: rollback test failure for {todo_id}")
    _verify_review_success(project_dir=project_dir, todo_id=todo_id, expect_post_diff=False)
    return {"status": "success", "phase_key": REVIEW_PHASE_KEY, "outcome": OUTCOME_REVERTED}
