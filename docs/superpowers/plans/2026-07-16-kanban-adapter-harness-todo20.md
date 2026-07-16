# Real Kanban Adapter Option for Mock Integration Test Harness (TODO-20) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--kanban {null,hermes}` flag to `hermes-pipeline test` so the mock integration
harness can optionally drive a real `HermesKanbanAdapter` against a dedicated tenant instead of
the hardcoded `NullKanbanAdapter()`, while fixing the pre-existing `KanbanOutcome`/cleanup bugs
this change would otherwise inherit.

**Architecture:** `run_harness()` gains a `kanban_mode: str` parameter. When `"hermes"`, it
constructs `KanbanOutbox`/`ActiveTasksStore` under the fixture's temp `state_dir`, runs a
preflight `hermes kanban list --tenant <tenant>` check, and builds `HermesKanbanAdapter(outbox,
active_tasks)` — using the fixture's **unsuffixed** `project_slug` as the tenant (never
suffixed) so repeated runs land in the same tenant, distinguished only by a `tick_id` embedded in
each card's body metadata. `tick_id` generation moves earlier in `run_harness()` so it exists
before `PipelineRunner`/adapter construction. Runner-side, `KanbanOutcome` gains no new literal
(bug was using an invalid `"failed"` value, not a missing one) and two previously-silent cleanup
gaps (`continue_on_failure=False` phase failure, convergence-halt) get explicit
`clear_active_task(..., outcome="abandoned")` calls.

**Tech Stack:** Python 3.12+, `uv`, `pytest`, `unittest.mock.patch`/`MagicMock` for subprocess
mocking (existing pattern in `tests/test_kanban.py`).

## Global Constraints
- `--kanban null` stays the default; behavior must be byte-identical to today's hardcoded
  `NullKanbanAdapter()` path (no network calls, no new subprocess invocations).
- The fixture's `project_slug` (`"mock-project"`) is **never suffixed or mutated** — it is always
  passed as `--tenant` unsuffixed. Only card **body** metadata carries the per-run `tick_id`.
- No cleanup logic is added for timeout/crash paths — those stay genuinely orphaned by design
  (see terminal-state table in Task 4). Only the `continue_on_failure=False` and
  convergence-halt gaps get closed, because those are cases where cleanup was silently skipped by
  a pre-existing bug, not cases where "orphaned" was the intended behavior.
- `merge.py`'s `clear_active_task(rec.kanban_task_id)` signature mismatch (positional arg vs. the
  `(project, *, outcome)` Protocol) is **out of scope** — tracked as a follow-up bug in Task 4,
  not fixed here.
- Version bump (VERSION + pyproject.toml + uv.lock + CHANGELOG, all four together) happens once,
  at the end, per this repo's CLAUDE.md — not per task.

---

## File Structure

| File | Responsibility |
|---|---|
| `hermes_pipeline/kanban.py` | Fix `KanbanOutcome` usage; add `metadata` param to `set_active_task()` Protocol/Null/Hermes implementations |
| `hermes_pipeline/runner.py` | Wire `metadata` through `PipelineRunner.run()`'s `set_active_task()` call; fix `continue_on_failure=False` and `outcome="failed"→"abandoned"` cleanup gaps |
| `hermes_pipeline/harness.py` | Reorder `tick_id` generation; branch on `kanban_mode`; preflight validation; convergence-halt cleanup; CLI-visible output |
| `hermes_pipeline/cli.py` | Add `--kanban` flag with `choices=`; thread `kanban_mode` through `_cmd_test` → `run_harness()` |
| `tests/test_kanban.py` | Tests for `metadata` param on all three `set_active_task()` implementations |
| `tests/test_runner.py` | Tests for `continue_on_failure=False` cleanup, `outcome="abandoned"` regression |
| `tests/test_harness.py` | Tests for `kanban_mode` branching, tenant-unsuffixed regression, preflight failure, convergence-halt cleanup, tick_id-before-construction ordering |
| `tests/test_cli.py` | Tests for `--kanban` argparse `choices=` rejection and default |
| `docs/howto-mock-integration-test-harness.md` | New "Run with real kanban adapter" section |

---

### Task 1: Fix `KanbanOutcome` usage and metadata channel in `kanban.py`

**Files:**
- Modify: `hermes_pipeline/kanban.py:42-73` (Protocol), `:76-104` (NullKanbanAdapter),
  `:325-347` (HermesKanbanAdapter.set_active_task)
- Test: `tests/test_kanban.py`

**Interfaces:**
- Consumes: nothing new — `json`, `Path`, `Literal`, `Protocol` already imported.
- Produces: `KanbanClient.set_active_task(self, project: str, *, todo_id: int, title: str,
  phase: str, metadata: dict[str, str] | None = None) -> SyncResult` — the new keyword-only
  `metadata` param, consumed by Task 2/3. When present, `HermesKanbanAdapter` appends its
  key/value pairs to the card body as `f"\n{key}: {value}"` lines after the existing
  `Phase:`/`TODO ID:` lines, in the order the dict is iterated (Python dicts preserve insertion
  order).

- [ ] **Step 1: Write the failing test for Null adapter's new `metadata` param**

```python
# tests/test_kanban.py — inside class TestNullKanbanAdapter (near existing set_active_task test)

def test_null_adapter_set_active_task_accepts_metadata(self):
    adapter = NullKanbanAdapter()
    result = adapter.set_active_task(
        "project_a",
        todo_id=1,
        title="Test TODO",
        phase="Phase 1",
        metadata={"tick_id": "abc123", "fixture_name": "happy-path"},
    )
    assert result.ok is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban.py::TestNullKanbanAdapter::test_null_adapter_set_active_task_accepts_metadata -v`
Expected: FAIL with `TypeError: set_active_task() got an unexpected keyword argument 'metadata'`

- [ ] **Step 3: Update the Protocol and NullKanbanAdapter signatures**

```python
# hermes_pipeline/kanban.py — replace KanbanClient.set_active_task (lines 45-54)

    def set_active_task(
        self,
        project: str,
        *,
        todo_id: int,
        title: str,
        phase: str,
        metadata: dict[str, str] | None = None,
    ) -> SyncResult:
        """Set the active task for a project. Called when a TODO moves into Phase 1 (Development).

        metadata, when provided, is additional key/value context (e.g. tick_id, fixture_name,
        state_dir) recorded in the card body for debug-trail purposes. Implementations that
        don't render a body (e.g. NullKanbanAdapter) accept and ignore it.
        """
        ...
```

```python
# hermes_pipeline/kanban.py — replace NullKanbanAdapter.set_active_task (lines 79-87)

    def set_active_task(
        self,
        project: str,
        *,
        todo_id: int,
        title: str,
        phase: str,
        metadata: dict[str, str] | None = None,
    ) -> SyncResult:
        return SyncResult(ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kanban.py::TestNullKanbanAdapter::test_null_adapter_set_active_task_accepts_metadata -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for HermesKanbanAdapter's metadata rendering into the card body**

```python
# tests/test_kanban.py — inside class TestHermesKanbanAdapter

@patch("hermes_pipeline.kanban.subprocess.run")
def test_set_active_task_includes_metadata_in_body(self, mock_run, tmp_path):
    """metadata dict entries should appear in the --body argv, not in --tenant."""
    outbox_path = tmp_path / "outbox.jsonl"
    store_path = tmp_path / "active_tasks.json"
    outbox = KanbanOutbox(outbox_path)
    store = ActiveTasksStore(store_path)
    adapter = HermesKanbanAdapter(outbox, store)

    task_result = MagicMock()
    task_result.returncode = 0
    task_result.stdout = '{"id": "task-789"}'
    task_result.stderr = ""
    mock_run.side_effect = [task_result]

    result = adapter.set_active_task(
        "mock-project",
        todo_id=1,
        title="Test TODO",
        phase="Phase 1",
        metadata={"tick_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "fixture_name": "happy-path"},
    )

    assert result.ok is True
    call_args = mock_run.call_args[0][0]  # first positional arg: the cmd list
    assert call_args[0:4] == ["hermes", "kanban", "create", "--tenant"]
    assert call_args[4] == "mock-project"
    body_index = call_args.index("--body") + 1
    body = call_args[body_index]
    assert "tick_id: 01ARZ3NDEKTSV4RRFFQ69G5FAV" in body
    assert "fixture_name: happy-path" in body
    # tenant must never contain the tick_id (regression guard for the tenant-conflation bug)
    assert call_args[4] == "mock-project"
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban.py::TestHermesKanbanAdapter::test_set_active_task_includes_metadata_in_body -v`
Expected: FAIL — `TypeError: set_active_task() got an unexpected keyword argument 'metadata'`

- [ ] **Step 7: Implement `metadata` rendering in `HermesKanbanAdapter.set_active_task`**

```python
# hermes_pipeline/kanban.py — replace HermesKanbanAdapter.set_active_task (lines 325-370)

    def set_active_task(
        self,
        project: str,
        *,
        todo_id: int,
        title: str,
        phase: str,
        metadata: dict[str, str] | None = None,
    ) -> SyncResult:
        """Set active task. Creates a task card in the project tenant."""
        # Create the task using --tenant for namespacing
        body = f"Phase: {phase}\nTODO ID: {todo_id}"
        if metadata:
            for key, value in metadata.items():
                body += f"\n{key}: {value}"
        ok, output = self._run_cmd(
            [
                "hermes", "kanban", "create",
                "--tenant", project,
                title,
                "--body", body,
                "--json",
            ]
        )

        if not ok:
            # Queue for retry
            entry = OutboxEntry(
                project=project,
                operation="set_active_task",
                has_task_id=False,
                params={
                    "todo_id": todo_id,
                    "title": title,
                    "phase": phase,
                },
            )
            self.outbox.enqueue(entry, has_task_id=False)
            return SyncResult(ok=False, error=output)

        # Parse task_id from JSON output
        log.debug("kanban registration payload (raw JSON, truncated): %s", output[:500])
        try:
            task_data = json.loads(output)
            task_id = task_data["id"]
        except (json.JSONDecodeError, KeyError) as e:
            return SyncResult(ok=False, error=f"Failed to parse task ID: {e}")

        self.active_tasks.set(project, task_id)
        return SyncResult(ok=True, task_id=task_id)
```

Note: `metadata` is deliberately dropped from `OutboxEntry.params` on the retry path — the
outbox's `params` dict is already JSON-serialized field-by-field elsewhere in this file
(`todo_id`, `title`, `phase` only), and `drain_outbox()` (line 458-484) calls
`adapter.set_active_task(entry.project, todo_id=..., title=..., phase=...)` without a `metadata`
kwarg. Extending outbox replay to carry metadata is out of scope for this task — a retried card
will have the standard `Phase:`/`TODO ID:` body without the tick_id metadata lines. This is a
pre-existing gap in outbox fidelity (also true for `phase`/`status` today), not something this
plan introduces.

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_kanban.py::TestHermesKanbanAdapter::test_set_active_task_includes_metadata_in_body -v`
Expected: PASS

- [ ] **Step 9: Run the full kanban test file to check for regressions**

Run: `uv run pytest tests/test_kanban.py -v`
Expected: All PASS (existing `test_set_active_task_success`/`test_set_active_task_failure_queues_to_outbox`
still call `set_active_task` without `metadata` — since it's optional with a default, they must
still pass unchanged)

- [ ] **Step 10: Commit**

```bash
git add hermes_pipeline/kanban.py tests/test_kanban.py
git commit -m "feat(kanban): add optional metadata param to set_active_task for card body context"
```

---

### Task 2: Wire `metadata` through `PipelineRunner` and fix `outcome`/cleanup bugs in `runner.py`

**Files:**
- Modify: `hermes_pipeline/runner.py:112-142` (dataclass fields), `:169-183` (Step 1 call site),
  `:236-256` (phase-failure branch), `:275-282` (had_failures cleanup)
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `KanbanClient.set_active_task(..., metadata=...)` from Task 1;
  `KanbanClient.clear_active_task(..., outcome: KanbanOutcome)` (unchanged Protocol, but now
  called with valid literal values and from more call sites).
- Produces: `PipelineRunner` gains a new field `kanban_metadata: dict[str, str] | None = None`
  (dataclass field, after `monitor`). `PipelineRunner.run()` now calls
  `self.kanban.clear_active_task(project=self.project, outcome="abandoned")` from three sites
  instead of one: (a) existing `had_failures` branch (was `outcome="failed"`, now
  `"abandoned"`), (b) new: immediately before the `continue_on_failure=False` early `return False`
  at the old line 254, (c) — convergence-halt cleanup is NOT here, it belongs to `harness.py`
  (Task 4), since `ConvergenceHaltError` is raised and caught outside `runner.run()`'s scope.

- [ ] **Step 1: Write the failing test for `outcome="abandoned"` on the existing `had_failures` cleanup path**

```python
# tests/test_runner.py — add near existing continue_on_failure tests

def test_had_failures_clears_kanban_with_abandoned_outcome(tmp_path):
    """continue_on_failure=True + a phase failure should clear kanban with outcome='abandoned',
    not the invalid 'failed' literal."""
    from unittest.mock import MagicMock
    from hermes_pipeline.phases import Phase
    from hermes_pipeline.runner import PipelineRunner
    from hermes_pipeline.state import State

    kanban = MagicMock()
    kanban.set_active_task.return_value = MagicMock(ok=True)
    kanban.update_phase.return_value = MagicMock(ok=True)
    kanban.clear_active_task.return_value = MagicMock(ok=True)

    state = State(
        project="p",
        lock_dir=tmp_path / "locks",
        checkpoint_dir=tmp_path / "ckpt",
        ready_dir=tmp_path / "ready",
    )
    phase = Phase(phase_key="phase_1", name="Phase 1", gate=False)

    def failing_run_phase_fn(p):
        return 1

    runner = PipelineRunner(
        project="p",
        project_dir=tmp_path,
        branch="feat/x",
        todo_id=1,
        title="T",
        phases=[phase],
        state=state,
        kanban=kanban,
        run_phase_fn=failing_run_phase_fn,
        continue_on_failure=True,
    )

    result = runner.run()

    assert result is False
    kanban.clear_active_task.assert_called_once_with(project="p", outcome="abandoned")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runner.py::test_had_failures_clears_kanban_with_abandoned_outcome -v`
Expected: FAIL — `AssertionError: expected call not found` (actual call has `outcome="failed"`)

- [ ] **Step 3: Fix the invalid literal in the `had_failures` branch**

```python
# hermes_pipeline/runner.py — replace lines 275-282

        # Check if any phase failed during continue_on_failure run
        if had_failures:
            log.warning("Pipeline completed with phase failures (continue_on_failure)")
            try:
                self.kanban.clear_active_task(project=self.project, outcome="abandoned")
            except Exception as e:
                log.warning("kanban.clear_active_task failed: %s", e)
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_runner.py::test_had_failures_clears_kanban_with_abandoned_outcome -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for the `continue_on_failure=False` cleanup gap**

```python
# tests/test_runner.py

def test_continue_on_failure_false_clears_kanban_before_early_return(tmp_path):
    """A phase failure with continue_on_failure=False must still call clear_active_task
    before returning — this was previously a silent cleanup gap."""
    from unittest.mock import MagicMock
    from hermes_pipeline.phases import Phase
    from hermes_pipeline.runner import PipelineRunner
    from hermes_pipeline.state import State

    kanban = MagicMock()
    kanban.set_active_task.return_value = MagicMock(ok=True)
    kanban.update_phase.return_value = MagicMock(ok=True)
    kanban.clear_active_task.return_value = MagicMock(ok=True)

    state = State(
        project="p",
        lock_dir=tmp_path / "locks",
        checkpoint_dir=tmp_path / "ckpt",
        ready_dir=tmp_path / "ready",
    )
    phase = Phase(phase_key="phase_1", name="Phase 1", gate=False)

    def failing_run_phase_fn(p):
        return 1

    runner = PipelineRunner(
        project="p",
        project_dir=tmp_path,
        branch="feat/x",
        todo_id=1,
        title="T",
        phases=[phase],
        state=state,
        kanban=kanban,
        run_phase_fn=failing_run_phase_fn,
        continue_on_failure=False,
    )

    result = runner.run()

    assert result is False
    kanban.clear_active_task.assert_called_once_with(project="p", outcome="abandoned")
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_runner.py::test_continue_on_failure_false_clears_kanban_before_early_return -v`
Expected: FAIL — `AssertionError: Expected 'clear_active_task' to have been called once. Called 0 times.`

- [ ] **Step 7: Add the missing cleanup call before the early return**

```python
# hermes_pipeline/runner.py — replace lines 236-256

            if rc != 0:
                # Phase failed
                log.error(
                    "Phase %s failed with return code %d",
                    phase.name,
                    rc,
                )
                if self.monitor:
                    self.monitor("phase_failed", {"phase_key": phase.phase_key, "todo_id": self.todo_id, "duration_ms": duration_ms, "return_code": rc})
                try:
                    self.kanban.update_phase(
                        project=self.project,
                        phase=phase.name,
                        status="failed",
                    )
                except Exception as e:
                    log.warning("kanban.update_phase (failed) failed: %s", e)
                if not self.continue_on_failure:
                    try:
                        self.kanban.clear_active_task(project=self.project, outcome="abandoned")
                    except Exception as e:
                        log.warning("kanban.clear_active_task failed: %s", e)
                    return False
                had_failures = True
                continue
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_runner.py::test_continue_on_failure_false_clears_kanban_before_early_return -v`
Expected: PASS

- [ ] **Step 9: Write the failing test for `metadata` passthrough on `set_active_task`**

```python
# tests/test_runner.py

def test_kanban_metadata_field_passed_to_set_active_task(tmp_path):
    """PipelineRunner.kanban_metadata, when set, is forwarded to set_active_task's metadata kwarg."""
    from unittest.mock import MagicMock
    from hermes_pipeline.phases import Phase
    from hermes_pipeline.runner import PipelineRunner
    from hermes_pipeline.state import State

    kanban = MagicMock()
    kanban.set_active_task.return_value = MagicMock(ok=True)
    kanban.update_phase.return_value = MagicMock(ok=True)

    state = State(
        project="p",
        lock_dir=tmp_path / "locks",
        checkpoint_dir=tmp_path / "ckpt",
        ready_dir=tmp_path / "ready",
    )
    phase = Phase(phase_key="phase_1", name="Phase 1", gate=False)

    runner = PipelineRunner(
        project="p",
        project_dir=tmp_path,
        branch="feat/x",
        todo_id=1,
        title="T",
        phases=[phase],
        state=state,
        kanban=kanban,
        run_phase_fn=lambda p: 0,
        continue_on_failure=True,
        kanban_metadata={"tick_id": "abc123"},
    )

    runner.run()

    kanban.set_active_task.assert_called_once_with(
        project="p",
        todo_id=1,
        title="T",
        phase="Phase 1",
        metadata={"tick_id": "abc123"},
    )
```

- [ ] **Step 10: Run test to verify it fails**

Run: `uv run pytest tests/test_runner.py::test_kanban_metadata_field_passed_to_set_active_task -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'kanban_metadata'`

- [ ] **Step 11: Add the `kanban_metadata` field and thread it through**

```python
# hermes_pipeline/runner.py — replace dataclass field block (lines 130-142)

    project: str
    project_dir: Path
    branch: str
    todo_id: int
    title: str
    phases: list[Phase]
    state: State
    kanban: KanbanClient
    run_phase_fn: Callable[[Phase], int]
    tick_id: str = ""
    pr_url_resolver: Callable[[], str] = lambda: ""
    continue_on_failure: bool = False
    monitor: Callable | None = None
    kanban_metadata: dict[str, str] | None = None
```

```python
# hermes_pipeline/runner.py — replace lines 173-183 (Step 1: Set active task)

        # Step 1: Set active task on kanban (best-effort)
        first_phase = self.phases[0]
        try:
            self.kanban.set_active_task(
                project=self.project,
                todo_id=self.todo_id,
                title=self.title,
                phase=first_phase.name,
                metadata=self.kanban_metadata,
            )
        except Exception as e:
            log.warning("kanban.set_active_task failed (non-blocking): %s", e)
```

- [ ] **Step 12: Run test to verify it passes**

Run: `uv run pytest tests/test_runner.py::test_kanban_metadata_field_passed_to_set_active_task -v`
Expected: PASS

- [ ] **Step 13: Run the full runner test file to check for regressions**

Run: `uv run pytest tests/test_runner.py -v`
Expected: All PASS

- [ ] **Step 14: Commit**

```bash
git add hermes_pipeline/runner.py tests/test_runner.py
git commit -m "fix(runner): use valid KanbanOutcome literal, close continue_on_failure=False cleanup gap, wire kanban_metadata"
```

---

### Task 3: Add `--kanban` CLI flag with `choices=` validation

**Files:**
- Modify: `hermes_pipeline/cli.py:537-565` (`test_parser`), `hermes_pipeline/cli.py:1545-1560`
  (`_cmd_test`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `args.kanban: str` (`"null"` or `"hermes"`, default `"null"`) available to
  `_cmd_test`. `_cmd_test` passes `kanban_mode=args.kanban` to `run_harness()` — this is the
  interface Task 4's `run_harness()` signature must accept.

- [ ] **Step 1: Write the failing test for `choices=` rejecting an invalid value**

```python
# tests/test_cli.py — add near other test-subcommand parser tests

def test_test_subcommand_kanban_rejects_invalid_choice(capsys):
    from hermes_pipeline.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["test", "--fixture", "happy-path", "--kanban", "not-a-real-choice"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err


def test_test_subcommand_kanban_defaults_to_null():
    from hermes_pipeline.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path"])
    assert args.kanban == "null"


def test_test_subcommand_kanban_accepts_hermes():
    from hermes_pipeline.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["test", "--fixture", "happy-path", "--kanban", "hermes"])
    assert args.kanban == "hermes"
```

Note: check the actual parser-builder function name before writing this — search
`hermes_pipeline/cli.py` for `def build_parser` or equivalent (e.g. `def _build_parser`,
`def main`) and adjust the import in the test to match. The `test_parser` construction shown in
Task exploration lives inside some top-level parser-building function; use whatever that
function is actually named in this codebase.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -k kanban -v`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'kanban'` (no such flag yet)

- [ ] **Step 3: Add the `--kanban` argument to `test_parser`**

```python
# hermes_pipeline/cli.py — insert after the existing test_parser.add_argument for --convergence-threshold
# (immediately before test_parser.set_defaults(func=_cmd_test), i.e. after line 564 as read during exploration)

    test_parser.add_argument(
        "--kanban", choices=["null", "hermes"], default="null",
        help=(
            "Kanban adapter to use (default: null, no network calls). "
            "'hermes' constructs a real HermesKanbanAdapter and requires prior "
            "`hermes login` plus access to the fixture's kanban tenant "
            "(verify with: hermes kanban list --tenant mock-project)."
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -k kanban -v`
Expected: PASS

- [ ] **Step 5: Thread `kanban_mode` through `_cmd_test`**

```python
# hermes_pipeline/cli.py — replace _cmd_test (lines 1545-1560 as read during exploration)

def _cmd_test(args, config: Config) -> int:
    """Handle 'test' subcommand — mock integration test harness."""
    from .harness import run_harness
    try:
        result = run_harness(
            fixture_name=args.fixture,
            loop=args.loop,
            phase_only=args.phase,
            keep_dir=args.keep,
            timeout=args.timeout,
            convergence_threshold=args.convergence_threshold,
            kanban_mode=args.kanban,
            config=config,
        )
        if result.exit_code != 0:
            return result.exit_code
        return 0
```

(Leave the rest of the function body — exception handling, print statements — unchanged; this
step only adds the one new kwarg to the `run_harness()` call. Confirm by reading the full
function body with `sed -n '1545,1580p' hermes_pipeline/cli.py` before editing, since only the
call-site portion was captured during exploration.)

- [ ] **Step 6: Run the full CLI test file to check for regressions**

Run: `uv run pytest tests/test_cli.py -v`
Expected: All PASS (note: this will still fail at this step because `run_harness()` doesn't
accept `kanban_mode` yet — that's Task 4. It's acceptable for `test_cli.py`'s `--kanban`
argparse-only tests to pass while any test that actually invokes `_cmd_test`/`run_harness` fails
until Task 4 lands; if `test_cli.py` has such an integration test, note it here and defer its
pass to Task 4's Step 9.)

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_cli.py
git commit -m "feat(cli): add --kanban {null,hermes} flag to test subcommand"
```

---

### Task 4: Reorder `tick_id`, branch on `kanban_mode`, preflight validation, convergence-halt cleanup in `harness.py`

**Files:**
- Modify: `hermes_pipeline/harness.py:316-464` (`run_harness`, plus the `except
  ConvergenceHaltError`-adjacent handling at the `"convergence_error" in result_box` branch,
  currently lines 413-415)
- Test: `tests/test_harness.py`

**Interfaces:**
- Consumes: `KanbanOutbox(path)`, `ActiveTasksStore(path)`, `HermesKanbanAdapter(outbox,
  active_tasks)` from `kanban.py` (Task 1); `PipelineRunner(..., kanban_metadata=...)` from
  `runner.py` (Task 2); `args.kanban` → `kanban_mode: str` from `cli.py` (Task 3).
- Produces: `run_harness(*, fixture_name, loop, phase_only, keep_dir, timeout,
  convergence_threshold, kanban_mode: str, config) -> HarnessResult` — the new required kwarg
  Task 3's `_cmd_test` call site depends on. Also produces a new exception,
  `KanbanPreflightError(RuntimeError)`, raised when `kanban_mode == "hermes"` and the tenant
  preflight check fails — `_cmd_test`'s existing generic `except Exception` handler in `cli.py`
  already catches this without changes (confirm the handler is a bare `except Exception` when
  editing; if it's narrower, this step must add `KanbanPreflightError` to it).

**Terminal-state table** (for reference while implementing — from the approved design doc):

| Terminal state | Runner path | Board state after this task |
|---|---|---|
| Success (ready-for-review) | `update_phase(..., status="ready_for_review")` | Card left **live**, not cleared (merge is a separate later step) |
| Phase failure, `continue_on_failure=True` | `clear_active_task(outcome="abandoned")` (Task 2) | Card **archived** |
| Phase failure, `continue_on_failure=False` | `clear_active_task(outcome="abandoned")` (Task 2, new) | Card **archived** |
| Convergence-halt | `run_harness()`'s `ConvergenceHaltError` handler (this task, new) | Card **archived** |
| Overall timeout | bypasses all cleanup (worker thread abandoned) | Card **live** — genuinely orphaned |
| Process crash | bypasses all cleanup | Card **live** — genuinely orphaned |

- [ ] **Step 1: Write the failing test for tenant-unsuffixed regression (the CRITICAL Eng Finding 1 fix)**

```python
# tests/test_harness.py — add new test class

class TestKanbanModeHermes:
    """Tests for --kanban hermes wiring in run_harness()."""

    @patch("hermes_pipeline.kanban.subprocess.run")
    def test_kanban_hermes_uses_unsuffixed_tenant(self, mock_run, tmp_path, monkeypatch):
        """Regression test for the tenant-conflation bug: --tenant must be the fixture's
        unsuffixed project_slug, never suffixed with tick_id."""
        from unittest.mock import MagicMock

        # preflight `hermes kanban list --tenant ...` succeeds
        preflight_result = MagicMock(returncode=0, stdout="[]", stderr="")
        # set_active_task's `hermes kanban create` call
        create_result = MagicMock(returncode=0, stdout='{"id": "task-1"}', stderr="")
        mock_run.side_effect = [preflight_result, create_result] + [MagicMock(returncode=0, stdout="", stderr="")] * 20

        monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)
        # Run only phase_1 to keep the run short; assume a real phase key exists — confirm via
        # `load_phases()` output before finalizing (see Step 2 note below for how to find one).
        result = run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only="phase_1_development",  # placeholder — confirm real phase key, see Step 2
            keep_dir=True,
            timeout=60,
            convergence_threshold=3,
            kanban_mode="hermes",
            config=None,
        )

        create_call = None
        for call in mock_run.call_args_list:
            argv = call[0][0]
            if argv[:3] == ["hermes", "kanban", "create"]:
                create_call = argv
                break
        assert create_call is not None, "expected a `hermes kanban create` subprocess call"
        tenant_index = create_call.index("--tenant") + 1
        assert create_call[tenant_index] == "mock-project"  # unsuffixed, regardless of tick_id
```

Note: this test's exact phase-mocking mechanics depend on how `_dispatch_phase`/`phases.run` are
already mocked elsewhere in `test_harness.py` or `test_harness_e2e.py` — read
`tests/test_harness_e2e.py` in full before writing this step for real, and reuse whatever
subprocess/phase-stubbing fixture it already has (it almost certainly patches
`hermes_pipeline.phases.run` or `hermes_pipeline.hermes_adapter` rather than letting a real
`claude`/`hermes` subprocess run). Adjust the test body to match that existing pattern — the
assertions on `--tenant` unsuffixed-ness are the part that must not change.

- [ ] **Step 2: Determine the real `phase_only` value and phase-mocking pattern**

Run: `uv run python -c "from hermes_pipeline.phases import load_phases; print([p.phase_key for p in load_phases()])"`

Read `tests/test_harness_e2e.py` in full (`cat -n tests/test_harness_e2e.py`) and copy its
phase-stubbing approach into Step 1's test before proceeding. Update the test written in Step 1
with the real phase key and the correct mock target.

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py::TestKanbanModeHermes::test_kanban_hermes_uses_unsuffixed_tenant -v`
Expected: FAIL — `TypeError: run_harness() got an unexpected keyword argument 'kanban_mode'`

- [ ] **Step 4: Reorder `tick_id` generation and branch on `kanban_mode`**

```python
# hermes_pipeline/harness.py — replace run_harness signature (lines 316-325)

def run_harness(
    *,
    fixture_name: str,
    loop: bool,
    phase_only: str | None,
    keep_dir: bool,
    timeout: int,
    convergence_threshold: int,
    kanban_mode: str,
    config: Any,
) -> HarnessResult:
```

```python
# hermes_pipeline/harness.py — replace lines 326-392 (body from imports through PipelineRunner
# construction)

    """Main orchestration: bootstrap fixture, run pipeline, generate report."""
    import threading

    from .runner import PipelineRunner
    from .phases import load_phases
    from .test_report import generate_report, summarize_report, diff_reports, summarize_diff
    from .state import State
    from .kanban import NullKanbanAdapter, HermesKanbanAdapter, KanbanOutbox, ActiveTasksStore
    from .logging_setup import new_tick_id

    preflight_check()

    temp_dir = Path(tempfile.mkdtemp(prefix="harness-"))
    try:
        fixture = create_mock_project(temp_dir, fixture_name)

        state_dir = temp_dir / ".hermes"
        lock_dir = temp_dir / ".hermes" / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)

        events_log = temp_dir / "events.jsonl"
        base_monitor = HarnessMonitor(events_log)
        detector = ConvergenceDetector(threshold=convergence_threshold)
        error_holder: dict[str, Any] = {}
        monitor = _ConvergenceMonitor(base_monitor, detector, error_holder)

        all_phases = load_phases()
        phases = all_phases
        if phase_only:
            phases = filter_phases(all_phases, phase_only)

        # tick_id must exist before any kanban-facing identity (State, adapter, runner) is
        # constructed, since the card-body metadata needs it at construction time.
        tick_id = new_tick_id()

        kanban_metadata: dict[str, str] | None = None
        if kanban_mode == "hermes":
            kanban_outbox = KanbanOutbox(state_dir / "kanban_outbox.jsonl")
            active_tasks = ActiveTasksStore(state_dir / "active_tasks.json")
            _kanban_preflight(tenant=fixture["project_slug"])
            kanban = HermesKanbanAdapter(kanban_outbox, active_tasks)
            kanban_metadata = {
                "tick_id": tick_id,
                "fixture_name": fixture_name,
                "state_dir": str(state_dir),
            }
        else:
            kanban = NullKanbanAdapter()

        checkpoint_dir = state_dir / "pipeline_checkpoints"
        ready_dir = state_dir / "ready_for_review"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        ready_dir.mkdir(parents=True, exist_ok=True)

        state = State(
            project=fixture["project_slug"],
            lock_dir=lock_dir,
            checkpoint_dir=checkpoint_dir,
            ready_dir=ready_dir,
        )

        runner = PipelineRunner(
            project=fixture["project_slug"],
            project_dir=temp_dir,
            branch=fixture["branch"],
            todo_id=fixture["todo_id"],
            title=f"Mock TODO-{fixture['todo_id']}",
            phases=phases,
            state=state,
            kanban=kanban,
            kanban_metadata=kanban_metadata,
            run_phase_fn=lambda phase: _dispatch_phase(
                phase,
                state_dir=state_dir,
                todo_id=fixture["todo_id"],
                tick_id=tick_id,
                project_slug=fixture["project_slug"],
                project_dir=temp_dir,
                error_holder=error_holder,
            ),
            continue_on_failure=True,
            monitor=monitor,
        )
```

`fixture["project_slug"]` is passed to `State(...)`/`PipelineRunner(...)` completely unchanged
from today — this is the D1 fix (no slug mutation, ever).

- [ ] **Step 5: Add the `_kanban_preflight` helper and `KanbanPreflightError`**

```python
# hermes_pipeline/harness.py — insert after the ConvergenceHaltError class definition (after line 133)

class KanbanPreflightError(RuntimeError):
    """Raised when --kanban hermes is selected but the tenant is not accessible."""


def _kanban_preflight(*, tenant: str) -> None:
    """Fail fast if the kanban tenant isn't accessible before constructing the real adapter.

    Runs `hermes kanban list --tenant <tenant>` and raises KanbanPreflightError with an
    actionable message on non-zero exit, rather than letting the failure surface later as a
    silent non-blocking warning deep in HermesKanbanAdapter.
    """
    result = subprocess.run(
        ["hermes", "kanban", "list", "--tenant", tenant],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise KanbanPreflightError(
            f"--kanban hermes requires `hermes login` and access to tenant '{tenant}'. "
            f"Verify with: hermes kanban list --tenant {tenant}\n"
            f"Preflight error: {detail}"
        )
```

- [ ] **Step 6: Add convergence-halt cleanup**

`ConvergenceHaltError` is caught at what was originally lines 413-415 (`elif
"convergence_error" in result_box:`), inside the `with isolate_config(...)` block after
`worker.join(timeout=timeout)`. This is the point where cleanup must be added, since
`runner.run()` never reaches its own cleanup code on this path.

```python
# hermes_pipeline/harness.py — replace the convergence_error branch (was lines 413-415)

            elif "convergence_error" in result_box:
                log.warning(str(result_box["convergence_error"]))
                if kanban_mode == "hermes":
                    try:
                        kanban.clear_active_task(project=fixture["project_slug"], outcome="abandoned")
                    except Exception as e:
                        log.warning("kanban.clear_active_task (convergence-halt) failed: %s", e)
                success = False
```

- [ ] **Step 7: Add CLI-visible output for `--kanban hermes` runs**

Locate the block building `HarnessResult` (was lines 450-455) and the `summary` construction
just above it (was lines 427-439). Insert output before the return:

```python
# hermes_pipeline/harness.py — insert immediately before `return HarnessResult(...)`
# (after the `if loop:` block, was before line 448)

        if kanban_mode == "hermes":
            active_task_id = active_tasks.get(fixture["project_slug"])
            print(
                f"[kanban] tenant={fixture['project_slug']} tick_id={tick_id} "
                f"task_id={active_task_id or '(none — check outbox)'} "
                f"report={report_json} keep={'yes' if keep_dir else 'no (temp dir will be removed)'}"
            )

        exit_code = 0 if (success and not timed_out) else 1
```

`active_tasks` is the `ActiveTasksStore` instance created in Step 4's `if kanban_mode ==
"hermes":` branch — it is in scope here since both live in the same `try:` block, not nested
inside the `if kanban_mode == "hermes":` from Step 4 (confirm this when editing: the
`active_tasks` variable must be assigned at the same indentation level as `kanban`, i.e. inside
the outer `try:`, not shadowed/lost after the `if/else`).

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_harness.py::TestKanbanModeHermes::test_kanban_hermes_uses_unsuffixed_tenant -v`
Expected: PASS

- [ ] **Step 9: Write the failing test for the preflight-failure fast path**

```python
# tests/test_harness.py — inside TestKanbanModeHermes

@patch("hermes_pipeline.harness.subprocess.run")
def test_kanban_hermes_preflight_failure_raises_before_adapter_construction(self, mock_run, monkeypatch):
    from hermes_pipeline.harness import KanbanPreflightError

    preflight_fail = MagicMock(returncode=1, stdout="", stderr="not authenticated")
    mock_run.return_value = preflight_fail
    monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)

    with pytest.raises(KanbanPreflightError, match="hermes login"):
        run_harness(
            fixture_name="happy-path",
            loop=False,
            phase_only=None,
            keep_dir=False,
            timeout=60,
            convergence_threshold=3,
            kanban_mode="hermes",
            config=None,
        )
```

- [ ] **Step 10: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py::TestKanbanModeHermes::test_kanban_hermes_preflight_failure_raises_before_adapter_construction -v`
Expected: FAIL — either `TypeError` (missing kwarg, if run before Step 4) or no exception raised
(if run after Step 4 but before Step 5's wiring is complete). By this point in the plan it should
already be wired; if it unexpectedly passes here, that's fine — move on. If it fails for a
different reason than "not implemented yet" (e.g. `mock_run` patch target wrong), fix the patch
target first.

- [ ] **Step 11: Run test to verify it passes (after Steps 4-7 are in place)**

Run: `uv run pytest tests/test_harness.py::TestKanbanModeHermes::test_kanban_hermes_preflight_failure_raises_before_adapter_construction -v`
Expected: PASS

- [ ] **Step 12: Write the failing test for `--kanban null` producing zero `hermes kanban` subprocess calls**

```python
# tests/test_harness.py — inside TestKanbanModeHermes (or a sibling TestKanbanModeNull class)

@patch("hermes_pipeline.harness.subprocess.run")
def test_kanban_null_default_produces_no_kanban_subprocess_calls(self, mock_run, monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)
    # Reuse whatever phase-stubbing pattern Step 2 identified so the run completes quickly
    # without a real claude/hermes subprocess call for phase execution itself.
    ...
    run_harness(
        fixture_name="happy-path",
        loop=False,
        phase_only="<real phase key from Step 2>",
        keep_dir=True,
        timeout=60,
        convergence_threshold=3,
        kanban_mode="null",
        config=None,
    )
    kanban_calls = [
        c for c in mock_run.call_args_list
        if c[0][0][:2] == ["hermes", "kanban"]
    ]
    assert kanban_calls == []
```

- [ ] **Step 13: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py -k kanban_null -v`
Expected: FAIL — `TypeError: run_harness() got an unexpected keyword argument 'kanban_mode'` if
this is somehow run before Step 4; otherwise it should already pass since `"null"` was always the
no-network path. If it fails for an unrelated reason (e.g. phase mocking not set up correctly per
Step 2), fix that first — this test's purpose is a regression guard, not new behavior.

- [ ] **Step 14: Run test to verify it passes**

Run: `uv run pytest tests/test_harness.py -k kanban_null -v`
Expected: PASS

- [ ] **Step 15: Write the failing test for convergence-halt cleanup**

```python
# tests/test_harness.py — inside TestKanbanModeHermes

@patch("hermes_pipeline.kanban.subprocess.run")
@patch("hermes_pipeline.harness.subprocess.run")
def test_convergence_halt_clears_kanban(self, mock_harness_sp, mock_kanban_sp, monkeypatch):
    """A convergence-halt must call clear_active_task(outcome='abandoned') even though
    ConvergenceHaltError bypasses PipelineRunner.run()'s own cleanup path."""
    from unittest.mock import MagicMock

    mock_harness_sp.return_value = MagicMock(returncode=0, stdout="[]", stderr="")  # preflight
    mock_kanban_sp.side_effect = [
        MagicMock(returncode=0, stdout='{"id": "task-1"}', stderr=""),  # create
    ]
    monkeypatch.setattr("hermes_pipeline.harness.preflight_check", lambda: None)
    # Force every phase dispatch to fail with the same error_class so ConvergenceDetector
    # trips after `convergence_threshold` consecutive failures. Reuse Step 2's phase-mocking
    # pattern, but make the mocked phase runner always raise/return failure.
    ...

    run_harness(
        fixture_name="happy-path",
        loop=False,
        phase_only=None,
        keep_dir=True,
        timeout=60,
        convergence_threshold=2,
        kanban_mode="hermes",
        config=None,
    )

    archive_calls = [
        c for c in mock_kanban_sp.call_args_list
        if c[0][0][:3] == ["hermes", "kanban", "archive"]
    ]
    assert len(archive_calls) == 1
```

- [ ] **Step 16: Run test to verify it fails**

Run: `uv run pytest tests/test_harness.py -k convergence_halt_clears_kanban -v`
Expected: FAIL prior to Step 6's edit; should PASS once Step 6 is in place given the run order in
this plan — if writing tests strictly TDD-first, temporarily stash Step 6's diff, confirm this
fails, then reapply.

- [ ] **Step 17: Run test to verify it passes**

Run: `uv run pytest tests/test_harness.py -k convergence_halt_clears_kanban -v`
Expected: PASS

- [ ] **Step 18: Run the full harness test file to check for regressions**

Run: `uv run pytest tests/test_harness.py tests/test_harness_e2e.py -v`
Expected: All PASS

- [ ] **Step 19: Commit**

```bash
git add hermes_pipeline/harness.py tests/test_harness.py
git commit -m "feat(harness): wire --kanban hermes through run_harness with preflight validation and convergence-halt cleanup"
```

---

### Task 5: Wire `_cmd_test`'s call site (unblock Task 3's deferred test) and run full CLI regression

**Files:**
- Verify: `hermes_pipeline/cli.py` (already edited in Task 3 Step 5 — this task closes the loop
  now that `run_harness()` accepts `kanban_mode`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `run_harness(..., kanban_mode=...)` from Task 4.
- Produces: nothing new — this task only re-runs the deferred assertion from Task 3 Step 6.

- [ ] **Step 1: Re-run the full CLI test file now that `run_harness()` accepts `kanban_mode`**

Run: `uv run pytest tests/test_cli.py -v`
Expected: All PASS, including any `_cmd_test`/`run_harness` integration test that was deferred in
Task 3 Step 6.

- [ ] **Step 2: If any integration test still fails, diagnose and fix in `_cmd_test` only**

Do not touch `run_harness()` or its callers beyond `cli.py`'s call site — Task 4 already covers
that. If a failure surfaces here, it's almost certainly a stale kwarg name or missing `config`
handling in `_cmd_test`; fix it in place.

- [ ] **Step 3: Commit (only if Step 2 required a change; otherwise skip this task's commit)**

```bash
git add hermes_pipeline/cli.py
git commit -m "fix(cli): finish wiring --kanban through _cmd_test call site"
```

---

### Task 6: Document the flag, tenant precondition, and terminal-state table

**Files:**
- Modify: `docs/howto-mock-integration-test-harness.md`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by later tasks — this is the terminal documentation task.

- [ ] **Step 1: Read the existing howto doc structure**

Run: `cat -n docs/howto-mock-integration-test-harness.md`

- [ ] **Step 2: Add a new "Run with real kanban adapter" section**

Insert a new `##` section (placement: after the existing "Basic usage"/flag-reference section,
before any troubleshooting section — match the doc's existing heading level and style) containing:

```markdown
## Run with real kanban adapter

By default (`hermes-pipeline test --fixture <name>`, or explicitly `--kanban null`), the harness
uses a no-op kanban adapter — no network calls, no board changes. Pass `--kanban hermes` to drive
a real `HermesKanbanAdapter` against a dedicated kanban tenant instead:

```bash
hermes-pipeline test --fixture happy-path --kanban hermes
```

**Precondition:** you must be logged in (`hermes login`) with access to the `mock-project`
tenant. The harness runs a preflight check (`hermes kanban list --tenant mock-project`) before
starting any phase and fails fast with an actionable error if this doesn't succeed — it will not
silently exit 0 with no card and no local evidence.

**Tenant is never suffixed.** Every `--kanban hermes` run creates a card in the same
`mock-project` tenant; runs are distinguished by a `tick_id` recorded in each card's body, not by
a separate tenant per run. Running `--kanban hermes` twice in a row produces two distinct cards
in the same tenant, not two tenants.

**Terminal-state table** — what the board looks like after a run ends, depending on how it ended:

| Terminal state | Board state |
|---|---|
| Success (ready for review) | Card **live** — not archived; a later `merge`/`abandon` step clears it |
| Phase failure (with or without `continue_on_failure`) | Card **archived** — inspectable, not deleted |
| Convergence-halt (3+ consecutive same-class failures) | Card **archived** |
| Overall `--timeout` fires | Card **live** — genuinely orphaned; this is intentional debug signal |
| Process crash | Card **live** — genuinely orphaned; this is intentional debug signal |

A live card after a run means "the run never got to clean up" (timeout/crash) or "still waiting
on review/merge" (success). An archived card means "it failed cleanly and the card body has the
`tick_id`/fixture/state_dir context for why."

**Output.** On both success and failure, a `--kanban hermes` run prints:

```
[kanban] tenant=mock-project tick_id=01ARZ3ND... task_id=abc123 report=/tmp/harness-.../reports/report.json keep=no (temp dir will be removed)
```

Pass `--keep` to retain the temp directory (including `kanban_outbox.jsonl` and
`active_tasks.json`) for post-run inspection.

**Known limitation:** the outbox retry path (`drain_outbox`) does not currently carry the
`tick_id`/fixture metadata on a queued-and-later-retried card — only the initial synchronous
create attempt includes it. This is a pre-existing outbox-fidelity gap, not introduced by this
feature.
```

- [ ] **Step 3: Commit**

```bash
git add docs/howto-mock-integration-test-harness.md
git commit -m "docs: document --kanban hermes flag, tenant precondition, and terminal-state table"
```

---

### Task 7: Version bump (VERSION + pyproject.toml + uv.lock + CHANGELOG, together)

**Files:**
- Modify: `VERSION`, `pyproject.toml`, `uv.lock`, `CHANGELOG.md`

**Interfaces:**
- Consumes: nothing — this is a release-bookkeeping task, not a code task.
- Produces: nothing consumed by later tasks (this is the last task).

Per this repo's `CLAUDE.md`, these four files must move together in one commit, and `uv sync`
(not hand-editing) must regenerate `uv.lock`'s `hermes-pipeline` entry.

- [ ] **Step 1: Bump the version**

Run: `cat VERSION` — current is `0.4.11`. This is a minor feature addition (new opt-in flag, no
breaking change to defaults), so bump the minor version: `0.5.0`.

```bash
echo "0.5.0" > VERSION
```

- [ ] **Step 2: Update `pyproject.toml`**

```toml
# pyproject.toml — change line 3
version = "0.5.0"
```

- [ ] **Step 3: Regenerate `uv.lock`**

Run: `uv sync`

- [ ] **Step 4: Add the CHANGELOG entry**

Insert at the top of `CHANGELOG.md` (match the existing entry format observed via `head -10
CHANGELOG.md` before writing):

```markdown
## [0.5.0] - 2026-07-16
- Add `--kanban {null,hermes}` flag to `hermes-pipeline test` — opt-in real kanban adapter for
  the mock integration test harness, wired to a dedicated tenant with tick_id-labeled card
  bodies (TODO-20). Default (`null`) behavior is unchanged.
- Fix invalid `KanbanOutcome` literal (`"failed"` → `"abandoned"`) and close two silent
  kanban-cleanup gaps (`continue_on_failure=False` phase failure, convergence-halt).
```

- [ ] **Step 5: Verify sync across all four files**

```bash
cat VERSION
grep '^version' pyproject.toml
grep -A1 'name = "hermes-pipeline"' uv.lock
head -10 CHANGELOG.md
```

Expected: all three version references read `0.5.0`, and the CHANGELOG's top entry is `##
[0.5.0]`.

- [ ] **Step 6: Run the full test suite one final time**

Run: `uv run pytest -v`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add VERSION pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore: bump version to 0.5.0 for TODO-20 kanban adapter feature"
```

---

## Self-Review

**Spec coverage** (against the approved design doc's Revised Next Steps 1-10 and Revised Success
Criteria):
1. `--kanban {null,hermes}` flag with `choices=` → Task 3. ✅
2. `kanban_mode` threaded `_cmd_test` → `run_harness()` → Tasks 3 & 5. ✅
3. `tick_id` reordered before State/PipelineRunner construction → Task 4 Step 4. ✅
4. `kanban_mode` branch: null unchanged, hermes constructs real adapter with unsuffixed
   `project_slug` as tenant → Task 4 Step 4. ✅
5. Metadata channel (`set_active_task(..., metadata=...)`), wired end-to-end
   `run_harness()`→`PipelineRunner`→`set_active_task()` → Tasks 1, 2, 4. ✅
6. `KanbanOutcome`/terminal-state fixes: invalid `"failed"` literal, `continue_on_failure=False`
   gap, convergence-halt gap → Task 2 (first two), Task 4 Step 6 (convergence-halt). ✅
   `merge.py` signature mismatch explicitly tracked as out-of-scope follow-up, not fixed — per
   Global Constraints. ✅
7. Preflight validation (`hermes kanban list --tenant`) with actionable error → Task 4 Steps 5,
   9-11. ✅
8. CLI-visible tick_id/report path/keep/task id output → Task 4 Step 7. ✅
9. Tests: null-zero-calls, hermes-unsuffixed-tenant, metadata-in-body, terminal-state coverage
   (`continue_on_failure=False`, convergence-halt), CLI choices= rejection, preflight failure →
   Tasks 1, 2, 3, 4 (all Step "write failing test" entries). ✅
10. Howto doc update with flag, precondition, terminal-state table, CLI output → Task 6. ✅

**Placeholder scan:** Two spots intentionally defer an exact literal to be looked up during
implementation rather than guessed: Task 4 Step 1's `phase_only="phase_1_development"` placeholder
(Step 2 requires running `load_phases()` to get the real key before finalizing) and Task 3 Step 1's
note to confirm the parser-builder function name. Both are flagged explicitly as "confirm before
finalizing" rather than silently wrong — this is a deliberate call-out, not a skipped step, because
the actual phase keys and parser function name were not visible in the file excerpts read during
planning and guessing them risks giving the implementer wrong code. No other placeholders,
"TBD"/"handle it later" language, or unshown code blocks remain.

**Type consistency:** `KanbanClient.set_active_task`'s `metadata: dict[str, str] | None = None`
signature (Task 1) matches its use at `PipelineRunner.run()`'s call site (Task 2 Step 11) and at
`run_harness()`'s `kanban_metadata` dict construction (Task 4 Step 4) — all three use the same
key names (`tick_id`, `fixture_name`, `state_dir`) and `dict[str, str]` shape.
`clear_active_task(project=..., outcome="abandoned")` call signature is consistent across Task 2
(runner.py, two sites) and Task 4 (harness.py convergence-halt site) — all three pass `project`
as a keyword and `outcome` as a keyword, matching the unchanged `KanbanClient` Protocol at
`kanban.py:66-73`. `run_harness(..., kanban_mode: str, ...)` signature in Task 4 Step 4 matches
the call site added in Task 3 Step 5 and Task 4's own test calls throughout.
