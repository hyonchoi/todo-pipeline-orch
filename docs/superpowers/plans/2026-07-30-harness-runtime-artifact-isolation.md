# Harness Runtime Artifact Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate harness diagnostics from the mock Git project and remove obsolete or empty terminal `.hermes` state from retained fixtures.

**Architecture:** Treat each `harness-*` directory as a workspace containing a `project/` Git repository and an `artifacts/` diagnostics directory. Keep production-parity `.hermes` state inside the project during execution, then apply a narrow retained-run cleanup after all status and report consumers finish.

**Tech Stack:** Python 3.12+, pathlib, shutil, pytest, pytest-mock, uv

## Global Constraints

- Keep `.hermes/pipeline.toml` inside the mock project as its execution contract.
- Keep events, reports, filtered phase YAML, and numbered loop reports under `artifacts/`.
- Do not change phase semantics, prompt rendering, Hermes registration, or report contents.
- Never recursively delete unknown `.hermes` content.
- Preserve every non-empty checkpoint, review, or outcome directory as failure evidence.
- Use only allowlisted terminal-state cleanup after final report and status reads.
- Run searches, reads, tests, linters, compilation, and Git checks with explicit `rtk` prefixes.

---

## File Map

- `hermes_pipeline/harness.py`: fixture initialization, workspace path ownership, retained-state cleanup, and harness orchestration.
- `tests/test_harness.py`: behavior tests for fixture contents, workspace/artifact separation, loop reports, and cleanup safety.
- `docs/howto-mock-integration-test-harness.md`: operator-facing retained workspace layout and inspection commands.
- `docs/reference-cli.md`: concise `tpo test` output-path contract.

### Task 1: Stop Seeding Obsolete Fixture State

**Files:**
- Modify: `tests/test_harness.py:30-80`
- Modify: `hermes_pipeline/harness.py:22-70`

**Interfaces:**
- Consumes: `create_mock_project(path: Path, fixture_name: str) -> dict[str, Any]`
- Produces: a committed mock project containing `.hermes/pipeline.toml` but no `.hermes/todo_id_counter`

- [ ] **Step 1: Replace the runtime-ignore test with a fixture ownership test**

Change `TestCreateMockProject` to exercise the real fixture and assert the
project-owned boundary:

```python
def test_create_mock_project_omits_harness_owned_and_legacy_state(
    self, tmp_path: Path
):
    create_mock_project(tmp_path, "happy-path")

    assert (tmp_path / ".hermes" / "pipeline.toml").exists()
    assert not (tmp_path / ".hermes" / "todo_id_counter").exists()
    assert not (tmp_path / "events.jsonl").exists()
    assert not (tmp_path / "reports").exists()

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""
```

Remove assertions that require `events.jsonl` to be ignored inside the Git
project. Keep coverage for agent scratch space and Python bytecode only if
those ignore rules remain in `_MOCK_PROJECT_GITIGNORE`.

- [ ] **Step 2: Run the test and verify the obsolete counter assertion fails**

Run:

```bash
rtk uv run pytest tests/test_harness.py::TestCreateMockProject::test_create_mock_project_omits_harness_owned_and_legacy_state -v
```

Expected: FAIL because `.hermes/todo_id_counter` exists.

- [ ] **Step 3: Remove legacy state creation and stale event ignore**

In `create_mock_project()`, retain `hermes_dir.mkdir()` and
`.hermes/pipeline.toml`, but delete:

```python
(path / ".hermes" / "todo_id_counter").write_text("0")
```

Remove `events.jsonl` from `_MOCK_PROJECT_GITIGNORE`; events will no longer
live inside the project. Keep ignores for runtime `.hermes` files that exist
during execution and for agent/Python scratch output.

- [ ] **Step 4: Run fixture tests**

Run:

```bash
rtk uv run pytest tests/test_harness.py::TestCreateMockProject -v
```

Expected: all fixture tests PASS.

- [ ] **Step 5: Commit the fixture initialization change**

```bash
rtk git add hermes_pipeline/harness.py tests/test_harness.py
rtk git commit -m "Remove obsolete harness fixture state"
```

### Task 2: Separate Workspace, Project, and Artifact Paths

**Files:**
- Modify: `tests/test_harness.py:500-710`
- Modify: `hermes_pipeline/harness.py:590-745`

**Interfaces:**
- Consumes: `run_harness(..., keep_dir: bool, ...) -> HarnessResult`
- Produces: retained `HarnessResult.temp_dir == workspace`, project at `workspace / "project"`, report at `workspace / "artifacts" / "reports" / "report.json"`

- [ ] **Step 1: Add a retained workspace boundary test**

Add a test that uses real `create_mock_project()` and Git, while replacing
only external Hermes interactions:

```python
def test_run_harness_separates_project_from_artifacts(
    self, tmp_path, monkeypatch, mocker
):
    workspace = tmp_path / "harness-run"
    monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)
    monkeypatch.setattr(
        "hermes_pipeline.harness.tempfile.mkdtemp",
        lambda prefix=None, dir=None: str(workspace),
    )
    mocker.patch("hermes_pipeline.harness._kanban_preflight")
    poll = mocker.patch(
        "hermes_pipeline.harness._poll_kanban_phases",
        return_value=True,
    )
    mocker.patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
        return_value={"phase_2_autoplan": "done"},
    )

    result = run_harness(
        fixture_name="happy-path",
        loop=False,
        phase_only=None,
        keep_dir=True,
        timeout=60,
        convergence_threshold=3,
        config=None,
    )

    project_dir = workspace / "project"
    artifacts_dir = workspace / "artifacts"
    assert result.temp_dir == workspace
    assert result.report_path == artifacts_dir / "reports" / "report.json"
    assert (project_dir / ".git").exists()
    assert (artifacts_dir / "events.jsonl").exists()
    assert not (project_dir / "events.jsonl").exists()
    assert not (project_dir / "reports").exists()
    assert poll.call_args.kwargs["project_dir"] == project_dir
    assert poll.call_args.kwargs["state_dir"] == project_dir / ".hermes"
```

- [ ] **Step 2: Run the boundary test and verify the old layout fails**

Run:

```bash
rtk uv run pytest tests/test_harness.py::TestKanbanModeHermes::test_run_harness_separates_project_from_artifacts -v
```

Expected: FAIL because the Git repository, events, and reports are created at
the workspace root.

- [ ] **Step 3: Introduce explicit workspace paths in `run_harness()`**

Immediately after creating the workspace, define:

```python
workspace_dir = Path(
    tempfile.mkdtemp(prefix="harness-", dir=harness_tmp_root)
)
project_dir = workspace_dir / "project"
artifacts_dir = workspace_dir / "artifacts"
artifacts_dir.mkdir(parents=True)
fixture = create_mock_project(project_dir, fixture_name)

state_dir = project_dir / ".hermes"
events_log = artifacts_dir / "events.jsonl"
```

Update all consumers consistently:

```python
_phases_path_override = artifacts_dir / "filtered-phases.yaml"
project_dir=project_dir
output_dir = artifacts_dir / "reports"
temp_dir=workspace_dir if keep_dir else None
```

Rename the local orchestration variable from `temp_dir` to `workspace_dir`
throughout `run_harness()` and remove `workspace_dir` in the finalizer when
`keep_dir` is false.

- [ ] **Step 4: Add and verify loop-report placement**

Extend the boundary test or add a focused test with `loop=True`:

```python
assert (
    workspace / "artifacts" / "happy-path-report.1.json"
).exists()
assert not (workspace / "project" / "happy-path-report.1.json").exists()
```

Run:

```bash
rtk uv run pytest tests/test_harness.py -k "separates_project_from_artifacts or loop" -v
```

Expected: PASS, with reports and loop snapshots under `artifacts/`.

- [ ] **Step 5: Run all harness tests**

```bash
rtk uv run pytest tests/test_harness.py tests/test_harness_e2e.py tests/test_report.py -v
```

Expected: all selected tests PASS.

- [ ] **Step 6: Commit workspace path isolation**

```bash
rtk git add hermes_pipeline/harness.py tests/test_harness.py
rtk git commit -m "Separate harness project and artifacts"
```

### Task 3: Prune Only Safe Retained Hermes State

**Files:**
- Modify: `tests/test_harness.py`
- Modify: `hermes_pipeline/harness.py:540-745`

**Interfaces:**
- Produces: `_prune_retained_state(state_dir: Path) -> None`
- Cleanup contract: remove allowlisted files and empty allowlisted directories; preserve `pipeline.toml`, unknown paths, and all non-empty directories

- [ ] **Step 1: Add a failing cleanup safety test**

Import `_prune_retained_state` and add:

```python
def test_prune_retained_state_removes_only_safe_terminal_state(tmp_path):
    state_dir = tmp_path / ".hermes"
    state_dir.mkdir()
    (state_dir / "pipeline.toml").write_text("schema_version = 2\n")
    (state_dir / "pipeline_branch.txt").write_text("feat/mock\n")
    (state_dir / "tpo-config.yaml").write_text("state_dir: .hermes\n")
    (state_dir / "unknown.json").write_text("{}\n")

    empty_outcomes = state_dir / "outcomes"
    empty_outcomes.mkdir()
    empty_checkpoints = state_dir / "pipeline_checkpoints"
    empty_checkpoints.mkdir()
    evidence_dir = state_dir / "ready_for_review"
    evidence_dir.mkdir()
    (evidence_dir / "failure.json").write_text("{}\n")

    _prune_retained_state(state_dir)

    assert (state_dir / "pipeline.toml").exists()
    assert (state_dir / "unknown.json").exists()
    assert not (state_dir / "pipeline_branch.txt").exists()
    assert not (state_dir / "tpo-config.yaml").exists()
    assert not empty_outcomes.exists()
    assert not empty_checkpoints.exists()
    assert (evidence_dir / "failure.json").exists()
```

- [ ] **Step 2: Run the cleanup test and verify the helper is missing**

```bash
rtk uv run pytest tests/test_harness.py::test_prune_retained_state_removes_only_safe_terminal_state -v
```

Expected: collection FAIL because `_prune_retained_state` cannot be imported.

- [ ] **Step 3: Implement allowlisted cleanup**

Add near `HarnessResult`:

```python
def _prune_retained_state(state_dir: Path) -> None:
    for filename in ("pipeline_branch.txt", "tpo-config.yaml"):
        path = state_dir / filename
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not prune retained harness state %s: %s", path, exc)

    for dirname in ("outcomes", "pipeline_checkpoints", "ready_for_review"):
        path = state_dir / dirname
        try:
            if not path.exists():
                continue
            if next(path.iterdir(), None) is not None:
                continue
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("could not prune retained harness state %s: %s", path, exc)
```

This intentionally uses `Path.rmdir()` rather than recursive deletion.

- [ ] **Step 4: Wire cleanup after final consumers**

After `status_map` is read and the summary line is printed, but before
constructing `HarnessResult`, add:

```python
if keep_dir:
    _prune_retained_state(state_dir)
```

Do not run the helper before `generate_report()`, loop report comparison, or
the final `get_todo_kanban_status()` call.

- [ ] **Step 5: Prove retained cleanup is invoked and non-retained cleanup is unchanged**

Extend the retained workspace test:

```python
assert (project_dir / ".hermes" / "pipeline.toml").exists()
assert not (project_dir / ".hermes" / "tpo-config.yaml").exists()
assert not (project_dir / ".hermes" / "pipeline_checkpoints").exists()
assert not (project_dir / ".hermes" / "ready_for_review").exists()
```

Keep or add a `keep_dir=False` test that asserts `workspace` does not exist
after `run_harness()` returns.

Run:

```bash
rtk uv run pytest tests/test_harness.py -k "prune_retained_state or separates_project_from_artifacts or keep_dir" -v
```

Expected: all selected tests PASS.

- [ ] **Step 6: Run harness regression coverage**

```bash
rtk uv run pytest tests/test_harness.py tests/test_harness_e2e.py tests/test_report.py -v
```

Expected: all selected tests PASS.

- [ ] **Step 7: Commit retained-state pruning**

```bash
rtk git add hermes_pipeline/harness.py tests/test_harness.py
rtk git commit -m "Prune stale retained harness state"
```

### Task 4: Align Harness Documentation

**Files:**
- Modify: `docs/howto-mock-integration-test-harness.md:18-105`
- Modify: `docs/howto-mock-integration-test-harness.md:125-215`
- Modify: `docs/reference-cli.md:245-285`

**Interfaces:**
- Consumes: the path contract implemented by Tasks 2 and 3
- Produces: operator documentation using `~/.hermes/tmp/harness-*/project` and `artifacts`

- [ ] **Step 1: Update retained workspace examples**

Document this exact layout:

```text
harness-xxxxxxxx/
  project/
    .git/
    TODOS.md
    README.md
    .hermes/
      pipeline.toml
  artifacts/
    events.jsonl
    reports/
      report.json
      report.md
```

Replace `/tmp` discovery with:

```bash
rtk find ~/.hermes/tmp -maxdepth 1 -name 'harness-*' -type d
```

Update report inspection to:

```bash
rtk python -m json.tool \
  "$HARNESS_DIR/artifacts/reports/report.json"
```

- [ ] **Step 2: Update event and loop-report paths**

Every harness-layout reference to root `events.jsonl`, root `reports/`, or
root numbered reports must point to `artifacts/events.jsonl`,
`artifacts/reports/`, or `artifacts/happy-path-report.<n>.json`.

Update the `tpo test` summary examples in both documents to use:

```text
~/.hermes/tmp/harness-.../artifacts/reports/report.json
```

- [ ] **Step 3: Verify documentation consistency**

```bash
rtk rg -n "/tmp/harness|HARNESS_DIR/reports|harness-[^ ]*/reports|^[[:space:]]*events\\.jsonl" \
  docs/howto-mock-integration-test-harness.md docs/reference-cli.md
```

Expected: no stale layout references.

- [ ] **Step 4: Commit documentation**

```bash
rtk git add docs/howto-mock-integration-test-harness.md docs/reference-cli.md
rtk git commit -m "Document isolated harness artifacts"
```

### Task 5: Complete Verification

**Files:**
- Verify only; no planned file changes

**Interfaces:**
- Consumes: all implementation and documentation commits
- Produces: fresh evidence for the complete change

- [ ] **Step 1: Run focused harness coverage**

```bash
rtk uv run pytest tests/test_harness.py tests/test_harness_e2e.py tests/test_report.py -v
```

Expected: all selected tests PASS with zero failures.

- [ ] **Step 2: Run the complete locked project suite**

```bash
rtk uv run --locked pytest
```

Expected: the complete suite PASS with zero failures.

- [ ] **Step 3: Run source checks**

```bash
rtk uv run ruff check hermes_pipeline/harness.py tests/test_harness.py
rtk uv run python -m compileall -q hermes_pipeline tests
rtk git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 4: Review final history and tree**

```bash
rtk git status --short --branch
rtk git log --oneline -6
```

Expected: no uncommitted implementation changes and four focused commits
after the design commit.
