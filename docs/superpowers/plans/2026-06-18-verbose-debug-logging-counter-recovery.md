# Verbose/Debug Logging + Counter Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--verbose`/`--debug` CLI flags for observability (TODO-13) and a `recover-counter` subcommand for counter initialization (TODO-1).

**Architecture:** TODO-13 uses a two-pass argparse parse (global flags extracted first to configure logging), a `pipeline.verbose` module logger for verbose-level info, and targeted `log.debug()` calls at ~6 strategic spots. TODO-1 introduces `hermes_pipeline/counter.py` with `recover_counter()` and a `recover-counter` CLI subcommand. Implement sequentially — TODO-13 first, then TODO-1 — due to shared file (`cli.py`).

**Tech Stack:** Python 3.12+, argparse, logging, pytest, uv

## Global Constraints

- Python 3.12+, managed via `uv`. Use `uv run` for test/execution commands.
- Default log level stays INFO (per review finding #1). `--verbose` enables `pipeline.verbose` logger at INFO. `--debug` lowers root to DEBUG.
- Counter semantic: `max(existing, scanned)`, never decrease (per review finding #3).
- Debug truncation at 2000 chars (per review finding #8).
- No test coverage required for TODO-13 (per design Decisions). TODO-1 requires tests.
- 281+ existing tests must pass — no regressions.
- `hermes_pipeline` package lives at repo root (no `src/` layout).

---

### Task 1: Add level parameter to logging_setup.configure()

**Files:**
- Modify: `hermes_pipeline/logging_setup.py`
- Test: `tests/test_logging_setup.py`

**Interfaces:**
- Produces: `configure(log_path, retention_days, level)` — new `level` parameter (default `logging.INFO`)
- Produces: `pipeline.verbose` logger set to `logging.WARNING` by default (gated, off by default)

- [ ] **Step 1: Write a failing test for the new level parameter**

```python
# In tests/test_logging_setup.py

def test_configure_with_debug_level(tmp_path):
    """configure() with level=DEBUG allows DEBUG messages through stderr."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7, level=logging.DEBUG)
    set_tick_id("01TESTTICKULID0000000000BB")
    logging.getLogger("hermes_pipeline.test2").debug("debug message")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "debug message" in text

def test_configure_default_level_is_info(tmp_path):
    """Default level is INFO — DEBUG messages should not appear."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7)
    set_tick_id("01TESTTICKULID0000000000CC")
    logging.getLogger("hermes_pipeline.test3").debug("should not appear")
    logging.getLogger("hermes_pipeline.test3").info("should appear")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "should appear" in text
    assert "should not appear" not in text

def test_verbose_logger_gated_by_default(tmp_path):
    """pipeline.verbose logger is WARNING by default — INFO messages hidden."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7)
    set_tick_id("01TESTTICKULID0000000000DD")
    vlog = logging.getLogger("pipeline.verbose")
    vlog.info("verbose info message")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "verbose info message" not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_logging_setup.py::test_configure_with_debug_level -v`
Expected: FAIL — `configure()` doesn't accept `level` parameter yet

- [ ] **Step 3: Modify configure() to accept level parameter and set up pipeline.verbose logger**

```python
# In hermes_pipeline/logging_setup.py — replace the configure function

def configure(log_path: Path, retention_days: int = 7, level: int = logging.INFO) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s tick_id=%(tick_id)s %(name)s %(message)s"
    )
    file_h = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=retention_days, encoding="utf-8",
    )
    file_h.setFormatter(fmt)
    file_h.addFilter(_TickFilter())
    err_h = logging.StreamHandler(sys.stderr)
    err_h.setFormatter(fmt)
    err_h.addFilter(_TickFilter())
    root = logging.getLogger()
    root.handlers = [file_h, err_h]
    root.setLevel(level)

    # pipeline.verbose logger — INFO when --verbose, WARNING (off) by default.
    verbose_logger = logging.getLogger("pipeline.verbose")
    verbose_logger.setLevel(logging.WARNING)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_logging_setup.py -v`
Expected: All tests PASS (including existing tests)

- [ ] **Step 5: Add test for --verbose enabling the pipeline.verbose logger**

```python
# In tests/test_logging_setup.py — append to the file

def test_verbose_logger_enabled_at_info(tmp_path):
    """When root level is INFO and verbose logger level is set to INFO, verbose messages appear."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7, level=logging.INFO)
    set_tick_id("01TESTTICKULID0000000000EE")
    vlog = logging.getLogger("pipeline.verbose")
    vlog.setLevel(logging.INFO)  # Simulates --verbose flag
    vlog.info("verbose info message enabled")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "verbose info message enabled" in text
```

- [ ] **Step 6: Run all logging tests**

Run: `uv run pytest tests/test_logging_setup.py -v`
Expected: All 7 tests PASS

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/logging_setup.py tests/test_logging_setup.py
git commit -m "feat: add level parameter to logging_setup.configure() + pipeline.verbose logger"
```

---

### Task 2: Add --verbose/--debug global flags to CLI

**Files:**
- Modify: `hermes_pipeline/cli.py`
- Test: No new tests (per design Decisions — no test coverage required for TODO-13)

**Interfaces:**
- Consumes: `configure()` with `level` parameter from Task 1
- Produces: `--verbose` and `--debug` flags on main parser (available to all subcommands)
- Produces: Two-pass parse in `main()` — global flags extracted first to configure logging

- [ ] **Step 1: Add --verbose and --debug flags to build_parser() before add_subparsers()**

In `build_parser()`, after the `--version` argument and before `add_subparsers()`:

```python
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Increase log detail (selection results, lock state)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Maximum log detail (raw agent payloads, circuit breaker internals)",
    )
```

- [ ] **Step 2: Modify main() to use two-pass parse and configure logging with the right level**

Replace the `main()` function body:

```python
def main(argv: Optional[list[str]] = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        argv: Command-line arguments (default: sys.argv[1:]).

    Returns:
        Exit code (0 on success, 2 on error).
    """
    # Two-pass parse: extract global flags first so logging can be configured
    # before the subcommand runs.
    parser = build_parser()
    first_pass, _ = parser.parse_known_args(argv)
    verbose = getattr(first_pass, "verbose", False)
    debug = getattr(first_pass, "debug", False)

    # Load config
    config = Config.from_env()

    # Configure logging based on flags
    log_path = config.state_dir / config.log_file_subpath
    if debug:
        configure_logging(log_path, config.log_retention_days, level=logging.DEBUG)
    elif verbose:
        configure_logging(log_path, config.log_retention_days, level=logging.INFO)
        logging.getLogger("pipeline.verbose").setLevel(logging.INFO)
    else:
        configure_logging(log_path, config.log_retention_days)

    # Full parse
    args = parser.parse_args(argv)

    # Dispatch to subcommand
    if hasattr(args, "func"):
        return args.func(args, config)
    else:
        parser.print_help()
        return 0
```

- [ ] **Step 3: Verify existing tests still pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: All existing tests PASS (the existing `TestMain` tests use `main()` which now uses two-pass parse — the behavior for subcommand-only args should be unchanged)

- [ ] **Step 4: Commit**

```bash
git add hermes_pipeline/cli.py
git commit -m "feat: add --verbose/--debug global flags with two-pass parse"
```

---

### Task 3: Add verbose & debug logging in cli.py _cmd_tick

**Files:**
- Modify: `hermes_pipeline/cli.py`

**Interfaces:**
- Consumes: `pipeline.verbose` logger from Task 1
- Produces: `vlog.info()` for selection result summary in `_cmd_tick`

- [ ] **Step 1: Add vlog import and add verbose logging in _cmd_tick**

At the top of `cli.py`, after the `log = logging.getLogger(__name__)` line, add:

```python
vlog = logging.getLogger("pipeline.verbose")
```

In `_cmd_tick()`, after the selection result (`picked = decision.picked`), add:

```python
            picked = decision.picked

            vlog.info("selection result: picked=%s rationale=%s", picked, decision.rationale[:200])
```

- [ ] **Step 2: Add verbose logging for lock acquisition in _cmd_tick**

In `_cmd_tick()`, just before the `with tick_lock.acquire(tick_id):` line, add:

```python
            # --- Step 2: Acquire lock ---
            tick_lock = TickLock(state_dir, max_age_min=cb_cfg.max_tick_duration_min)

            tick_id = _generate_tick_id()

            vlog.info("acquiring tick lock: lock_dir=%s tick_id=%s", tick_lock.lock_dir, tick_id)

In `_cmd_tick()`, after the `with tick_lock.acquire(tick_id):` line (inside the context manager, so the lock is held), add a debug log for the lock holder details. Place it near the top of the `with` block:

```python
            try:
                with tick_lock.acquire(tick_id):
                    log.debug("tick lock acquired: lock_file=%s holder_pid=%d",
                              tick_lock._holder_path(), os.getpid())
```

- [ ] **Step 3: Add debug logging for selection agent data in _cmd_tick**

After `decision = run_selection(...)`, add debug logging:

```python
            decision = run_selection(
                tick_id=tick_id,
                ctx=ctx,
                cfg=full_cfg,
            )

            log.debug("selection decision: picked=%s candidates=%s rationale=%s",
                      decision.picked, decision.candidates_considered, decision.rationale[:500])
```

- [ ] **Step 4: Add verbose logging for lock release**

In the `finally` block or after the `with tick_lock.acquire(tick_id):` context manager exits successfully, lock release is implicit. Add a vlog line at the end of the `with` block (before the `except TickLockHeld`):

The lock release is in the `finally` of `tick_lock.acquire()`. Add a vlog line right before `return 0` at the end of the `with` block:

Actually, the lock is released by the context manager's `__exit__`. Add a vlog line at the end of the `try` block inside `with tick_lock.acquire(tick_id):`, just before the `return 0` on the success path:

Find the `return 0` at the end of the `with tick_lock.acquire(tick_id):` block (line ~638) and add before it:

```python
            vlog.info("tick lock released: tick_id=%s", tick_id)

            return 0
```

- [ ] **Step 5: Verify existing tests still pass**

Run: `uv run pytest tests/test_cli.py tests/test_tick_subcommand.py -v`
Expected: All tests PASS — new vlog/log.debug calls should not affect behavior at default INFO level

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/cli.py
git commit -m "feat: add verbose/debug logging in _cmd_tick"
```

---

### Task 4: Add debug logging in decision/agent.py

**Files:**
- Modify: `hermes_pipeline/decision/agent.py`

**Interfaces:**
- Consumes: Standard logging at DEBUG level (gated, zero cost when --debug is not used)
- Produces: `log.debug()` for rendered prompt (truncated to 2000 chars) and raw response in `call_agent()`

- [ ] **Step 1: Add logging import and debug calls in call_agent()**

Add at the top of `hermes_pipeline/decision/agent.py`, after the existing imports:

```python
import logging

log = logging.getLogger(__name__)
```

In `call_agent()`, after the `rendered = build_prompt(...)` line and before the `_hermes_call`, add:

```python
    rendered = build_prompt(prompt_path, ctx)
    log.debug("agent prompt (truncated to 2000 chars): %s", rendered[:2000])
    raw = _hermes_call(model=model, max_tokens=max_tokens, prompt=rendered)
    log.debug("agent raw response (truncated to 2000 chars): %s", raw[:2000])
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `uv run pytest tests/test_decision_agent.py -v`
Expected: All tests PASS — DEBUG calls don't fire at default INFO level

- [ ] **Step 3: Commit**

```bash
git add hermes_pipeline/decision/agent.py
git commit -m "feat: add debug logging for agent prompt/response in call_agent"
```

---

### Task 5: Add debug logging in circuit.py

**Files:**
- Modify: `hermes_pipeline/circuit.py`

**Interfaces:**
- Consumes: Standard logging at DEBUG level
- Produces: `log.debug()` for circuit breaker state transitions in `observe()`

Note: circuit.py currently has NO logging infrastructure. Must add `import logging` and `log = logging.getLogger(__name__)`.

- [ ] **Step 1: Add logging import and logger to circuit.py**

At the top of `hermes_pipeline/circuit.py`, after `from pathlib import Path`, add:

```python
import logging

log = logging.getLogger(__name__)
```

- [ ] **Step 2: Add debug logging for state transitions in observe()**

In the `observe()` method of `CircuitBreaker`, add debug logging at key decision points.

After `st = self._load()`, add:

```python
    def observe(self, *, picked: str | None, counts_as_no_progress: bool) -> None:
        st = self._load()
        log.debug("circuit breaker observe: picked=%s counts_as_no_progress=%s state=%s",
                  picked, counts_as_no_progress, st)
```

After the counter reset when `picked is not None`:

```python
        if picked is not None:
            st["consecutive_no_progress"] = 0
            if st.get("backed_off"):
                log.debug("circuit breaker: resuming from backoff (was backed_off=True)")
                _set_cron_interval(minutes=5)
                st["backed_off"] = False
            self._save(st)
            return
```

After the backoff trigger:

```python
            if dedup_ok:
                log.debug("circuit breaker: sending slack alert after %d consecutive no-progress ticks",
                          st["consecutive_no_progress"])
                _send_slack(
```

And after the backoff state change:

```python
            _set_cron_interval(minutes=self.backoff_interval_min)
            st["backed_off"] = True
            log.debug("circuit breaker: backed off to %d min interval", self.backoff_interval_min)
```

- [ ] **Step 3: Add debug logging in observe_from_outcomes()**

At the start of `observe_from_outcomes()`, add:

```python
        phases_file = state_dir / "outcomes" / f"{prior_tick_id}-phases.json"
        if not phases_file.exists():
            log.debug("circuit breaker: no outcomes file for tick %s — counting as no-progress", prior_tick_id)
            return self.observe(picked=None, counts_as_no_progress=True)
```

- [ ] **Step 4: Verify existing tests still pass**

Run: `uv run pytest tests/test_circuit.py -v`
Expected: All tests PASS — DEBUG calls don't fire at default INFO level

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/circuit.py
git commit -m "feat: add debug logging for circuit breaker state transitions"
```

---

### Task 6: Add debug logging in kanban.py (HermesKanbanAdapter)

**Files:**
- Modify: `hermes_pipeline/kanban.py`

**Interfaces:**
- Consumes: Standard logging at DEBUG level
- Produces: `log.debug()` for kanban registration payload in `set_active_task()`

The `log` variable already exists in kanban.py (`log = logging.getLogger(__name__)`). Just add the debug calls.

- [ ] **Step 1: Add debug logging in set_active_task() of HermesKanbanAdapter**

In the `set_active_task()` method, after the `_run_cmd` call succeeds and before parsing the task_id, add:

```python
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
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `uv run pytest tests/test_kanban.py -v`
Expected: All tests PASS — DEBUG calls don't fire at default INFO level

- [ ] **Step 3: Commit**

```bash
git add hermes_pipeline/kanban.py
git commit -m "feat: add debug logging for kanban registration payload"
```

---

### Task 7: Manual verification for TODO-13

**Files:**
- No code changes — manual verification step

**Interfaces:**
- Consumes: All changes from Tasks 1-6

- [ ] **Step 1: Verify --debug flag surfaces debug output**

Run: `uv run pipeline-watch tick --debug <project>` (use a real project or dry-run)
Expected: Log output includes debug lines from agent.py (prompt/response truncated), circuit.py (state transitions), kanban.py (registration payload)

- [ ] **Step 2: Verify --verbose flag surfaces verbose output**

Run: `uv run pipeline-watch tick --verbose <project>`
Expected: Log output includes vlog.info lines (selection result, lock acquisition/release) but NOT debug lines

- [ ] **Step 3: Verify default (no flag) behavior is unchanged**

Run: `uv run pipeline-watch tick <project>`
Expected: Same log output as before — only INFO and above. No debug or verbose messages.

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/ -v --ignore=tests/eval --ignore=tests/regression`
Expected: All 281+ existing tests PASS

- [ ] **Step 5: Commit**

No code changes to commit. Move on to TODO-1.

---

### Task 8: Create hermes_pipeline/counter.py with recover_counter()

**Files:**
- Create: `hermes_pipeline/counter.py`
- Test: `tests/test_counter.py`

**Interfaces:**
- Produces: `recover_counter(project_dir: Path) -> int` — scans TODOS.md for TODO-N, writes max to counter file

- [ ] **Step 1: Write failing tests for recover_counter**

Create `tests/test_counter.py`:

```python
"""Tests for counter.py — recover_counter and auto-initialize logic."""

from pathlib import Path
from hermes_pipeline.counter import recover_counter

COUNTER_SUBPATH = ".hermes/todo_id_counter"

class TestRecoverCounter:
    """Test recover_counter() function."""

    def test_happy_path(self, tmp_path):
        """TODO-1..5 in TODOS.md → writes 5 to counter."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-1: Do something\n- TODO-2: Do another\n- TODO-5: Do fifth\n"
        )
        result = recover_counter(project_dir)
        assert result == 5
        counter_file = project_dir / COUNTER_SUBPATH
        assert counter_file.exists()
        assert counter_file.read_text() == "5"

    def test_existing_counter_higher(self, tmp_path):
        """Counter at 8, max in TODOS.md is 4 → keeps 8."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-1: Do something\n- TODO-4: Do fourth\n"
        )
        (project_dir / ".hermes").mkdir()
        (project_dir / COUNTER_SUBPATH).write_text("8")
        result = recover_counter(project_dir)
        assert result == 8
        assert (project_dir / COUNTER_SUBPATH).read_text() == "8"

    def test_scanned_higher_than_existing(self, tmp_path):
        """Counter at 3, max in TODOS.md is 7 → writes 7."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-1: Do something\n- TODO-7: Do seventh\n"
        )
        (project_dir / ".hermes").mkdir()
        (project_dir / COUNTER_SUBPATH).write_text("3")
        result = recover_counter(project_dir)
        assert result == 7
        assert (project_dir / COUNTER_SUBPATH).read_text() == "7"

    def test_no_todos_file(self, tmp_path):
        """TODOS.md missing → raises FileNotFoundError."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        import pytest
        with pytest.raises(FileNotFoundError):
            recover_counter(project_dir)

    def test_no_todo_entries(self, tmp_path):
        """TODOS.md with no TODO-N entries → writes 0."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\nNo todos yet.\n"
        )
        result = recover_counter(project_dir)
        assert result == 0
        counter_file = project_dir / COUNTER_SUBPATH
        assert counter_file.exists()
        assert counter_file.read_text() == "0"

    def test_creates_hermes_dir(self, tmp_path):
        """.hermes/ doesn't exist → creates it + writes counter."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-3: Do third\n"
        )
        # .hermes/ does NOT exist
        assert not (project_dir / ".hermes").exists()
        result = recover_counter(project_dir)
        assert result == 3
        assert (project_dir / ".hermes").is_dir()
        assert (project_dir / COUNTER_SUBPATH).read_text() == "3"

    def test_corrupt_counter_file(self, tmp_path):
        """Counter file contains non-integer → treats as 0, uses scanned max."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-4: Do fourth\n"
        )
        (project_dir / ".hermes").mkdir()
        (project_dir / COUNTER_SUBPATH).write_text("not-a-number")
        result = recover_counter(project_dir)
        assert result == 4
        assert (project_dir / COUNTER_SUBPATH).read_text() == "4"

    def test_empty_counter_file(self, tmp_path):
        """Counter file is empty → treats as 0, uses scanned max."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-2: Do second\n"
        )
        (project_dir / ".hermes").mkdir()
        (project_dir / COUNTER_SUBPATH).write_text("")
        result = recover_counter(project_dir)
        assert result == 2
        assert (project_dir / COUNTER_SUBPATH).read_text() == "2"

    def test_ignores_todo_references_in_body(self, tmp_path):
        """TODO-N in body text (not as entry) is still matched — this is correct behavior per design."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text(
            "# TODOS\n\n- TODO-1: Depends on TODO-6\n- TODO-3: Standalone\n"
        )
        result = recover_counter(project_dir)
        # TODO-6 appears in body text; it's matched by the regex. This is the
        # same regex the agent validation uses. If TODO-6 is referenced but
        # not a real entry, the counter is set higher — that's fine, it just
        # means the counter won't collide.
        assert result == 6

    def test_both_empty(self, tmp_path):
        """No counter file and no TODO entries → writes 0."""
        project_dir = tmp_path / "demo"
        project_dir.mkdir()
        (project_dir / "TODOS.md").write_text("# TODOS\n\n")
        result = recover_counter(project_dir)
        assert result == 0
        assert (project_dir / COUNTER_SUBPATH).read_text() == "0"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_counter.py -v`
Expected: FAIL — module doesn't exist yet

- [ ] **Step 3: Create hermes_pipeline/counter.py**

```python
"""Counter recovery — scan TODOS.md for max TODO-N and initialize .hermes/todo_id_counter."""

from __future__ import annotations
import re
from pathlib import Path

TODO_ID_RE = re.compile(r"\bTODO-(\d+)\b")


def recover_counter(project_dir: Path) -> int:
    """Scan TODOS.md for TODO-N entries and initialize/update the counter file.

    Reads project_dir / "TODOS.md", finds the maximum N in TODO-N patterns,
    and writes max(existing_counter, scanned_max) to
    project_dir / ".hermes" / "todo_id_counter".

    If the counter file exists and has a higher value than the scanned max
    (e.g., completed TODOs were removed), the existing counter is preserved.
    This prevents ID resurrection.

    Args:
        project_dir: Path to the project root (containing TODOS.md).

    Returns:
        The counter value after recovery.

    Raises:
        FileNotFoundError: If TODOS.md doesn't exist in the project directory.
    """
    todos_path = project_dir / "TODOS.md"
    if not todos_path.exists():
        raise FileNotFoundError(f"TODOS.md not found in {project_dir}")

    # Scan TODOS.md for TODO-N patterns
    todos_content = todos_path.read_text()
    scanned_ids = [int(m) for m in TODO_ID_RE.findall(todos_content)]
    scanned_max = max(scanned_ids) if scanned_ids else 0

    # Read existing counter (if any)
    counter_path = project_dir / ".hermes" / "todo_id_counter"
    existing_value = 0
    if counter_path.exists():
        try:
            existing_value = int(counter_path.read_text().strip())
        except (ValueError, OSError):
            # Corrupt or unreadable counter — treat as 0
            existing_value = 0

    # Use the maximum of existing and scanned (never decrease)
    result = max(existing_value, scanned_max)

    # Write the counter file (create .hermes/ if needed)
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text(str(result))

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_counter.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/counter.py tests/test_counter.py
git commit -m "feat: add recover_counter() function with tests"
```

---

### Task 9: Add recover-counter subcommand to CLI

**Files:**
- Modify: `hermes_pipeline/cli.py`
- Test: No new tests (CLI subcommand tested via integration with Task 8's unit tests)

**Interfaces:**
- Consumes: `recover_counter()` from Task 8
- Produces: `recover-counter` subcommand with `project` argument

- [ ] **Step 1: Add recover-counter subcommand to build_parser()**

In `build_parser()`, after the `tick` subcommand and before `return parser`:

```python
    # recover-counter: Scan TODOS.md and initialize counter file
    rc_parser = subparsers.add_parser(
        "recover-counter",
        help="Scan TODOS.md and initialize .hermes/todo_id_counter",
    )
    rc_parser.add_argument("project", help="Project name/slug")
    rc_parser.set_defaults(func=_cmd_recover_counter)
```

- [ ] **Step 2: Implement _cmd_recover_counter handler**

Add the handler function before `main()`:

```python
def _cmd_recover_counter(args, config: Config) -> int:
    """Handle 'recover-counter' subcommand."""
    project = args.project

    # Validate project slug
    if not _validate_project_slug(project):
        log.error("invalid project slug: %r (must be alphanumeric, dot, dash, underscore)", project)
        return 2

    project_dir = config.projects_dir / project
    if not project_dir.exists():
        log.error("project not found: %s", project)
        return 2

    from .counter import recover_counter

    try:
        result = recover_counter(project_dir)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 2
    except Exception as e:
        log.error("recover-counter failed: %s", e)
        return 2

    log.info("recover-counter: set counter to %d for project %s", result, project)
    print(f"Counter set to {result} for project {project}")
    return 0
```

- [ ] **Step 3: Verify CLI help shows the new subcommand**

Run: `uv run pipeline-watch --help`
Expected: `recover-counter` appears in the subcommands list

- [ ] **Step 4: Test the subcommand with --help**

Run: `uv run pipeline-watch recover-counter --help`
Expected: Shows help for recover-counter with project argument

- [ ] **Step 5: Verify existing tests still pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: All existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/cli.py
git commit -m "feat: add recover-counter CLI subcommand"
```

---

### Task 10: Manual verification for TODO-1

**Files:**
- No code changes — manual verification step

**Interfaces:**
- Consumes: All changes from Tasks 8-9

- [ ] **Step 1: Test recover-counter with a real project**

Create a test project:

```bash
mkdir -p /tmp/test-recover-counter/demo
echo "# TODOS\n\n- TODO-1: First\n- TODO-5: Fifth\n- TODO-3: Third\n" > /tmp/test-recover-counter/demo/TODOS.md
PIPELINE_PROJECTS_DIR=/tmp/test-recover-counter uv run pipeline-watch recover-counter demo
```

Expected: Output: "Counter set to 5 for project demo"

- [ ] **Step 2: Verify the counter file was created**

```bash
cat /tmp/test-recover-counter/demo/.hermes/todo_id_counter
```

Expected: File contains `5`

- [ ] **Step 3: Run recover-counter again — counter should stay at 5**

```bash
PIPELINE_PROJECTS_DIR=/tmp/test-recover-counter uv run pipeline-watch recover-counter demo
```

Expected: "Counter set to 5 for project demo" (no change)

- [ ] **Step 4: Test with missing TODOS.md**

```bash
mkdir -p /tmp/test-recover-counter/empty
PIPELINE_PROJECTS_DIR=/tmp/test-recover-counter uv run pipeline-watch recover-counter empty
```

Expected: Error message about TODOS.md not found, exit code 2

- [ ] **Step 5: Test with missing project**

```bash
PIPELINE_PROJECTS_DIR=/tmp/test-recover-counter uv run pipeline-watch recover-counter nonexistent
```

Expected: Error message about project not found, exit code 2

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/ -v --ignore=tests/eval --ignore=tests/regression`
Expected: All 281+ existing tests + 10 new counter tests PASS (291+ total)

- [ ] **Step 7: Cleanup**

```bash
rm -rf /tmp/test-recover-counter
```

- [ ] **Step 8: Commit**

No code changes to commit. Final full test run serves as the commit gate.

---

### Task 11: Final integration and commit

**Files:**
- All modified files

**Interfaces:**
- Consumes: All prior changes

- [ ] **Step 1: Run the full test suite one final time**

Run: `uv run pytest tests/ -v --ignore=tests/eval --ignore=tests/regression`
Expected: All tests PASS

- [ ] **Step 2: Squash TODO-13 commits into one**

```bash
git rebase -i HEAD~7
```

Mark Tasks 1-7 commits as `squash` into the first commit. Message:

```
feat: add --verbose/--debug logging with debug instrumentation (TODO-13)

- Add level parameter to logging_setup.configure()
- Add --verbose/--debug global flags with two-pass parse
- Add pipeline.verbose logger for verbose-level info
- Add vlog.info() for selection results and lock state
- Add log.debug() in agent.py, circuit.py, kanban.py, cli.py
```

- [ ] **Step 3: Squash TODO-1 commits into one**

```bash
git rebase -i HEAD~3
```

Mark Tasks 8-10 commits as `squash` into the first commit. Message:

```
feat: add recover-counter CLI subcommand + counter.py (TODO-1)

- Add recover_counter() function with tests
- Add recover-counter subcommand to CLI
- Counter semantic: max(existing, scanned), never decrease
- Handles: missing TODOS.md, no TODO entries, corrupt counter, empty .hermes/
```

- [ ] **Step 4: Verify git log looks clean**

```bash
git log --oneline -5
```

Expected:

```
feat: add recover-counter CLI subcommand + counter.py (TODO-1)
feat: add --verbose/--debug logging with debug instrumentation (TODO-13)
<previous commit>
```

- [ ] **Step 5: Final manual smoke test**

```bash
uv run pipeline-watch --help
uv run pipeline-watch recover-counter --help
uv run pipeline-watch tick --verbose --help
uv run pipeline-watch tick --debug --help
```

Expected: All commands show correct help text
