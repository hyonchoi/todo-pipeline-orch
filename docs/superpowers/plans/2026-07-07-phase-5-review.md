# Phase 5 Review — Code-Owned Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert a `phase_5_review` phase between development and CSO that runs the gstack `/review` skill autonomously, then deterministically commits-or-rolls-back the fixes in Python code (not the LLM prompt), giving CSO a clean, known worktree in every branch.

**Architecture:** The phase prompt is instruction-only ("run /review with autonomous fix mode"). A new `hermes_pipeline/review_phase.py` module owns the lifecycle: PRE (snapshot HEAD + save pre-review diff), RUN (existing hermes subprocess), POST (run pytest, commit fixes on pass / `git reset --hard` + `git clean -fd` on fail-or-timeout, machine-verify the outcome). `phases._invoke_hermes` dispatches `phase_5_review` through this module; all other phases keep their current generic path unchanged.

**Tech Stack:** Python 3.12+, `uv`, `pytest` + `pytest-mock`, `pyyaml`, `subprocess`/`git`.

## Global Constraints

- **Python floor:** `requires-python = ">=3.12"`. Use `from __future__ import annotations` (matches every existing module).
- **Package name / layout:** source lives under `hermes_pipeline/`, tests under `tests/`. New module: `hermes_pipeline/review_phase.py`. New test file: `tests/test_review_phase.py`.
- **Run tests with:** `uv run pytest` (never bare `pytest`).
- **Phase config lives in:** `configs/phases.yaml`, loaded by `hermes_pipeline/phases.py::load_phases`.
- **Phase key (exact):** `phase_5_review`. Position: between `phase_4_development` and `phase_6_1_cso`. Final sequence: `2 → 3 → 4 → 5 → 6.1 → 7 → 8 → 9`.
- **Phase infra values (from design):** `tools: "Read,Edit,Bash"` (Write dropped — least privilege), `turns: 30`, `timeout: 2400`.
- **Diff base (exact):** `git merge-base HEAD main` — never bare `git diff main`. The default branch is `main`.
- **Review artifacts (committed to `docs/pipeline/`, so CSO and the human PR see them):**
  - `docs/pipeline/{todo_id}-pre-review.diff`
  - `docs/pipeline/{todo_id}-post-review.diff` (clean path only)
  - `docs/pipeline/{todo_id}-review-findings.md`
  - `docs/pipeline/{todo_id}-review-outcome.json`
- **Outcome vocabulary (exact strings):** `review_clean`, `review_reverted_test_failure`, `review_timeout`, `review_skipped_no_diff`.
- **Success is a machine check, not `rc==0`:** a returning (success) path must pass `_verify_review_success` (tree clean + expected artifacts exist) or it raises instead.
- **Commit through git directly in code** (this module runs unattended; the git-atomic-commits skill is for human-driven commits). Every commit sets author via `-c user.name=... -c user.email=...` OR relies on repo config; tests configure repo-local `user.email`/`user.name`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `configs/phases.yaml` | Phase registry | **Modify** — add `phase_5_review` entry between dev and CSO |
| `hermes_pipeline/review_phase.py` | Code-owned review lifecycle: pre-snapshot, restore, pytest, finalize, verify | **Create** |
| `hermes_pipeline/phases.py` | Phase executor | **Modify** — dispatch `phase_5_review` to `review_phase` in `_invoke_hermes` |
| `tests/test_phases.py` | Config-parse + ordering assertions | **Modify** — add ordering/field tests for new phase |
| `tests/test_review_phase.py` | Unit tests for the review lifecycle (real temp git repo) | **Create** |
| `tests/test_phases_invoke.py` | `_invoke_hermes` routing | **Modify** — add dispatch test |

**Decomposition rationale:** All review control flow lives in one focused module (`review_phase.py`) with small, independently-testable functions. `phases.py` gains exactly one dispatch branch so the generic path for the other seven phases is untouched. Config change is isolated to one YAML entry + its parse tests.

---

## Task 1: Add `phase_5_review` to `configs/phases.yaml`

**Files:**
- Modify: `configs/phases.yaml` (insert between `phase_4_development` block ending at line 29 and `phase_6_1_cso` block starting at line 30)
- Test: `tests/test_phases.py`

**Interfaces:**
- Consumes: `hermes_pipeline.phases.load_phases()` → `list[Phase]`; `Phase` dataclass fields `phase_key, name, prompt, tools, turns, timeout, terminal, gate` (defined `phases.py:12-20`).
- Produces: a `Phase` with `phase_key == "phase_5_review"`, `tools == "Read,Edit,Bash"`, `turns == 30`, `timeout == 2400`, positioned at index 3 (0-based) in the loaded list.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_phases.py` (after `test_real_phases_yaml_ends_with_blocked_gate`):

```python
def test_real_phases_yaml_has_review_phase_between_dev_and_cso():
    phases = load_phases()  # default: configs/phases.yaml
    keys = [p.phase_key for p in phases]
    assert "phase_5_review" in keys, keys
    dev_i = keys.index("phase_4_development")
    rev_i = keys.index("phase_5_review")
    cso_i = keys.index("phase_6_1_cso")
    assert dev_i < rev_i < cso_i, keys


def test_real_phases_yaml_review_phase_fields():
    phases = {p.phase_key: p for p in load_phases()}
    rev = phases["phase_5_review"]
    assert rev.tools == "Read,Edit,Bash"
    assert rev.turns == 30
    assert rev.timeout == 2400
    assert rev.terminal is False
    assert rev.gate is False
    # Prompt is instruction-only: it must NOT carry rollback/test control flow.
    assert "reset --hard" not in rev.prompt
    assert "pytest" not in rev.prompt.lower()


def test_real_phases_yaml_order_unchanged_for_existing_phases():
    keys = [p.phase_key for p in load_phases()]
    assert keys == [
        "phase_2_autoplan",
        "phase_3_writing_plan",
        "phase_4_development",
        "phase_5_review",
        "phase_6_1_cso",
        "phase_7_document_release",
        "phase_8_finish_branch",
        "phase_9_ship",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_phases.py -k "review_phase or order_unchanged" -v`
Expected: FAIL — `KeyError: 'phase_5_review'` / `assert 'phase_5_review' in keys`.

- [ ] **Step 3: Add the YAML entry**

In `configs/phases.yaml`, insert this block immediately **after** the `phase_4_development` entry (the block ending `timeout: 3600` at line 29) and **before** the `phase_6_1_cso` entry (line 30):

```yaml
  - phase_key: "phase_5_review"
    name: "Phase 5: Code Review"
    prompt: |
      Use the gstack /review skill on the current branch.

      AUTONOMOUS MODE — there is no human at the terminal. Apply the review's
      fixes without asking for confirmation. Do NOT pause for user input.

      Scope: {todo_id}. Focus on correctness bugs and simplification cleanups
      in the changes on this branch. Apply fixes directly to the working tree.
      Do NOT run tests, commit, revert, or reset — the pipeline does that after
      you finish.
    tools: "Read,Edit,Bash"
    turns: 30
    timeout: 2400
```

Note: the prompt is instruction-only. Test execution, committing, and rollback are owned by `review_phase.py` (Tasks 2–6), not the prompt.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_phases.py -v`
Expected: PASS (all existing `test_phases.py` tests plus the three new ones).

- [ ] **Step 5: Commit**

```bash
git add configs/phases.yaml tests/test_phases.py
git commit -m "feat(phases): add phase_5_review config entry between dev and CSO"
```

---

## Task 2: `review_phase.py` — capture pre-review state

**Files:**
- Create: `hermes_pipeline/review_phase.py`
- Test: `tests/test_review_phase.py`

**Interfaces:**
- Produces:
  - `REVIEW_PHASE_KEY = "phase_5_review"`
  - `OUTCOME_CLEAN = "review_clean"`, `OUTCOME_REVERTED = "review_reverted_test_failure"`, `OUTCOME_TIMEOUT = "review_timeout"`, `OUTCOME_NO_DIFF = "review_skipped_no_diff"`
  - `@dataclass(frozen=True) PreReviewState(head_sha: str, diff_is_empty: bool)`
  - `_git(project_dir: Path, *args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess`
  - `_diff_base(project_dir: Path, base_ref: str = "main") -> str` — returns `git merge-base HEAD <base_ref>`
  - `capture_pre_review_state(*, project_dir: Path, todo_id: str, base_ref: str = "main") -> PreReviewState` — writes `docs/pipeline/{todo_id}-pre-review.diff`, returns snapshot.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing test**

Create `tests/test_review_phase.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_review_phase.py -k capture -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_pipeline.review_phase'`.

- [ ] **Step 3: Write minimal implementation**

Create `hermes_pipeline/review_phase.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_review_phase.py -k capture -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/review_phase.py tests/test_review_phase.py
git commit -m "feat(review): add pre-review state capture for phase_5_review"
```

---

## Task 3: `review_phase.py` — deterministic worktree restore

**Files:**
- Modify: `hermes_pipeline/review_phase.py`
- Test: `tests/test_review_phase.py`

**Interfaces:**
- Consumes: `PreReviewState.head_sha` from Task 2.
- Produces: `restore_worktree(*, project_dir: Path, head_sha: str) -> None` — runs `git reset --hard <head_sha>` then `git clean -fd`, leaving the tree exactly as it was pre-review (tracked changes reverted, untracked files removed).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_phase.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_review_phase.py -k restore -v`
Expected: FAIL — `AttributeError: module 'hermes_pipeline.review_phase' has no attribute 'restore_worktree'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/review_phase.py` (after `capture_pre_review_state`):

```python
def restore_worktree(*, project_dir, head_sha: str) -> None:
    """Deterministically restore the tree to head_sha.

    `reset --hard` reverts tracked changes and moves HEAD; `clean -fd` removes
    untracked files/dirs the review may have created. Plain `clean -fd` does
    NOT remove gitignored files (e.g. .hermes/), which is intentional.
    """
    project_dir = Path(project_dir)
    _git(project_dir, "reset", "--hard", head_sha)
    _git(project_dir, "clean", "-fd")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_review_phase.py -k restore -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/review_phase.py tests/test_review_phase.py
git commit -m "feat(review): add deterministic worktree restore"
```

---

## Task 4: `review_phase.py` — pytest runner + artifact/commit helpers

**Files:**
- Modify: `hermes_pipeline/review_phase.py`
- Test: `tests/test_review_phase.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) PytestResult(returncode: int, stdout: str, stderr: str)`
  - `run_pytest(*, project_dir: Path, timeout: int = 1200) -> PytestResult` — runs `uv run pytest` in `project_dir`.
  - `write_review_artifacts(*, project_dir: Path, todo_id: str, outcome: str, findings_text: str, base_ref: str = "main", include_post_diff: bool) -> None` — writes `{todo_id}-review-findings.md`, `{todo_id}-review-outcome.json`, and (when `include_post_diff`) `{todo_id}-post-review.diff`.
  - `commit_all(*, project_dir: Path, todo_id: str, message: str) -> None` — `git add -A` then `git commit -m <message>` (no-ops cleanly if nothing staged).
- Consumes: `_git`, `_docs_dir`, `_diff_base` from Tasks 2–3.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_phase.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_review_phase.py -k "pytest or artifacts or commit_all" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'run_pytest'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/review_phase.py`:

```python
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
```

Note: `write_review_artifacts` computes the post-review diff BEFORE the caller commits, so on the clean path the diff captures the fixes as working-tree changes; `commit_all` then commits the fixes **and** the artifacts together. See Task 5 for ordering.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_review_phase.py -k "pytest or artifacts or commit_all" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/review_phase.py tests/test_review_phase.py
git commit -m "feat(review): add pytest runner, artifact writer, commit helper"
```

---

## Task 5: `review_phase.py` — `finalize_review` + machine verify

**Files:**
- Modify: `hermes_pipeline/review_phase.py`
- Test: `tests/test_review_phase.py`

**Interfaces:**
- Consumes: `PreReviewState`, `restore_worktree`, `run_pytest`, `write_review_artifacts`, `commit_all`, all `OUTCOME_*` constants.
- Produces:
  - `_verify_review_success(*, project_dir: Path, todo_id: str, expect_post_diff: bool) -> None` — raises `RuntimeError` unless `git status --porcelain` is empty AND `{todo_id}-review-outcome.json` + `{todo_id}-review-findings.md` exist (and, when `expect_post_diff`, `{todo_id}-post-review.diff` exists).
  - `finalize_review(*, project_dir: Path, todo_id: str, pre_state: PreReviewState, hermes_result: dict, base_ref: str = "main", pytest_runner=run_pytest) -> dict` — the POST decision:
    - **timeout or `returncode != 0`** → `restore_worktree`; write `OUTCOME_TIMEOUT` artifacts; `commit_all` (rollback note); verify (no post-diff); then **raise `RuntimeError`** (phase fails cleanly, tree restored).
    - **`returncode == 0` and pytest passes** → write `OUTCOME_CLEAN` artifacts (with post-diff); `commit_all`; verify (expect post-diff); return `{"status":"success","phase_key":REVIEW_PHASE_KEY,"outcome":OUTCOME_CLEAN}`.
    - **`returncode == 0` and pytest fails** → `restore_worktree`; write `OUTCOME_REVERTED` artifacts; `commit_all` (rollback note); verify (no post-diff); return `{"status":"success",...,"outcome":OUTCOME_REVERTED}` (phase completes; pipeline continues to CSO).
  - `hermes_result` dict shape matches `_run_hermes_subprocess` return: keys `returncode`, `stdout`, `stderr`, `timed_out`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_phase.py`:

```python
def _apply_review_edit(repo: Path):
    """Simulate what /review does: edit a tracked file in the working tree."""
    (repo / "feature.py").write_text("y = 2\nz = 3  # review-added\n")


def test_finalize_clean_commits_and_returns_clean(repo: Path):
    pre = rp.capture_pre_review_state(project_dir=repo, todo_id="TODO-7")
    _apply_review_edit(repo)
    out = rp.finalize_review(
        project_dir=repo, todo_id="TODO-7", pre_state=pre,
        hermes_result={"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False},
        pytest_runner=lambda **kw: rp.PytestResult(0, "1 passed", ""),
    )
    assert out["outcome"] == rp.OUTCOME_CLEAN
    assert out["status"] == "success"
    # Fix is committed and tree is clean.
    assert _run(repo, "git", "status", "--porcelain") == ""
    assert "review-added" in (repo / "feature.py").read_text()
    docs = repo / "docs" / "pipeline"
    assert (docs / "TODO-7-post-review.diff").exists()
    assert json.loads((docs / "TODO-7-review-outcome.json").read_text())["outcome"] == rp.OUTCOME_CLEAN


def test_finalize_test_failure_reverts_but_completes(repo: Path):
    head = _run(repo, "git", "rev-parse", "HEAD")
    pre = rp.capture_pre_review_state(project_dir=repo, todo_id="TODO-7")
    _apply_review_edit(repo)
    out = rp.finalize_review(
        project_dir=repo, todo_id="TODO-7", pre_state=pre,
        hermes_result={"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False},
        pytest_runner=lambda **kw: rp.PytestResult(1, "1 failed", "assert"),
    )
    assert out["outcome"] == rp.OUTCOME_REVERTED
    assert out["status"] == "success"  # phase COMPLETES
    # The review edit is gone; tree is clean; a rollback note commit exists on top of pre-HEAD.
    assert "review-added" not in (repo / "feature.py").read_text()
    assert _run(repo, "git", "status", "--porcelain") == ""
    assert head in _run(repo, "git", "rev-list", "HEAD")  # pre-HEAD is an ancestor
    assert not (repo / "docs" / "pipeline" / "TODO-7-post-review.diff").exists()


def test_finalize_timeout_reverts_and_raises(repo: Path):
    pre = rp.capture_pre_review_state(project_dir=repo, todo_id="TODO-7")
    _apply_review_edit(repo)
    with pytest.raises(RuntimeError, match="phase_5_review"):
        rp.finalize_review(
            project_dir=repo, todo_id="TODO-7", pre_state=pre,
            hermes_result={"returncode": -1, "stdout": "", "stderr": "[killed]", "timed_out": True},
            pytest_runner=lambda **kw: rp.PytestResult(0, "", ""),  # must NOT be called
        )
    assert "review-added" not in (repo / "feature.py").read_text()
    assert _run(repo, "git", "status", "--porcelain") == ""
    assert json.loads(
        (repo / "docs" / "pipeline" / "TODO-7-review-outcome.json").read_text()
    )["outcome"] == rp.OUTCOME_TIMEOUT


def test_finalize_nonzero_rc_reverts_and_raises(repo: Path):
    pre = rp.capture_pre_review_state(project_dir=repo, todo_id="TODO-7")
    _apply_review_edit(repo)
    with pytest.raises(RuntimeError, match="phase_5_review"):
        rp.finalize_review(
            project_dir=repo, todo_id="TODO-7", pre_state=pre,
            hermes_result={"returncode": 2, "stdout": "", "stderr": "boom", "timed_out": False},
            pytest_runner=lambda **kw: rp.PytestResult(0, "", ""),
        )
    assert _run(repo, "git", "status", "--porcelain") == ""


def test_verify_review_success_raises_on_dirty_tree(repo: Path):
    rp.write_review_artifacts(
        project_dir=repo, todo_id="TODO-7", outcome=rp.OUTCOME_CLEAN,
        findings_text="x", include_post_diff=False,
    )
    (repo / "dirty.txt").write_text("uncommitted\n")  # leave tree dirty
    with pytest.raises(RuntimeError, match="verify"):
        rp._verify_review_success(project_dir=repo, todo_id="TODO-7", expect_post_diff=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_review_phase.py -k "finalize or verify_review" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'finalize_review'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/review_phase.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_review_phase.py -v`
Expected: PASS (all review_phase tests: capture, restore, pytest/artifacts/commit, finalize×4, verify).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/review_phase.py tests/test_review_phase.py
git commit -m "feat(review): add finalize_review decision and machine verify"
```

---

## Task 6: Dispatch `phase_5_review` from `phases._invoke_hermes`

**Files:**
- Modify: `hermes_pipeline/phases.py` (`_invoke_hermes`, around lines 167-229)
- Test: `tests/test_phases_invoke.py`

**Interfaces:**
- Consumes: `review_phase.REVIEW_PHASE_KEY`, `review_phase.capture_pre_review_state`, `review_phase.finalize_review`, `review_phase.write_review_artifacts`, `review_phase.commit_all`, `review_phase.OUTCOME_NO_DIFF`; existing `phases._render_phase_prompt`, `phases._run_hermes_subprocess`, `phases._update_marker_pid`.
- Produces: `_invoke_review_phase(*, phase, todo_id, tick_id, state_dir, project_slug, project_dir, on_pid) -> dict` and a dispatch branch in `_invoke_hermes` that routes `phase.phase_key == REVIEW_PHASE_KEY` to it. The generic path for all other phases is unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_phases_invoke.py`:

```python
def test_invoke_routes_review_phase_through_review_lifecycle(state_dir, monkeypatch, tmp_path):
    """phase_5_review must go through capture -> hermes -> finalize, not the
    generic rc-check path."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda: [
        _fake_phase(phase_key="phase_5_review", terminal=False,
                    prompt="run /review", turns=30, timeout=2400),
    ])
    calls = {}

    from hermes_pipeline import review_phase as rp

    monkeypatch.setattr(rp, "capture_pre_review_state",
                        lambda **kw: rp.PreReviewState(head_sha="abc123", diff_is_empty=False))
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess",
                        lambda **kw: {"returncode": 0, "stdout": "reviewed", "stderr": "", "timed_out": False})

    def _fake_finalize(**kw):
        calls["finalize"] = kw
        return {"status": "success", "phase_key": "phase_5_review", "outcome": rp.OUTCOME_CLEAN}

    monkeypatch.setattr(rp, "finalize_review", _fake_finalize)

    out = phases_mod._invoke_hermes(
        todo_id="TODO-7", phase_key="phase_5_review", tick_id="01JT",
        state_dir=state_dir, project_slug="demo", project_dir=str(tmp_path),
    )
    assert out["outcome"] == rp.OUTCOME_CLEAN
    assert calls["finalize"]["hermes_result"]["stdout"] == "reviewed"
    assert calls["finalize"]["pre_state"].head_sha == "abc123"


def test_invoke_review_phase_short_circuits_on_no_diff(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda: [
        _fake_phase(phase_key="phase_5_review", terminal=False, prompt="run /review"),
    ])
    from hermes_pipeline import review_phase as rp
    monkeypatch.setattr(rp, "capture_pre_review_state",
                        lambda **kw: rp.PreReviewState(head_sha="abc", diff_is_empty=True))
    monkeypatch.setattr(rp, "write_review_artifacts", lambda **kw: None)
    monkeypatch.setattr(rp, "commit_all", lambda **kw: None)

    def _boom(**kw):
        raise AssertionError("hermes must not run on a no-diff branch")

    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _boom)

    out = phases_mod._invoke_hermes(
        todo_id="TODO-9", phase_key="phase_5_review", tick_id="01JT",
        state_dir=state_dir, project_slug="demo", project_dir=str(tmp_path),
    )
    assert out["outcome"] == rp.OUTCOME_NO_DIFF
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_phases_invoke.py -k review -v`
Expected: FAIL — the generic path runs `_run_hermes_subprocess` and never calls `finalize_review` (KeyError on `calls["finalize"]`), and the no-diff test hits the `_boom` AssertionError.

- [ ] **Step 3: Write minimal implementation**

In `hermes_pipeline/phases.py`, add `_invoke_review_phase` **before** `_invoke_hermes`:

```python
def _invoke_review_phase(
    *, phase, todo_id: str, tick_id: str, state_dir, project_slug: str, project_dir, on_pid,
) -> dict:
    """Code-owned lifecycle for phase_5_review (Approach C).

    PRE (snapshot + pre-review diff) and POST (pytest + commit-or-restore +
    verify) are owned here, NOT the prompt. The prompt only instructs /review.
    """
    from . import review_phase as rp

    pre = rp.capture_pre_review_state(project_dir=project_dir, todo_id=todo_id)

    # No-diff guard: nothing to review (e.g. docs-only TODO). Skip hermes.
    if pre.diff_is_empty:
        rp.write_review_artifacts(
            project_dir=project_dir, todo_id=todo_id, outcome=rp.OUTCOME_NO_DIFF,
            findings_text="No changes vs main; review skipped.", include_post_diff=False,
        )
        rp.commit_all(project_dir=project_dir, todo_id=todo_id,
                      message=f"review: no changes to review for {todo_id}")
        return {"status": "success", "phase_key": rp.REVIEW_PHASE_KEY, "outcome": rp.OUTCOME_NO_DIFF}

    prompt = _render_phase_prompt(
        phase.prompt, todo_id=todo_id, tick_id=tick_id, project_slug=project_slug,
    )
    result = _run_hermes_subprocess(
        prompt=prompt, tools=phase.tools, turns=phase.turns, timeout=phase.timeout,
        cwd=project_dir, on_pid=on_pid,
    )
    return rp.finalize_review(
        project_dir=project_dir, todo_id=todo_id, pre_state=pre, hermes_result=result,
    )
```

Then add the dispatch branch inside `_invoke_hermes`, immediately after the `phase is None` check (after line 175, before `sd = Path(state_dir)`... — place it right after the `_record_child_pid` definition so `on_pid` is available). Replace the region from the `prompt = _render_phase_prompt(...)` assignment; insert the branch **before** it:

```python
    sd = Path(state_dir)

    def _record_child_pid(pid: int) -> None:
        _update_marker_pid(sd, todo_id, pid)

    from . import review_phase as _rp
    if phase.phase_key == _rp.REVIEW_PHASE_KEY:
        return _invoke_review_phase(
            phase=phase, todo_id=todo_id, tick_id=tick_id, state_dir=sd,
            project_slug=project_slug, project_dir=kw.get("project_dir"),
            on_pid=_record_child_pid,
        )

    prompt = _render_phase_prompt(
```

(The existing `prompt = _render_phase_prompt(...)` line and everything after it stays as the generic path.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_phases_invoke.py -v`
Expected: PASS (all existing invoke tests plus the two new routing tests).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/phases.py tests/test_phases_invoke.py
git commit -m "feat(phases): dispatch phase_5_review to code-owned review lifecycle"
```

---

## Task 7: Full-suite regression + dry-run validation note

**Files:**
- No source changes (validation task). If regressions surface, fix in the owning task's file.

**Interfaces:** none.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest`
Expected: PASS — the entire existing suite plus all new `test_review_phase.py`, `test_phases.py`, and `test_phases_invoke.py` tests. No regressions in `test_phases_invoke.py`'s generic-path tests (they must still exercise the unchanged `rc != 0 → RuntimeError` behavior for non-review phases).

- [ ] **Step 2: Confirm the generic failure path is untouched**

Run: `uv run pytest tests/test_phases_invoke.py::test_invoke_propagates_subprocess_failure tests/test_phases_invoke.py::test_invoke_writes_ready_for_review_on_terminal_phase -v`
Expected: PASS — proves the dispatch branch did not alter non-review phase behavior.

- [ ] **Step 3: Record the required pre-merge dry run (manual, out of band)**

This plan implements the code contract. Per the design's **critical open question (T2)**, one manual validation MUST happen on a feature branch before this reaches production ticks — it cannot be unit-tested because it depends on live `/review` behavior inside `hermes chat -q`:

> Run `phase_5_review` manually against a recent feature branch and confirm the gstack `/review` skill proceeds autonomously (applies fixes without hanging on a confirmation prompt) inside the hermes subprocess. If it hangs, the 2400s timeout will trigger the timeout-restore path — verify the worktree is restored clean and the phase fails cleanly.

Document the dry-run result in `docs/pipeline/` or the TODO's review notes. Do not enable the phase for unattended runs until this passes.

- [ ] **Step 4: Commit (only if any regression fixes were needed)**

```bash
git add -A
git commit -m "test: fix regressions surfaced by phase_5_review integration"
```

If Step 1 was green with no changes, skip this commit.

---

## Self-Review

**1. Spec coverage:**

| Design requirement | Task |
|--------------------|------|
| `phase_5_review` entry at correct position, `Read,Edit,Bash`, 30t/2400s, instruction-only prompt | Task 1 |
| PRE: capture HEAD sha + save pre-review diff via `merge-base HEAD main` | Task 2 |
| Deterministic restore: `git reset --hard <PRE_HEAD>` + `git clean -fd` | Task 3 |
| POST: `uv run pytest` machine-checked; committed artifacts | Task 4 |
| tests PASS → commit fixes, save post-review.diff + findings, `outcome=review_clean` | Task 5 |
| tests FAIL → restore, `outcome=review_reverted_test_failure`, phase completes | Task 5 |
| timeout / rc≠0 → restore, `outcome=review_timeout`, phase FAILS cleanly | Task 5 |
| VERIFY: tree clean + pytest ran + artifacts exist == success (not exit 0) | Task 5 (`_verify_review_success`) |
| No-diff guard (docs-only TODO) | Task 6 (`_invoke_review_phase`) |
| Prompt carries no rollback/test logic | Task 1 (asserted) + Task 6 (logic in code) |
| Phase numbering coherence `2→3→4→5→6.1→7→8→9` | Task 1 |
| Autonomous-mode dry run (T2, critical) | Task 7 Step 3 (manual, documented as required) |

**2. Placeholder scan:** No `TBD`/`add error handling`/`similar to`/`write tests for the above` — every code and test step contains complete content.

**3. Type consistency:** `PreReviewState(head_sha, diff_is_empty)`, `PytestResult(returncode, stdout, stderr)`, `finalize_review(..., pre_state, hermes_result, pytest_runner=run_pytest) -> dict`, `write_review_artifacts(..., include_post_diff)`, `commit_all(..., todo_id, message)`, `_verify_review_success(..., expect_post_diff)` — names and signatures match across Tasks 2→6. `hermes_result` dict keys (`returncode/stdout/stderr/timed_out`) match `_run_hermes_subprocess`'s return shape in `phases.py:132-137`. Outcome constants (`review_clean/review_reverted_test_failure/review_timeout/review_skipped_no_diff`) are used identically in Tasks 5 and 6.

**Deferred (per design "NOT in Scope"):** structured findings JSON schema, static-analysis pre-filter, bounded-fix policy in code, CSO turn re-tuning, per-TODO skip flag. Markdown findings + a minimal outcome JSON ship now.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-07-phase-5-review.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
