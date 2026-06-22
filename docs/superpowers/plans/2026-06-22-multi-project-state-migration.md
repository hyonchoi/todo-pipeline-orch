# Multi-project Per-Project State Migration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move per-project state (`current_tick_id.txt`, `circuit.json`, `outcomes/`) from the global `~/.hermes/` into `<project>/.hermes/` and add automatic migration for existing projects.

**Architecture:** Create `hermes_pipeline/state_migration.py` with a one-time migration function. The migration runs inside the `TickLock` before the scan loop starts. Existing functions in `cli.py` that read/write state files already accept `state_dir` — this plan ensures the per-project `state_dir` is threaded through all call sites.

**Tech Stack:** Python 3.12+, `uv`, no new dependencies.

## Global Constraints

- One TODO in flight per project (existing constraint)
- Hermes-only for LLM queries (TODO-6)
- Filesystem-based config — no database or network calls before selection
- Python 3.12+, `uv`-managed
- Per-project state lives at `<project>/.hermes/`
- Global state (`~/.hermes/`) retains only `tick.lock` and `config.toml`

---

### Context: Files Already with `state_dir` Parameters

The following functions in `cli.py` and `kanban_tasks.py` already accept `state_dir` and need to be called with the per-project state directory when running in the scan loop:

- `_read_prior_tick_id(state_dir)` — reads `current_tick_id.txt`
- `_persist_tick_id(state_dir, tick_id)` — writes `current_tick_id.txt` + sentinel
- `_make_circuit_breaker(state_dir, cb_cfg, slack_channel)` — creates `circuit.json`
- `all_phases_complete(project, prior_tick_id, state_dir)` — checks kanban + outcomes
- `observe_outcomes(state_dir, tick_id, status_map)` — writes to `outcomes/`
- `build_context(tick_id, state_dir, ...)` — already takes `state_dir`

Functions that stay global:
- `_load_toml_overlay(state_dir, config)` — `config.toml` is shared
- `TickLock(state_dir, ...)` — single lock for the scan

### Task 1: Create state_migration module

**Files:**
- Create: `hermes_pipeline/state_migration.py`
- Test: `tests/test_state_migration.py`

**Interfaces:**
- Consumes: Nothing from other tasks
- Produces: `_migrate_global_state(project_dir, config)`, `_get_project_state_dir(project_dir)`

- [ ] **Step 1: Write the failing test**

```python
import json
import shutil
from pathlib import Path

from hermes_pipeline.state_migration import _get_project_state_dir, _migrate_global_state

from hermes_pipeline.config import Config


def test_get_project_state_dir(tmp_path: Path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    result = _get_project_state_dir(project_dir)
    assert result == project_dir / ".hermes"


def test_migrate_current_tick_id(tmp_path: Path):
    """Migration moves current_tick_id.txt from global to per-project dir."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    # Set up global state
    (global_state / "current_tick_id.txt").write_text("abc123\n")

    _migrate_global_state(project_dir, config)

    # Should be in per-project dir now
    per_project_state = project_dir / ".hermes"
    assert (per_project_state / "current_tick_id.txt").exists()
    assert (per_project_state / "current_tick_id.txt").read_text().strip() == "abc123"
    # Global file should be gone
    assert not (global_state / "current_tick_id.txt").exists()


def test_migrate_circuit_json(tmp_path: Path):
    """Migration moves circuit.json from global to per-project dir."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    circuit_data = {"consecutive_no_progress": 3}
    (global_state / "circuit.json").write_text(json.dumps(circuit_data))

    _migrate_global_state(project_dir, config)

    per_project_state = project_dir / ".hermes"
    assert (per_project_state / "circuit.json").exists()
    data = json.loads((per_project_state / "circuit.json").read_text())
    assert data["consecutive_no_progress"] == 3
    assert not (global_state / "circuit.json").exists()


def test_migrate_outcomes_dir(tmp_path: Path):
    """Migration moves outcomes/ directory from global to per-project dir."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    # Set up outcomes directory with a file
    outcomes_dir = global_state / "outcomes"
    outcomes_dir.mkdir()
    (outcomes_dir / "abc123-phases.json").write_text('{"outcome": "phase_complete"}\n')

    _migrate_global_state(project_dir, config)

    per_project_state = project_dir / ".hermes"
    assert (per_project_state / "outcomes").is_dir()
    assert (per_project_state / "outcomes" / "abc123-phases.json").exists()
    assert not (global_state / "outcomes").exists()


def test_migrate_skips_if_already_migrated(tmp_path: Path):
    """Migration does not overwrite per-project files that already exist."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    per_project_state = project_dir / ".hermes"
    per_project_state.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    # Per-project already has data
    (per_project_state / "current_tick_id.txt").write_text("existing\n")
    (global_state / "current_tick_id.txt").write_text("global\n")

    _migrate_global_state(project_dir, config)

    # Per-project data preserved
    assert (per_project_state / "current_tick_id.txt").read_text().strip() == "existing"


def test_migrate_no_op_when_no_global_state(tmp_path: Path):
    """Migration does nothing when global state files don't exist."""
    global_state = tmp_path / "global"
    global_state.mkdir()
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    config = Config(
        projects_dir=tmp_path,
        state_dir=global_state,
    )

    _migrate_global_state(project_dir, config)

    # Per-project dir should not be created
    per_project_state = project_dir / ".hermes"
    assert not per_project_state.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_state_migration.py -v`
Expected: FAIL with "cannot import name '_get_project_state_dir' from 'hermes_pipeline.state_migration'"

- [ ] **Step 3: Write minimal implementation**

```python
"""State migration for multi-project support.

One-time migration of per-project state files from the global state directory
(~/.hermes/) to per-project directories (<project>/.hermes/).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


def _get_project_state_dir(project_dir: Path) -> Path:
    """Return the per-project state directory path (<project>/.hermes/)."""
    return project_dir / ".hermes"


def _migrate_global_state(project_dir: Path, config: Config) -> None:
    """One-time: move per-project state from config.state_dir to <project>/.hermes/.

    Files moved:
      - current_tick_id.txt
      - circuit.json
      - outcomes/ (directory)

    If the per-project state directory already contains a file, the global
    version is not overwritten (migration already done or manual setup).

    Args:
        project_dir: Project root directory.
        config: Global config with state_dir pointing to the global state.
    """
    project_state = _get_project_state_dir(project_dir)
    project_state.mkdir(exist_ok=True)

    global_state = config.state_dir

    for name in ("current_tick_id.txt", "circuit.json"):
        src = global_state / name
        dst = project_state / name
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            log.info("migrated %s -> %s", src, dst)

    outcomes_src = global_state / "outcomes"
    outcomes_dst = project_state / "outcomes"
    if outcomes_src.is_dir() and not outcomes_dst.exists():
        shutil.move(str(outcomes_src), str(outcomes_dst))
        log.info("migrated outcomes/ -> %s", outcomes_dst)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_state_migration.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/state_migration.py tests/test_state_migration.py
git commit -m "feat: add per-project state migration module

Add _migrate_global_state() to move current_tick_id.txt, circuit.json,
and outcomes/ from global ~/.hermes/ to <project>/.hermes/ on first tick."
```

### Task 2: Update kanban_tasks.py all_phases_complete per-project state_dir

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py:182-224` (the `all_phases_complete` function)
- Test: `tests/test_kanban_tasks.py` (add new tests)

**Interfaces:**
- Consumes: None from other tasks
- Produces: `all_phases_complete` reads `expected-phases.json` from the given `state_dir` instead of hardcoded `.hermes/outcomes/`

Currently `all_phases_complete` uses a hardcoded `Path(".hermes") / "outcomes"` for the expected-phases sentinel check. With per-project state, this path should be derived from the `state_dir` parameter.

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path
from unittest.mock import patch

from hermes_pipeline.kanban_tasks import all_phases_complete


def test_all_phases_complete_reads_expected_from_state_dir(tmp_path: Path):
    """expected-phases.json should be read from state_dir, not .hermes/."""
    # Set up state_dir
    state_dir = tmp_path / "myproject" / ".hermes"
    state_dir.mkdir(parents=True)
    outcomes_dir = state_dir / "outcomes"
    outcomes_dir.mkdir()

    expected_file = outcomes_dir / "expected-phases.json"
    expected_file.write_text(json.dumps(["P1_research", "P2_implementation"]))

    # Mock get_todo_kanban_status to return completion statuses
    with patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
        return_value={"P1_research": "done", "P2_implementation": "done"},
    ):
        result = all_phases_complete(
            tenant="myproject",
            tick_id="abc123",
            state_dir=state_dir,
        )
        assert result is True


def test_all_phases_complete_partial_reg_from_state_dir(tmp_path: Path):
    """Missing phase in status map should be detected using state_dir sentinel."""
    state_dir = tmp_path / "myproject" / ".hermes"
    state_dir.mkdir(parents=True)
    outcomes_dir = state_dir / "outcomes"
    outcomes_dir.mkdir()

    expected_file = outcomes_dir / "expected-phases.json"
    expected_file.write_text(json.dumps(["P1_research", "P2_implementation", "P3_review"]))

    # Only 2 phases in status map, expected has 3
    with patch(
        "hermes_pipeline.kanban_tasks.get_todo_kanban_status",
        return_value={"P1_research": "done", "P2_implementation": "done"},
    ):
        result = all_phases_complete(
            tenant="myproject",
            tick_id="abc123",
            state_dir=state_dir,
        )
        assert result is False  # Partial registration detected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban_tasks.py::test_all_phases_complete_reads_expected_from_state_dir -v`
Expected: FAIL — currently `expected-phases.json` is read from `Path(".hermes") / "outcomes"` regardless of `state_dir`.

- [ ] **Step 3: Fix all_phases_complete to use state_dir**

In `hermes_pipeline/kanban_tasks.py`, in the `all_phases_complete` function, replace:

```python
    # Guard against partial registration: if we have an expected-phases
    # sentinel, verify all expected phases are in the status map.
    try:
        outcomes_dir = Path(".hermes") / "outcomes"
        expected_file = outcomes_dir / "expected-phases.json"
```

with:

```python
    # Guard against partial registration: if we have an expected-phases
    # sentinel, verify all expected phases are in the status map.
    try:
        state_dir_path = Path(state_dir) if state_dir else Path(".hermes")
        outcomes_dir = state_dir_path / "outcomes"
        expected_file = outcomes_dir / "expected-phases.json"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kanban_tasks.py::test_all_phases_complete_reads_expected_from_state_dir -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
git commit -m "fix: use state_dir for expected-phases sentinel in all_phases_complete

The expected-phases.json check was hardcoded to .hermes/outcomes/.
With per-project state, read from state_dir parameter."
```

### Task 3: Update _persist_expected_phases to accept project_dir

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py:77-95` (the `_persist_expected_phases` function)
- Test: `tests/test_kanban_tasks.py` (add new test)

**Interfaces:**
- Consumes: None from other tasks
- Produces: `_persist_expected_phases` takes a `project_dir` parameter and writes to `<project>/.hermes/outcomes/expected-phases.json` instead of hardcoded `.hermes/outcomes/`

Currently `_persist_expected_phases` writes to `Path(".hermes") / "outcomes"`. With per-project state, this should be derived from the project directory.

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path

from hermes_pipeline.kanban_tasks import _persist_expected_phases


class FakePhase:
    """Minimal phase object for testing."""
    def __init__(self, key):
        self.phase_key = key
        self.name = key
        self.prompt = ""
        self.turns = 1


def test_persist_expected_phases_writes_to_project_hermes_dir(tmp_path: Path):
    """_persist_expected_phases should write to project_dir/.hermes/outcomes/."""
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()

    phases = [FakePhase("P1_research"), FakePhase("P2_implementation")]

    _persist_expected_phases(phases, project_dir=project_dir)

    expected = project_dir / ".hermes" / "outcomes" / "expected-phases.json"
    assert expected.exists()
    data = json.loads(expected.read_text())
    assert data == ["P1_research", "P2_implementation"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban_tasks.py::test_persist_expected_phases_writes_to_project_hermes_dir -v`
Expected: FAIL — `_persist_expected_phases` doesn't accept `project_dir` parameter and writes to hardcoded `.hermes/outcomes/`.

- [ ] **Step 3: Fix _persist_expected_phases to accept project_dir**

Replace:

```python
def _persist_expected_phases(phases: list) -> None:
    """Write expected phase keys to a sentinel file for crash recovery.

    Called after successful registration so all_phases_complete can verify
    all expected phases are present (guards against partial registration).
    """
    try:
        phase_keys = [p.phase_key for p in phases]
        outcomes_dir = Path(".hermes") / "outcomes"
        outcomes_dir.mkdir(parents=True, exist_ok=True)
        # Overwrite previous — only the latest registration matters.
        sentinel = outcomes_dir / "expected-phases.json"
        sentinel.write_text(json.dumps(phase_keys, sort_keys=False))
    except OSError:
        # Best-effort — don't fail registration if we can't write the sentinel.
        log.warning("failed to persist expected phases sentinel")
```

with:

```python
def _persist_expected_phases(
    phases: list,
    *,
    project_dir: Path | str | None = None,
) -> None:
    """Write expected phase keys to a sentinel file for crash recovery.

    Called after successful registration so all_phases_complete can verify
    all expected phases are present (guards against partial registration).

    Args:
        phases: List of phase objects.
        project_dir: If given, write to <project_dir>/.hermes/outcomes/.
            Defaults to .hermes/outcomes/ for backward compatibility.
    """
    try:
        phase_keys = [p.phase_key for p in phases]
        if project_dir is not None:
            outcomes_dir = Path(project_dir) / ".hermes" / "outcomes"
        else:
            outcomes_dir = Path(".hermes") / "outcomes"
        outcomes_dir.mkdir(parents=True, exist_ok=True)
        # Overwrite previous — only the latest registration matters.
        sentinel = outcomes_dir / "expected-phases.json"
        sentinel.write_text(json.dumps(phase_keys, sort_keys=False))
    except OSError:
        # Best-effort — don't fail registration if we can't write the sentinel.
        log.warning("failed to persist expected phases sentinel")
```

And update the call site in `register_todo_phases` at the end of the function (after the for loop that creates tasks), change:

```python
    # Persist expected phase keys so all_phases_complete can verify
    # completeness (guards against partial registration on crash).
    _persist_expected_phases(phases)
```

to:

```python
    # Persist expected phase keys so all_phases_complete can verify
    # completeness (guards against partial registration on crash).
    _persist_expected_phases(phases, project_dir=project_dir)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kanban_tasks.py::test_persist_expected_phases_writes_to_project_hermes_dir -v`
Expected: PASS

Also run existing tests to ensure backward compatibility:

Run: `uv run pytest tests/test_kanban_tasks.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
git commit -m "fix: add project_dir parameter to _persist_expected_phases

With per-project state, write expected-phases.json to
<project_dir>/.hermes/outcomes/ instead of hardcoded .hermes/outcomes/."
```

### Task 4: Verify full test suite passes

**Files:**
- No code changes — verification only.

- [ ] **Step 1: Run affected test suites**

Run: `uv run pytest tests/test_state_migration.py tests/test_kanban_tasks.py tests/test_tick_subcommand.py -v`
Expected: All PASS

- [ ] **Step 2: Commit (if any fixes needed from Step 1)**

```bash
git commit -m "fix: resolve test failures from state migration changes"
```

---
