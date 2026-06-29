# Ship Gate + Approval for the Kanban Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Phase 9 "ship gate" that holds every completed TODO in-flight until a human runs a deterministic `pipeline-watch approve <project> --todo TODO-N` command that bumps the version in the PR, merges to main, and completes the gate — with a one-time "ready to ship" Slack alert.

**Architecture:** The gate is a kanban task `phase_9_ship` created at registration time with `--initial-status blocked` and never dispatched to an agent (a pure marker). Because `blocked ∉ COMPLETION_STATUSES`, `all_phases_complete` keeps returning False, so `_tick_project` treats the project as in-flight and never selects a new TODO. A new `maybe_ship_ready` helper, called in the prior-tick branch *before* the `all_phases_complete` early-return, detects "all phases done except the blocked gate," writes a `<tick_id>-ship.json` sidecar recording the PR head SHA, and fires a one-time Slack alert. The new `approve` command reads that sidecar, runs an all-deterministic guard set (clean tree, SHA-staleness, CI-green), bumps the version *on the work branch inside the PR*, squash-merges with `--match-head-commit`, then completes the gate task so the next tick advances.

**Tech Stack:** Python 3.12+, `uv`, `argparse`, `fcntl`, `subprocess` shelling out to `hermes`/`gh`/`git`, `pytest` + `pytest-mock` (`mocker`).

## Global Constraints

- Python 3.12+, managed via `uv`. Run every test with `uv run pytest`.
- Merge-to-main is a **one-way door**: every guard in the approve path is deterministic Python — never an LLM decision.
- `blocked ∉ COMPLETION_STATUSES` (`frozenset({"done", "failed"})`) — this is the entire mechanism by which the gate holds the project in-flight. Do not add `blocked` to that set.
- `--force` requires a **double pass** (`--force --force`): argparse `action="count"`, the command requires `count >= 2`. Force bypasses **only** the SHA-staleness guard. It never bypasses the dirty-tree guard or the CI-red guard. Every force use is **audit-logged**.
- All sidecar writes are **atomic**: write a temp file, then `os.rename` over the target.
- `approve` is serialized with `fcntl.flock` on `<state_dir>/approve.lock` — **not** `TickLock` (TickLock's stale-reclamation semantics are wrong for a human-driven merge).
- The "ready to ship" Slack alert fires **exactly once** per ship, deduped by the existence of the ship sidecar.
- The gate phase (`phase_9_ship`) is a marker: created `blocked`, **no** `--goal` flags, no agent ever runs it.
- **Reuse `hermes_pipeline/slack.py::notify(channel, message)`** for the alert. Do **not** rename `circuit.py::_send_slack` (six `tests/test_circuit.py` patches depend on that name). This supersedes the original design's task T6.
- **Bump-in-PR-before-merge:** the version bump is committed and pushed to the work branch and included in the squash merge. The bump invalidates the reviewed SHA, so after pushing, the sidecar re-baselines `pr_head_sha` to the bumped SHA and records `bump_version`, making a retry (after CI goes green) skip re-bumping.
- `hermes kanban complete <task_id>` is the command that transitions the gate task `blocked → done` (confirmed in `hermes_pipeline/kanban.py`).
- The work branch is read from `<state_dir>/pipeline_branch.txt` (written by Phase 2, step 5 of `configs/phases.yaml`).

---

## File Structure

**New files:**
- `hermes_pipeline/ship.py` — ship-gate domain logic: `ShipSidecar` dataclass + atomic sidecar IO + `find_ship_sidecar`; `gh`/`git` subprocess wrappers + CI-green parser; `bump_in_pr`; `resolve_ship_task`; `approve_lock`; `ApproveRefused`/`ShipError`; `_check_ship_guards`; `_bump_and_merge`; `approve_ship`; `maybe_ship_ready`.
- `tests/test_ship.py` — unit tests for everything in `ship.py` (sidecar IO, guards, bump, transaction, detection).
- `tests/test_approve_cli.py` — tests for the `approve` subcommand wiring (`_cmd_approve`, parser).

**Modified files:**
- `hermes_pipeline/phases.py` — add `Phase.gate: bool = False`; make `prompt`/`tools`/`turns` optional so a gate needs no LLM fields.
- `configs/phases.yaml` — add `phase_9_ship` (gate); move `terminal: true` from phase 8 to phase 9.
- `hermes_pipeline/kanban_tasks.py` — add `BLOCKED` constant; gate branch in `register_todo_phases`; new `KanbanTaskInfo` + `get_todo_kanban_tasks`.
- `hermes_pipeline/cli.py` — add `_cmd_approve`; add `approve` subparser; wire `maybe_ship_ready` into `_tick_project`; update module docstring.
- `tests/test_phases.py`, `tests/test_kanban_tasks.py` — extend with gate cases.

---

## Task 1: Add `gate` flag and optional LLM fields to `Phase`

**Files:**
- Modify: `hermes_pipeline/phases.py:12-19` (the `Phase` dataclass)
- Test: `tests/test_phases.py`

**Interfaces:**
- Produces: `Phase(phase_key: str, name: str, prompt: str = "", tools: str = "", turns: int = 0, timeout: int = 1800, terminal: bool = False, gate: bool = False)` — a frozen dataclass. `load_phases(path) -> list[Phase]` unchanged in signature.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_phases.py`:

```python
def test_gate_phase_needs_no_llm_fields(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(
        """
phases:
  - phase_key: "phase_9_ship"
    name: "Phase 9: Ship Gate"
    gate: true
    terminal: true
"""
    )
    phases = load_phases(p)
    assert len(phases) == 1
    gate = phases[0]
    assert gate.gate is True
    assert gate.terminal is True
    assert gate.prompt == ""
    assert gate.tools == ""
    assert gate.turns == 0


def test_non_gate_phase_defaults_gate_false(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(FIXTURE)
    phases = load_phases(p)
    assert phases[0].gate is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_phases.py::test_gate_phase_needs_no_llm_fields -v`
Expected: FAIL — `TypeError: __init__() missing ... 'prompt'` (current `Phase` requires `prompt`, `tools`, `turns`).

- [ ] **Step 3: Write minimal implementation**

In `hermes_pipeline/phases.py`, replace the `Phase` dataclass:

```python
@dataclass(frozen=True)
class Phase:
    phase_key: str
    name: str
    prompt: str = ""
    tools: str = ""
    turns: int = 0
    timeout: int = 1800
    terminal: bool = False
    gate: bool = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_phases.py -v`
Expected: PASS (all, including the two new tests and the pre-existing `test_load_phases_from_yaml`).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/phases.py tests/test_phases.py
git commit -m "feat(phases): add Phase.gate flag and optional LLM fields"
```

---

## Task 2: Add `phase_9_ship` gate to `configs/phases.yaml`

**Files:**
- Modify: `configs/phases.yaml` (the `phase_8_finish_branch` entry and end of file)
- Test: `tests/test_phases.py`

**Interfaces:**
- Consumes: `Phase.gate` from Task 1.
- Produces: the canonical phases list now ends with a non-LLM gate `phase_9_ship`; `phase_8_finish_branch` is no longer `terminal`.

**Note:** No existing test loads the real `configs/phases.yaml` with a length/terminal assertion (`tests/test_phases_invoke.py` fabricates its own `Phase` objects), so moving `terminal` does not break them. Removing `terminal` from phase 8 means `_invoke_hermes` no longer writes a `ReadyForReview` record for the real pipeline — intended, because the ship-gate flow obtains PR info from `gh` directly, not from `ReadyForReview`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_phases.py`:

```python
def test_real_phases_yaml_ends_with_blocked_gate():
    phases = load_phases()  # default: configs/phases.yaml
    keys = [p.phase_key for p in phases]
    assert keys[-1] == "phase_9_ship"
    gate = phases[-1]
    assert gate.gate is True
    # Phase 8 must no longer be terminal — the gate replaces the
    # ready-for-review handoff.
    phase_8 = next(p for p in phases if p.phase_key == "phase_8_finish_branch")
    assert phase_8.terminal is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_phases.py::test_real_phases_yaml_ends_with_blocked_gate -v`
Expected: FAIL — last key is `phase_8_finish_branch`, and `phase_8.terminal` is `True`.

- [ ] **Step 3: Edit the YAML**

In `configs/phases.yaml`, change the `phase_8_finish_branch` entry to remove the `terminal: true` line:

```yaml
  - phase_key: "phase_8_finish_branch"
    name: "Phase 8: Finish Branch"
    prompt: "Use superpowers finishing-a-development-branch. Open a PR and HALT — do NOT merge."
    tools: "Read,Write,Bash"
    turns: 15
    timeout: 1800
```

Then append a new gate entry at the end of the file:

```yaml
  - phase_key: "phase_9_ship"
    name: "Phase 9: Ship Gate"
    gate: true
    terminal: true
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_phases.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add configs/phases.yaml tests/test_phases.py
git commit -m "feat(phases): add phase_9_ship blocked gate, move terminal off phase 8"
```

---

## Task 3: Register the gate task as `--initial-status blocked` (skip `--goal`)

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py` (constants near line 29; the `register_todo_phases` command-building loop near lines 118-132)
- Test: `tests/test_kanban_tasks.py`

**Interfaces:**
- Consumes: `Phase.gate` from Task 1.
- Produces: `BLOCKED = "blocked"` module constant; `register_todo_phases` emits `--initial-status blocked` and omits `--goal`/`--goal-max-turns` for gate phases.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_kanban_tasks.py` (the `FakePhase` namedtuple-style helper used by other tests does not set `gate`; the implementation reads it with `getattr(..., "gate", False)`, so add a gate-aware fake here):

```python
class FakeGatePhase:
    def __init__(self, phase_key, name="P", prompt="", tools="", turns=0, gate=False):
        self.phase_key = phase_key
        self.name = name
        self.prompt = prompt
        self.tools = tools
        self.turns = turns
        self.gate = gate


def test_gate_phase_registered_blocked_without_goal(tmp_path, mocker):
    phases = [
        FakeGatePhase("phase_8_finish_branch", name="P8", turns=15),
        FakeGatePhase("phase_9_ship", name="Ship Gate", gate=True),
    ]
    mocker.patch("hermes_pipeline.kanban_tasks.load_phases", return_value=phases)
    mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout='{"id": "t_x"}', stderr="")

    from hermes_pipeline.kanban_tasks import register_todo_phases
    register_todo_phases(
        todo_id="TODO-5",
        tick_id="01TICK",
        board_slug="demo",
        project_dir=tmp_path,
    )

    gate_cmd = mock_run.call_args_list[1][0][0]
    assert "--initial-status" in gate_cmd
    assert gate_cmd[gate_cmd.index("--initial-status") + 1] == "blocked"
    assert "--goal" not in gate_cmd

    phase8_cmd = mock_run.call_args_list[0][0][0]
    assert "--goal" in phase8_cmd
    assert "--initial-status" not in phase8_cmd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban_tasks.py::test_gate_phase_registered_blocked_without_goal -v`
Expected: FAIL — `--initial-status` not present; `--goal` present on the gate command.

- [ ] **Step 3: Write minimal implementation**

In `hermes_pipeline/kanban_tasks.py`, near the existing status constants (around line 29) add:

```python
# A "blocked" kanban task is a GATE, not an error: it deliberately holds the
# project in-flight (blocked ∉ COMPLETION_STATUSES) until a human approves.
BLOCKED = "blocked"
```

Then in `register_todo_phases`, replace the goal-flag block (currently `cmd.extend(["--goal", "--goal-max-turns", str(phase.turns)])`) with:

```python
        # Gate phases are pure markers: created blocked, never dispatched to
        # an agent. Everything else runs as a goal-mode kanban task.
        if getattr(phase, "gate", False):
            cmd.extend(["--initial-status", BLOCKED])
        else:
            cmd.extend(["--goal", "--goal-max-turns", str(phase.turns)])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_kanban_tasks.py -v`
Expected: PASS (new test plus all existing register tests — they use non-gate fakes, so `getattr` returns `False` and they keep getting `--goal`).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
git commit -m "feat(kanban): register gate phase as --initial-status blocked"
```

---

## Task 4: Add `get_todo_kanban_tasks` (task_id + status per phase)

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py` (add after `get_todo_kanban_status`)
- Test: `tests/test_kanban_tasks.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class KanbanTaskInfo: task_id: str; phase_key: str; status: str; todo_id: str`
  - `get_todo_kanban_tasks(tenant: str, tick_id: str) -> dict[str, KanbanTaskInfo]` — maps `phase_key → KanbanTaskInfo` for every task whose body-header `tick_id` matches. Empty dict on any CLI failure.

**Note:** This intentionally mirrors `get_todo_kanban_status`'s `hermes kanban list --tenant ... --json` parse rather than refactoring it, to avoid regressing the existing tested function. Later tasks need the `task_id` (to complete the gate) and the `todo_id` (to match the sidecar), which the status-only function discards.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_kanban_tasks.py`:

```python
def test_get_todo_kanban_tasks_returns_ids_and_status(mocker):
    import json as _json
    tasks = [
        {
            "id": "t_8",
            "status": "done",
            "body": _json.dumps(
                {"tick_id": "01TICK", "phase_key": "phase_8_finish_branch",
                 "todo_id": "TODO-5", "project_slug": "demo"},
                sort_keys=True,
            ) + "\nbody text",
        },
        {
            "id": "t_9",
            "status": "blocked",
            "body": _json.dumps(
                {"tick_id": "01TICK", "phase_key": "phase_9_ship",
                 "todo_id": "TODO-5", "project_slug": "demo"},
                sort_keys=True,
            ) + "\ngate",
        },
        {
            "id": "t_other",
            "status": "done",
            "body": _json.dumps(
                {"tick_id": "OTHER", "phase_key": "phase_9_ship",
                 "todo_id": "TODO-9", "project_slug": "demo"},
                sort_keys=True,
            ),
        },
    ]
    mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout=_json.dumps(tasks), stderr="")

    from hermes_pipeline.kanban_tasks import get_todo_kanban_tasks
    out = get_todo_kanban_tasks("demo", "01TICK")

    assert set(out) == {"phase_8_finish_branch", "phase_9_ship"}
    assert out["phase_9_ship"].task_id == "t_9"
    assert out["phase_9_ship"].status == "blocked"
    assert out["phase_9_ship"].todo_id == "TODO-5"
    assert out["phase_8_finish_branch"].task_id == "t_8"


def test_get_todo_kanban_tasks_empty_on_cli_failure(mocker):
    mock_run = mocker.patch("hermes_pipeline.kanban_tasks.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=1, stdout="", stderr="boom")
    from hermes_pipeline.kanban_tasks import get_todo_kanban_tasks
    assert get_todo_kanban_tasks("demo", "01TICK") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kanban_tasks.py::test_get_todo_kanban_tasks_returns_ids_and_status -v`
Expected: FAIL — `ImportError: cannot import name 'get_todo_kanban_tasks'`.

- [ ] **Step 3: Write minimal implementation**

In `hermes_pipeline/kanban_tasks.py`, add `from dataclasses import dataclass` to the imports (if not already present), then after `get_todo_kanban_status`:

```python
@dataclass(frozen=True)
class KanbanTaskInfo:
    """One kanban task, resolved by phase_key for a single tick."""
    task_id: str
    phase_key: str
    status: str
    todo_id: str


def get_todo_kanban_tasks(tenant: str, tick_id: str) -> dict[str, KanbanTaskInfo]:
    """Query kanban for all tasks of a tick, return {phase_key: KanbanTaskInfo}.

    Like get_todo_kanban_status but preserves the task id and todo id so
    callers can complete the gate task and match it to a ship sidecar.
    Returns an empty dict if no tasks match or the CLI fails.
    """
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--tenant", tenant, "--json"],
            capture_output=True,
            text=True,
            timeout=HERMES_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            return {}
        snapshot = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        log.warning("kanban list failed for tenant=%s", tenant)
        return {}

    tasks = snapshot if isinstance(snapshot, list) else snapshot.get("tasks", [])

    out: dict[str, KanbanTaskInfo] = {}
    for task in tasks:
        body = task.get("body", "")
        first_line = body.split("\n")[0]
        try:
            header = json.loads(first_line)
        except (json.JSONDecodeError, IndexError):
            continue
        if header.get("tick_id") != tick_id:
            continue
        phase_key = header.get("phase_key")
        if not phase_key:
            continue
        out[phase_key] = KanbanTaskInfo(
            task_id=task.get("id", ""),
            phase_key=phase_key,
            status=task.get("status", "unknown"),
            todo_id=header.get("todo_id", ""),
        )
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_kanban_tasks.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
git commit -m "feat(kanban): add get_todo_kanban_tasks with task ids"
```

---

## Task 5: `ShipSidecar` dataclass + atomic sidecar IO

**Files:**
- Create: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Produces:
  - `@dataclass class ShipSidecar: tick_id: str; todo_id: int; pr_number: int; pr_head_sha: str; base_branch: str; work_branch: str; phase_8_task_id: str | None = None; bump_version: str | None = None`
  - `write_sidecar(sidecar: ShipSidecar, *, state_dir: Path | str) -> Path` — atomic temp+rename into `<state_dir>/outcomes/<tick_id>-ship.json`.
  - `read_sidecar(state_dir: Path | str, tick_id: str) -> ShipSidecar | None`
  - `find_ship_sidecar(state_dir: Path | str, todo_id: int) -> ShipSidecar | None` — scans `<state_dir>/outcomes/*-ship.json`, returns the one whose `todo_id` matches (newest by filename if several).
  - `delete_sidecar(state_dir: Path | str, tick_id: str) -> None` — best-effort removal.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ship.py`:

```python
from pathlib import Path

import pytest

from hermes_pipeline.ship import (
    ShipSidecar,
    write_sidecar,
    read_sidecar,
    find_ship_sidecar,
    delete_sidecar,
)


def _sidecar(**kw):
    base = dict(
        tick_id="01TICK",
        todo_id=5,
        pr_number=42,
        pr_head_sha="abc123",
        base_branch="main",
        work_branch="todo-5-feature",
        phase_8_task_id="t_8",
        bump_version=None,
    )
    base.update(kw)
    return ShipSidecar(**base)


def test_write_then_read_roundtrip(tmp_path):
    sc = _sidecar()
    path = write_sidecar(sc, state_dir=tmp_path)
    assert path == tmp_path / "outcomes" / "01TICK-ship.json"
    assert path.exists()
    got = read_sidecar(tmp_path, "01TICK")
    assert got == sc


def test_read_missing_returns_none(tmp_path):
    assert read_sidecar(tmp_path, "NOPE") is None


def test_write_is_atomic_no_temp_left(tmp_path):
    write_sidecar(_sidecar(), state_dir=tmp_path)
    leftovers = list((tmp_path / "outcomes").glob("*.tmp"))
    assert leftovers == []


def test_find_by_todo_id(tmp_path):
    write_sidecar(_sidecar(tick_id="01AAA", todo_id=5), state_dir=tmp_path)
    write_sidecar(_sidecar(tick_id="01BBB", todo_id=9), state_dir=tmp_path)
    got = find_ship_sidecar(tmp_path, 9)
    assert got is not None
    assert got.todo_id == 9
    assert got.tick_id == "01BBB"
    assert find_ship_sidecar(tmp_path, 123) is None


def test_delete_sidecar(tmp_path):
    write_sidecar(_sidecar(), state_dir=tmp_path)
    delete_sidecar(tmp_path, "01TICK")
    assert read_sidecar(tmp_path, "01TICK") is None
    # idempotent
    delete_sidecar(tmp_path, "01TICK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hermes_pipeline.ship'`.

- [ ] **Step 3: Write minimal implementation**

Create `hermes_pipeline/ship.py`:

```python
"""Ship-gate domain logic: the deterministic merge-to-main path.

A completed TODO is held in-flight by a blocked `phase_9_ship` kanban task.
`maybe_ship_ready` detects that state, records a sidecar, and alerts once.
`approve_ship` runs an all-deterministic guard set, bumps the version inside
the PR, squash-merges, and completes the gate task.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SHIP_SIDECAR_SUFFIX = "-ship.json"


@dataclass
class ShipSidecar:
    tick_id: str
    todo_id: int
    pr_number: int
    pr_head_sha: str
    base_branch: str
    work_branch: str
    phase_8_task_id: str | None = None
    bump_version: str | None = None


def _outcomes_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "outcomes"


def _sidecar_path(state_dir: Path | str, tick_id: str) -> Path:
    return _outcomes_dir(state_dir) / f"{tick_id}{SHIP_SIDECAR_SUFFIX}"


def write_sidecar(sidecar: ShipSidecar, *, state_dir: Path | str) -> Path:
    """Atomically write the ship sidecar (temp file + os.rename)."""
    target = _sidecar_path(state_dir, sidecar.tick_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(dataclasses.asdict(sidecar), sort_keys=True))
    os.rename(tmp, target)
    return target


def read_sidecar(state_dir: Path | str, tick_id: str) -> ShipSidecar | None:
    path = _sidecar_path(state_dir, tick_id)
    if not path.exists():
        return None
    try:
        return ShipSidecar(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("corrupt ship sidecar %s: %s", path, e)
        return None


def find_ship_sidecar(state_dir: Path | str, todo_id: int) -> ShipSidecar | None:
    out_dir = _outcomes_dir(state_dir)
    if not out_dir.exists():
        return None
    matches: list[ShipSidecar] = []
    for path in sorted(out_dir.glob(f"*{SHIP_SIDECAR_SUFFIX}")):
        try:
            sc = ShipSidecar(**json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError):
            continue
        if sc.todo_id == todo_id:
            matches.append(sc)
    return matches[-1] if matches else None


def delete_sidecar(state_dir: Path | str, tick_id: str) -> None:
    try:
        _sidecar_path(state_dir, tick_id).unlink()
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): ShipSidecar dataclass with atomic sidecar IO"
```

---

## Task 6: `gh`/`git` subprocess wrappers + CI-green parser

**Files:**
- Modify: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Produces:
  - `class ShipError(Exception)` — unexpected subprocess failure (maps to a non-refusal error exit).
  - `GH_TIMEOUT = 60`, `GIT_TIMEOUT = 60` constants.
  - `gh_pr_view(branch: str, *, cwd: Path | str) -> dict` — runs `gh pr view <branch> --json number,state,headRefOid,baseRefName,headRefName,statusCheckRollup`; raises `ShipError` on non-zero exit.
  - `gh_pr_merge_squash(branch: str, *, match_head: str, cwd: Path | str) -> None` — `gh pr merge <branch> --squash --match-head-commit <sha>`; raises `ShipError` on failure.
  - `git_tree_clean(cwd: Path | str) -> bool` — `git status --porcelain` empty.
  - `ci_is_green(checks: list) -> bool` — `True` if every rollup entry is success; an **empty** list is `True` (no checks configured) with a warning logged by the caller.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
from hermes_pipeline.ship import (
    ShipError, gh_pr_view, gh_pr_merge_squash, git_tree_clean, ci_is_green,
)


def test_gh_pr_view_parses_json(mocker, tmp_path):
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(
        returncode=0, stdout='{"number": 42, "state": "OPEN"}', stderr="")
    out = gh_pr_view("todo-5-feature", cwd=tmp_path)
    assert out["number"] == 42
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["gh", "pr", "view"]
    assert "--json" in cmd


def test_gh_pr_view_raises_on_failure(mocker, tmp_path):
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=1, stdout="", stderr="no pr")
    with pytest.raises(ShipError):
        gh_pr_view("nope", cwd=tmp_path)


def test_gh_pr_merge_squash_uses_match_head(mocker, tmp_path):
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")
    gh_pr_merge_squash("todo-5-feature", match_head="deadbeef", cwd=tmp_path)
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["gh", "pr", "merge"]
    assert "--squash" in cmd
    assert cmd[cmd.index("--match-head-commit") + 1] == "deadbeef"


def test_git_tree_clean(mocker, tmp_path):
    mock_run = mocker.patch("hermes_pipeline.ship.subprocess.run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")
    assert git_tree_clean(tmp_path) is True
    mock_run.return_value = mocker.Mock(returncode=0, stdout=" M file.py\n", stderr="")
    assert git_tree_clean(tmp_path) is False


def test_ci_is_green():
    assert ci_is_green([]) is True  # no checks configured
    assert ci_is_green([{"status": "COMPLETED", "conclusion": "SUCCESS"}]) is True
    assert ci_is_green([{"state": "SUCCESS"}]) is True
    assert ci_is_green([{"status": "IN_PROGRESS", "conclusion": ""}]) is False
    assert ci_is_green([{"status": "COMPLETED", "conclusion": "FAILURE"}]) is False
    assert ci_is_green([{"state": "PENDING"}]) is False
    assert ci_is_green([
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
    ]) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_ci_is_green -v`
Expected: FAIL — `ImportError: cannot import name 'gh_pr_view'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/ship.py` (add `import subprocess` to the imports):

```python
GH_TIMEOUT = 60
GIT_TIMEOUT = 60
_GH_PR_VIEW_FIELDS = "number,state,headRefOid,baseRefName,headRefName,statusCheckRollup"


class ShipError(Exception):
    """An unexpected gh/git failure during the ship transaction."""


def gh_pr_view(branch: str, *, cwd: Path | str) -> dict:
    result = subprocess.run(
        ["gh", "pr", "view", branch, "--json", _GH_PR_VIEW_FIELDS],
        cwd=str(cwd), capture_output=True, text=True, timeout=GH_TIMEOUT,
    )
    if result.returncode != 0:
        raise ShipError(f"gh pr view {branch} failed: {result.stderr.strip()[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ShipError(f"gh pr view returned non-JSON: {e}")


def gh_pr_merge_squash(branch: str, *, match_head: str, cwd: Path | str) -> None:
    result = subprocess.run(
        ["gh", "pr", "merge", branch, "--squash", "--match-head-commit", match_head],
        cwd=str(cwd), capture_output=True, text=True, timeout=GH_TIMEOUT,
    )
    if result.returncode != 0:
        raise ShipError(f"gh pr merge {branch} failed: {result.stderr.strip()[:200]}")


def git_tree_clean(cwd: Path | str) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(cwd), capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def ci_is_green(checks: list) -> bool:
    """True if every status-rollup entry is a success.

    An empty list means no CI checks are configured for the repo — treated as
    green so approve does not deadlock on repos without required checks.
    Handles both CheckRun ({status, conclusion}) and StatusContext ({state}).
    """
    if not checks:
        return True
    for c in checks:
        state = (c.get("state") or "").upper()
        if state:  # StatusContext
            if state != "SUCCESS":
                return False
            continue
        status = (c.get("status") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        if status != "COMPLETED":
            return False
        if conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            return False
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): gh/git wrappers and CI-green parser"
```

---

## Task 7: `bump_in_pr` — bump version on the work branch and push

**Files:**
- Modify: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Consumes: `make_default_bump_fn(project_dir)` from `hermes_pipeline/merge.py` (its returned `bump_fn` ignores its `rec` argument — call it with `None`).
- Produces: `bump_in_pr(*, project_dir: Path | str, work_branch: str, todo_id: int) -> tuple[str, str]` returning `(new_version, new_head_sha)`. It checks out `work_branch`, writes `VERSION`, rewrites `pyproject.toml`'s `version = "..."`, prepends a `CHANGELOG.md` entry, commits, pushes to `origin <work_branch>`, and returns the bumped version and the new HEAD sha.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
from hermes_pipeline.ship import bump_in_pr


def test_bump_in_pr_writes_files_and_pushes(mocker, tmp_path):
    (tmp_path / "VERSION").write_text("0.3.3\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hermes-pipeline"\nversion = "0.3.3"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        stdout = "newsha999\n" if cmd[:2] == ["git", "rev-parse"] else ""
        return mocker.Mock(returncode=0, stdout=stdout, stderr="")

    mocker.patch("hermes_pipeline.ship.subprocess.run", side_effect=fake_run)

    new_version, new_sha = bump_in_pr(
        project_dir=tmp_path, work_branch="todo-5-feat", todo_id=5)

    assert new_version == "0.3.4"
    assert new_sha == "newsha999"
    assert (tmp_path / "VERSION").read_text() == "0.3.4\n"
    assert 'version = "0.3.4"' in (tmp_path / "pyproject.toml").read_text()
    assert "0.3.4" in (tmp_path / "CHANGELOG.md").read_text()
    assert "TODO-5" in (tmp_path / "CHANGELOG.md").read_text()

    flat = [" ".join(c) for c in calls]
    assert any(c.startswith("git checkout todo-5-feat") for c in flat)
    assert any(c.startswith("git commit") for c in flat)
    assert any(c.startswith("git push origin todo-5-feat") for c in flat)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_bump_in_pr_writes_files_and_pushes -v`
Expected: FAIL — `ImportError: cannot import name 'bump_in_pr'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/ship.py` (add `import re` and `from datetime import datetime, timezone` to the imports):

```python
def _run_git(args: list[str], *, cwd: Path | str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    if result.returncode != 0:
        raise ShipError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def bump_in_pr(*, project_dir: Path | str, work_branch: str, todo_id: int) -> tuple[str, str]:
    """Bump VERSION/pyproject/CHANGELOG on work_branch, commit, push.

    Returns (new_version, new_head_sha). The pushed commit becomes part of the
    squash merge, so the caller MUST re-baseline the sidecar's pr_head_sha to
    the returned sha.
    """
    from .merge import make_default_bump_fn

    project_dir = Path(project_dir)
    new_version, _label = make_default_bump_fn(project_dir)(None)

    _run_git(["checkout", work_branch], cwd=project_dir)

    (project_dir / "VERSION").write_text(f"{new_version}\n")

    pyproject = project_dir / "pyproject.toml"
    text = pyproject.read_text()
    new_text = re.sub(
        r'(?m)^version = "[^"]*"',
        f'version = "{new_version}"',
        text,
        count=1,
    )
    pyproject.write_text(new_text)

    changelog = project_dir / "CHANGELOG.md"
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry = (
        f"\n## [{new_version}] - {timestamp}\n"
        f"- Ship TODO-{todo_id}: bump to {new_version}\n"
    )
    if changelog.exists():
        changelog.write_text(entry + "\n" + changelog.read_text())
    else:
        changelog.write_text(f"# Changelog\n{entry}")

    _run_git(["add", "VERSION", "pyproject.toml", "CHANGELOG.md"], cwd=project_dir)
    _run_git(
        ["commit", "-m", f"chore: bump to {new_version} for TODO-{todo_id}"],
        cwd=project_dir,
    )
    _run_git(["push", "origin", work_branch], cwd=project_dir)

    new_sha = _run_git(["rev-parse", "HEAD"], cwd=project_dir)
    return new_version, new_sha
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): bump_in_pr writes bump commit to work branch"
```

---

## Task 8: `resolve_ship_task` — find the gate task id for a tick

**Files:**
- Modify: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Consumes: `get_todo_kanban_tasks(tenant, tick_id)` from Task 4.
- Produces: `GATE_PHASE_KEY = "phase_9_ship"`; `resolve_ship_task(*, project_slug: str, tick_id: str) -> KanbanTaskInfo | None` — returns the gate `KanbanTaskInfo`, or `None` if absent.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
from hermes_pipeline.ship import resolve_ship_task, GATE_PHASE_KEY
from hermes_pipeline.kanban_tasks import KanbanTaskInfo


def test_resolve_ship_task_returns_gate(mocker):
    tasks = {
        "phase_8_finish_branch": KanbanTaskInfo("t_8", "phase_8_finish_branch", "done", "TODO-5"),
        GATE_PHASE_KEY: KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"),
    }
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=tasks)
    got = resolve_ship_task(project_slug="demo", tick_id="01TICK")
    assert got is not None
    assert got.task_id == "t_9"


def test_resolve_ship_task_none_when_absent(mocker):
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value={})
    assert resolve_ship_task(project_slug="demo", tick_id="01TICK") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_resolve_ship_task_returns_gate -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_ship_task'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/ship.py` (add the import at the top of the module: `from .kanban_tasks import get_todo_kanban_tasks, KanbanTaskInfo`):

```python
GATE_PHASE_KEY = "phase_9_ship"


def resolve_ship_task(*, project_slug: str, tick_id: str) -> KanbanTaskInfo | None:
    tasks = get_todo_kanban_tasks(project_slug, tick_id)
    return tasks.get(GATE_PHASE_KEY)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): resolve_ship_task finds the gate task"
```

---

## Task 9: `approve_lock` — fcntl serialization for approve

**Files:**
- Modify: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Produces:
  - `class ApproveRefused(Exception)` — a guard refusal (maps to a distinct non-error exit).
  - `approve_lock(state_dir: Path | str)` — a context manager taking a non-blocking exclusive `fcntl.flock` on `<state_dir>/approve.lock`; raises `ApproveRefused` if another approve holds it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
from hermes_pipeline.ship import approve_lock, ApproveRefused


def test_approve_lock_excludes_second_holder(tmp_path):
    with approve_lock(tmp_path):
        with pytest.raises(ApproveRefused):
            with approve_lock(tmp_path):
                pass


def test_approve_lock_reacquirable_after_release(tmp_path):
    with approve_lock(tmp_path):
        pass
    # Should not raise the second time.
    with approve_lock(tmp_path):
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_approve_lock_excludes_second_holder -v`
Expected: FAIL — `ImportError: cannot import name 'approve_lock'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/ship.py` (add `import fcntl` and `from contextlib import contextmanager` to the imports):

```python
class ApproveRefused(Exception):
    """A deterministic guard refused the approve. Not an internal error."""


@contextmanager
def approve_lock(state_dir: Path | str):
    """Serialize approve via a non-blocking exclusive flock.

    Uses a dedicated <state_dir>/approve.lock — NOT TickLock, whose
    stale-reclamation is wrong for a human-driven, possibly-slow merge.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "approve.lock"
    f = open(lock_path, "w")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ApproveRefused("another approve is already in progress")
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): approve_lock serializes approve via fcntl"
```

---

## Task 10: `_check_ship_guards` — clean-tree + SHA-staleness (force-aware, audited)

**Files:**
- Modify: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Consumes: `git_tree_clean`, `ShipSidecar`, `ApproveRefused` (this module).
- Produces:
  - `_audit(state_dir: Path | str, message: str) -> None` — appends a UTC-timestamped line to `<state_dir>/approve_audit.log` and logs at WARNING.
  - `_check_ship_guards(*, sidecar: ShipSidecar, live_head_sha: str, project_dir: Path | str, state_dir: Path | str, force_count: int) -> None` — raises `ApproveRefused` on a dirty tree (never bypassable) or a stale SHA (bypassable only with `force_count >= 2`, which is audited). The SHA check is skipped entirely once `sidecar.bump_version` is set (the bump already re-baselined the SHA).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
from hermes_pipeline.ship import _check_ship_guards


def _guard_sidecar(**kw):
    base = dict(
        tick_id="01TICK", todo_id=5, pr_number=42, pr_head_sha="reviewed_sha",
        base_branch="main", work_branch="todo-5-feat",
        phase_8_task_id="t_8", bump_version=None,
    )
    base.update(kw)
    return ShipSidecar(**base)


def test_guards_refuse_dirty_tree(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=False)
    with pytest.raises(ApproveRefused, match="dirty"):
        _check_ship_guards(
            sidecar=_guard_sidecar(), live_head_sha="reviewed_sha",
            project_dir=tmp_path, state_dir=tmp_path, force_count=0)


def test_guards_dirty_tree_not_force_bypassable(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=False)
    with pytest.raises(ApproveRefused, match="dirty"):
        _check_ship_guards(
            sidecar=_guard_sidecar(), live_head_sha="reviewed_sha",
            project_dir=tmp_path, state_dir=tmp_path, force_count=2)


def test_guards_refuse_stale_sha(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    with pytest.raises(ApproveRefused, match="SHA"):
        _check_ship_guards(
            sidecar=_guard_sidecar(), live_head_sha="DIFFERENT",
            project_dir=tmp_path, state_dir=tmp_path, force_count=0)


def test_guards_stale_sha_bypassed_by_double_force_and_audited(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    _check_ship_guards(
        sidecar=_guard_sidecar(), live_head_sha="DIFFERENT",
        project_dir=tmp_path, state_dir=tmp_path, force_count=2)
    audit = (tmp_path / "approve_audit.log").read_text()
    assert "force" in audit.lower()
    assert "DIFFERENT" in audit


def test_guards_single_force_does_not_bypass_sha(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    with pytest.raises(ApproveRefused, match="SHA"):
        _check_ship_guards(
            sidecar=_guard_sidecar(), live_head_sha="DIFFERENT",
            project_dir=tmp_path, state_dir=tmp_path, force_count=1)


def test_guards_skip_sha_check_after_bump(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    # bump_version set => SHA already re-baselined; live mismatch is fine here
    # because the live sha equals the bumped sidecar sha in practice. Pass a
    # mismatch to prove the check is skipped, not merely satisfied.
    _check_ship_guards(
        sidecar=_guard_sidecar(bump_version="0.3.4", pr_head_sha="bumped"),
        live_head_sha="something_else",
        project_dir=tmp_path, state_dir=tmp_path, force_count=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_guards_refuse_dirty_tree -v`
Expected: FAIL — `ImportError: cannot import name '_check_ship_guards'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/ship.py`:

```python
def _audit(state_dir: Path | str, message: str) -> None:
    """Append a timestamped audit line; also log at WARNING."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    line = f"{ts} {message}\n"
    with open(state_dir / "approve_audit.log", "a") as f:
        f.write(line)
    log.warning("AUDIT: %s", message)


def _check_ship_guards(
    *,
    sidecar: ShipSidecar,
    live_head_sha: str,
    project_dir: Path | str,
    state_dir: Path | str,
    force_count: int,
) -> None:
    """Run the deterministic pre-merge guards.

    - Dirty tree: ALWAYS refuses (force can never bypass).
    - SHA staleness: refuses unless force_count >= 2 (audited). Skipped once
      the bump has re-baselined the SHA (sidecar.bump_version set).
    """
    if not git_tree_clean(project_dir):
        raise ApproveRefused(
            f"working tree is dirty in {project_dir}; commit or clean before approving"
        )

    if sidecar.bump_version is not None:
        return  # SHA already re-baselined by the bump commit.

    if live_head_sha != sidecar.pr_head_sha:
        if force_count >= 2:
            _audit(
                state_dir,
                f"force-bypass SHA guard for TODO-{sidecar.todo_id}: "
                f"reviewed={sidecar.pr_head_sha} live={live_head_sha}",
            )
            return
        raise ApproveRefused(
            f"PR head SHA changed since review "
            f"(reviewed={sidecar.pr_head_sha}, live={live_head_sha}); "
            f"re-review, or pass --force --force to override"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): pre-merge guard set (clean tree, SHA staleness, force audit)"
```

---

## Task 11: `_bump_and_merge` — bump (if needed), CI gate, match-head merge

**Files:**
- Modify: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Consumes: `bump_in_pr`, `write_sidecar`, `gh_pr_view`, `ci_is_green`, `gh_pr_merge_squash` (this module).
- Produces: `_bump_and_merge(*, sidecar: ShipSidecar, project_dir: Path | str, state_dir: Path | str) -> None`. If `sidecar.bump_version` is `None`, it bumps via `bump_in_pr`, sets `sidecar.bump_version`/`sidecar.pr_head_sha` to the bumped values, and re-writes the sidecar. It then re-reads the PR; if CI is not green it raises `ApproveRefused` ("retry when checks pass" — no hang). When green it squash-merges with `--match-head-commit sidecar.pr_head_sha`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
from hermes_pipeline.ship import _bump_and_merge


def _merge_sidecar(**kw):
    base = dict(
        tick_id="01TICK", todo_id=5, pr_number=42, pr_head_sha="reviewed_sha",
        base_branch="main", work_branch="todo-5-feat",
        phase_8_task_id="t_8", bump_version=None,
    )
    base.update(kw)
    return ShipSidecar(**base)


def test_bump_then_ci_pending_refuses_with_retry(mocker, tmp_path):
    mocker.patch("hermes_pipeline.ship.bump_in_pr", return_value=("0.3.4", "bumpedsha"))
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "state": "OPEN", "headRefOid": "bumpedsha",
        "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}],
    })
    merge = mocker.patch("hermes_pipeline.ship.gh_pr_merge_squash")
    sc = _merge_sidecar()
    with pytest.raises(ApproveRefused, match="CI"):
        _bump_and_merge(sidecar=sc, project_dir=tmp_path, state_dir=tmp_path)
    merge.assert_not_called()
    # Sidecar must have been re-baselined so a retry skips the bump.
    persisted = read_sidecar(tmp_path, "01TICK")
    assert persisted.bump_version == "0.3.4"
    assert persisted.pr_head_sha == "bumpedsha"


def test_retry_skips_bump_and_merges_when_green(mocker, tmp_path):
    bump = mocker.patch("hermes_pipeline.ship.bump_in_pr")
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "state": "OPEN", "headRefOid": "bumpedsha",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    })
    merge = mocker.patch("hermes_pipeline.ship.gh_pr_merge_squash")
    sc = _merge_sidecar(bump_version="0.3.4", pr_head_sha="bumpedsha")
    _bump_and_merge(sidecar=sc, project_dir=tmp_path, state_dir=tmp_path)
    bump.assert_not_called()
    merge.assert_called_once()
    _, kwargs = merge.call_args
    assert kwargs["match_head"] == "bumpedsha"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_retry_skips_bump_and_merges_when_green -v`
Expected: FAIL — `ImportError: cannot import name '_bump_and_merge'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/ship.py`:

```python
def _bump_and_merge(
    *,
    sidecar: ShipSidecar,
    project_dir: Path | str,
    state_dir: Path | str,
) -> None:
    """Bump-in-PR (once), gate on CI, then squash-merge at the exact SHA."""
    if sidecar.bump_version is None:
        new_version, new_sha = bump_in_pr(
            project_dir=project_dir,
            work_branch=sidecar.work_branch,
            todo_id=sidecar.todo_id,
        )
        # Re-baseline: the bump commit invalidated the reviewed SHA. Persist so
        # a retry (after CI passes) skips the bump and merges this exact SHA.
        sidecar.bump_version = new_version
        sidecar.pr_head_sha = new_sha
        write_sidecar(sidecar, state_dir=state_dir)

    view = gh_pr_view(sidecar.work_branch, cwd=project_dir)
    checks = view.get("statusCheckRollup") or []
    if not checks:
        log.warning(
            "no CI checks found for %s; proceeding (nothing to gate on)",
            sidecar.work_branch,
        )
    if not ci_is_green(checks):
        raise ApproveRefused(
            "CI is not green yet; re-run approve once checks pass "
            "(the bump commit is already pushed)"
        )

    gh_pr_merge_squash(
        sidecar.work_branch, match_head=sidecar.pr_head_sha, cwd=project_dir
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): bump-once + CI gate + match-head squash merge"
```

---

## Task 12: `approve_ship` — orchestrator (lock, idempotency, guards, complete gate)

**Files:**
- Modify: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Consumes: `approve_lock`, `find_ship_sidecar`, `resolve_ship_task`, `gh_pr_view`, `_check_ship_guards`, `_bump_and_merge`, `delete_sidecar` (this module).
- Produces:
  - `complete_gate_task(task_id: str) -> None` — `hermes kanban complete <task_id>`; raises `ShipError` on failure.
  - `approve_ship(*, project_dir: Path | str, project_slug: str, todo_id: int, state_dir: Path | str, force_count: int = 0) -> str` — returns a human-readable success summary, or raises `ApproveRefused`/`ShipError`. Order: acquire lock → find sidecar (else refuse) → resolve gate task (else refuse) → `gh pr view`; if PR `state == "MERGED"`, just complete the gate, delete the sidecar, return (idempotent) → run guards → `_bump_and_merge` → complete gate → delete sidecar.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
from hermes_pipeline.ship import approve_ship, write_sidecar


def _seed_sidecar(tmp_path, **kw):
    base = dict(
        tick_id="01TICK", todo_id=5, pr_number=42, pr_head_sha="reviewed_sha",
        base_branch="main", work_branch="todo-5-feat",
        phase_8_task_id="t_8", bump_version=None,
    )
    base.update(kw)
    write_sidecar(ShipSidecar(**base), state_dir=tmp_path)


def test_approve_refuses_without_sidecar(tmp_path, mocker):
    mocker.patch("hermes_pipeline.ship.resolve_ship_task")
    with pytest.raises(ApproveRefused, match="no pending ship"):
        approve_ship(project_dir=tmp_path, project_slug="demo",
                     todo_id=5, state_dir=tmp_path)


def test_approve_idempotent_when_already_merged(tmp_path, mocker):
    _seed_sidecar(tmp_path)
    mocker.patch("hermes_pipeline.ship.resolve_ship_task",
                 return_value=KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"))
    mocker.patch("hermes_pipeline.ship.gh_pr_view",
                 return_value={"state": "MERGED", "headRefOid": "x", "statusCheckRollup": []})
    complete = mocker.patch("hermes_pipeline.ship.complete_gate_task")
    bump = mocker.patch("hermes_pipeline.ship._bump_and_merge")
    summary = approve_ship(project_dir=tmp_path, project_slug="demo",
                           todo_id=5, state_dir=tmp_path)
    bump.assert_not_called()
    complete.assert_called_once_with("t_9")
    assert find_ship_sidecar(tmp_path, 5) is None
    assert "already" in summary.lower()


def test_approve_happy_path_merges_and_completes(tmp_path, mocker):
    _seed_sidecar(tmp_path)
    mocker.patch("hermes_pipeline.ship.resolve_ship_task",
                 return_value=KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"))
    mocker.patch("hermes_pipeline.ship.gh_pr_view",
                 return_value={"state": "OPEN", "headRefOid": "reviewed_sha",
                               "statusCheckRollup": []})
    mocker.patch("hermes_pipeline.ship.git_tree_clean", return_value=True)
    bump = mocker.patch("hermes_pipeline.ship._bump_and_merge")
    complete = mocker.patch("hermes_pipeline.ship.complete_gate_task")
    summary = approve_ship(project_dir=tmp_path, project_slug="demo",
                           todo_id=5, state_dir=tmp_path)
    bump.assert_called_once()
    complete.assert_called_once_with("t_9")
    assert find_ship_sidecar(tmp_path, 5) is None
    assert "TODO-5" in summary


def test_approve_refuses_when_no_gate_task(tmp_path, mocker):
    _seed_sidecar(tmp_path)
    mocker.patch("hermes_pipeline.ship.resolve_ship_task", return_value=None)
    with pytest.raises(ApproveRefused, match="gate task"):
        approve_ship(project_dir=tmp_path, project_slug="demo",
                     todo_id=5, state_dir=tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_approve_happy_path_merges_and_completes -v`
Expected: FAIL — `ImportError: cannot import name 'approve_ship'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/ship.py`:

```python
def complete_gate_task(task_id: str) -> None:
    result = subprocess.run(
        ["hermes", "kanban", "complete", task_id],
        capture_output=True, text=True, timeout=GH_TIMEOUT,
    )
    if result.returncode != 0:
        raise ShipError(
            f"hermes kanban complete {task_id} failed: {result.stderr.strip()[:200]}"
        )


def approve_ship(
    *,
    project_dir: Path | str,
    project_slug: str,
    todo_id: int,
    state_dir: Path | str,
    force_count: int = 0,
) -> str:
    """Deterministically ship an approved TODO. Returns a success summary.

    Raises ApproveRefused on any guard refusal, ShipError on subprocess failure.
    """
    with approve_lock(state_dir):
        sidecar = find_ship_sidecar(state_dir, todo_id)
        if sidecar is None:
            raise ApproveRefused(
                f"no pending ship for TODO-{todo_id} "
                f"(not ready, or already shipped)"
            )

        gate = resolve_ship_task(project_slug=project_slug, tick_id=sidecar.tick_id)
        if gate is None:
            raise ApproveRefused(
                f"no gate task found for tick {sidecar.tick_id}; cannot ship"
            )

        view = gh_pr_view(sidecar.work_branch, cwd=project_dir)

        # Idempotency: if the PR is already merged (e.g. a crash after merge
        # but before completing the gate), just finish the gate and clean up.
        if (view.get("state") or "").upper() == "MERGED":
            complete_gate_task(gate.task_id)
            delete_sidecar(state_dir, sidecar.tick_id)
            return f"TODO-{todo_id} PR already merged; gate completed."

        _check_ship_guards(
            sidecar=sidecar,
            live_head_sha=view.get("headRefOid", ""),
            project_dir=project_dir,
            state_dir=state_dir,
            force_count=force_count,
        )

        _bump_and_merge(sidecar=sidecar, project_dir=project_dir, state_dir=state_dir)

        complete_gate_task(gate.task_id)
        delete_sidecar(state_dir, sidecar.tick_id)
        return (
            f"Shipped TODO-{todo_id}: merged {sidecar.work_branch} to "
            f"{sidecar.base_branch} (v{sidecar.bump_version}); gate completed."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): approve_ship orchestrator with idempotent merge + gate completion"
```

---

## Task 13: `maybe_ship_ready` — detect, write sidecar, alert once

**Files:**
- Modify: `hermes_pipeline/ship.py`
- Test: `tests/test_ship.py`

**Interfaces:**
- Consumes: `get_todo_kanban_tasks`, `COMPLETION_STATUSES` (from `kanban_tasks`); `read_sidecar`, `write_sidecar`, `gh_pr_view`, `BLOCKED` (from `kanban_tasks`); `slack.notify`.
- Produces: `maybe_ship_ready(*, project_dir: Path | str, project_slug: str, prior_tick_id: str, state_dir: Path | str, slack_channel: str) -> None`. Best-effort, never raises. It returns immediately if the sidecar already exists (dedup), if there is no gate task, if the gate is not `blocked`, or if any non-gate phase is not in `COMPLETION_STATUSES`. Otherwise it reads the work branch from `<state_dir>/pipeline_branch.txt`, queries `gh pr view`, writes the sidecar, then fires a one-time Slack alert.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
from hermes_pipeline.ship import maybe_ship_ready


def _ready_tasks():
    return {
        "phase_8_finish_branch": KanbanTaskInfo("t_8", "phase_8_finish_branch", "done", "TODO-5"),
        GATE_PHASE_KEY: KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"),
    }


def test_maybe_ship_ready_writes_sidecar_and_alerts(tmp_path, mocker):
    (tmp_path / "pipeline_branch.txt").write_text("todo-5-feat\n")
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=_ready_tasks())
    mocker.patch("hermes_pipeline.ship.gh_pr_view", return_value={
        "number": 42, "headRefOid": "reviewed_sha", "baseRefName": "main",
        "state": "OPEN", "statusCheckRollup": [],
    })
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")

    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path,
                     slack_channel="#ship")

    sc = read_sidecar(tmp_path, "01TICK")
    assert sc is not None
    assert sc.todo_id == 5
    assert sc.pr_number == 42
    assert sc.pr_head_sha == "reviewed_sha"
    assert sc.work_branch == "todo-5-feat"
    notify.assert_called_once()
    assert "#ship" == notify.call_args[0][0]


def test_maybe_ship_ready_dedups_on_existing_sidecar(tmp_path, mocker):
    write_sidecar(ShipSidecar(
        tick_id="01TICK", todo_id=5, pr_number=42, pr_head_sha="x",
        base_branch="main", work_branch="b"), state_dir=tmp_path)
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=_ready_tasks())
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")
    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path, slack_channel="#ship")
    notify.assert_not_called()


def test_maybe_ship_ready_noop_when_phase_unfinished(tmp_path, mocker):
    tasks = {
        "phase_8_finish_branch": KanbanTaskInfo("t_8", "phase_8_finish_branch", "running", "TODO-5"),
        GATE_PHASE_KEY: KanbanTaskInfo("t_9", GATE_PHASE_KEY, "blocked", "TODO-5"),
    }
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=tasks)
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")
    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path, slack_channel="#ship")
    assert read_sidecar(tmp_path, "01TICK") is None
    notify.assert_not_called()


def test_maybe_ship_ready_noop_when_no_gate(tmp_path, mocker):
    tasks = {
        "phase_8_finish_branch": KanbanTaskInfo("t_8", "phase_8_finish_branch", "done", "TODO-5"),
    }
    mocker.patch("hermes_pipeline.ship.get_todo_kanban_tasks", return_value=tasks)
    notify = mocker.patch("hermes_pipeline.ship.slack.notify")
    maybe_ship_ready(project_dir=tmp_path, project_slug="demo",
                     prior_tick_id="01TICK", state_dir=tmp_path, slack_channel="#ship")
    assert read_sidecar(tmp_path, "01TICK") is None
    notify.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_maybe_ship_ready_writes_sidecar_and_alerts -v`
Expected: FAIL — `ImportError: cannot import name 'maybe_ship_ready'`.

- [ ] **Step 3: Write minimal implementation**

Add to `hermes_pipeline/ship.py` (add `from . import slack` and `from .kanban_tasks import COMPLETION_STATUSES, BLOCKED` to the imports):

```python
def maybe_ship_ready(
    *,
    project_dir: Path | str,
    project_slug: str,
    prior_tick_id: str,
    state_dir: Path | str,
    slack_channel: str,
) -> None:
    """Detect a ship-ready TODO, record a sidecar, and alert once.

    Best-effort: any failure is logged and swallowed so the tick continues.
    MUST be called before _tick_project's all_phases_complete early-return,
    because a blocked gate makes all_phases_complete return False.
    """
    try:
        if read_sidecar(state_dir, prior_tick_id) is not None:
            return  # already detected + alerted for this tick

        tasks = get_todo_kanban_tasks(project_slug, prior_tick_id)
        gate = tasks.get(GATE_PHASE_KEY)
        if gate is None or gate.status != BLOCKED:
            return  # no gate, or gate already moved past blocked

        non_gate = [t for k, t in tasks.items() if k != GATE_PHASE_KEY]
        if not non_gate or any(t.status not in COMPLETION_STATUSES for t in non_gate):
            return  # real work still in flight

        branch_file = Path(state_dir) / "pipeline_branch.txt"
        if not branch_file.exists():
            log.warning("ship-ready but no pipeline_branch.txt at %s", branch_file)
            return
        work_branch = branch_file.read_text().strip()
        if not work_branch:
            return

        view = gh_pr_view(work_branch, cwd=project_dir)
        todo_num = int(gate.todo_id.removeprefix("TODO-"))
        sidecar = ShipSidecar(
            tick_id=prior_tick_id,
            todo_id=todo_num,
            pr_number=int(view.get("number", 0)),
            pr_head_sha=view.get("headRefOid", ""),
            base_branch=view.get("baseRefName", "main"),
            work_branch=work_branch,
            phase_8_task_id=(
                tasks["phase_8_finish_branch"].task_id
                if "phase_8_finish_branch" in tasks else None
            ),
            bump_version=None,
        )
        write_sidecar(sidecar, state_dir=state_dir)

        slack.notify(
            slack_channel,
            f":rocket: {project_slug} TODO-{todo_num} is ready to ship — "
            f"PR #{sidecar.pr_number} passed all phases. "
            f"Run: pipeline-watch approve {project_slug} --todo TODO-{todo_num}",
        )
    except Exception as e:  # never break the tick
        log.warning("maybe_ship_ready failed for %s tick %s: %s",
                    project_slug, prior_tick_id, e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/ship.py tests/test_ship.py
git commit -m "feat(ship): maybe_ship_ready detection + one-time slack alert"
```

---

## Task 14: Wire `maybe_ship_ready` into `_tick_project`

**Files:**
- Modify: `hermes_pipeline/cli.py:851-875` (the `prior_tick_id` branch of `_tick_project`)
- Test: `tests/test_ship.py` (a focused integration test of the branch ordering)

**Interfaces:**
- Consumes: `ship.maybe_ship_ready` from Task 13.
- Produces: `_tick_project` calls `maybe_ship_ready(...)` after the circuit breaker is built and **before** the `all_phases_complete` early-return, so the sidecar/alert path runs even while the gate keeps the project in-flight.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ship.py`:

```python
def test_tick_project_calls_maybe_ship_ready_before_early_return(mocker, tmp_path):
    """maybe_ship_ready must run even when all_phases_complete is False."""
    import hermes_pipeline.cli as cli

    # Force the in-flight early-return path.
    mocker.patch.object(cli, "_read_prior_tick_id", return_value="01TICK")
    mocker.patch.object(cli, "all_phases_complete", return_value=False)
    mocker.patch.object(cli, "_make_circuit_breaker", return_value=mocker.Mock())
    mocker.patch("hermes_pipeline.project_config._resolve_slack_channel",
                 return_value="#ship")
    called = mocker.patch("hermes_pipeline.ship.maybe_ship_ready")

    # all_phases_complete is False, so _tick_project returns cleanly at the
    # early-return — but maybe_ship_ready must have already fired before it.
    cli._tick_project(
        project_dir=tmp_path,
        project_slug="demo",
        project_state=tmp_path,
        tick_id="02NEXT",
        config=mocker.Mock(slack_channel=None),
        cb_cfg=mocker.Mock(),
        project_toml={},
    )

    called.assert_called_once()
    kwargs = called.call_args.kwargs
    assert kwargs["prior_tick_id"] == "01TICK"
    assert kwargs["project_slug"] == "demo"
```

> Note: `_tick_project` is keyword-only with exactly this signature — `(*, project_dir, project_slug, project_state, config, cb_cfg, tick_id, project_toml=None)` (confirmed at `hermes_pipeline/cli.py:810`). `_resolve_slack_channel` is imported locally inside the function from `project_config`, so it is patched at `hermes_pipeline.project_config._resolve_slack_channel`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ship.py::test_tick_project_calls_maybe_ship_ready_before_early_return -v`
Expected: FAIL — `maybe_ship_ready` not called (it isn't wired in yet).

- [ ] **Step 3: Write minimal implementation**

In `hermes_pipeline/cli.py`, inside `_tick_project`, change the `prior_tick_id` block so it calls `maybe_ship_ready` immediately after `cb` is built and before the `all_phases_complete` check:

```python
    cb = _make_circuit_breaker(project_state, cb_cfg, slack_channel)

    if prior_tick_id is not None:
        # Ship-gate: a blocked phase_9_ship makes all_phases_complete return
        # False, so detect/alert "ready to ship" BEFORE the early-return below.
        from . import ship
        ship.maybe_ship_ready(
            project_dir=project_dir,
            project_slug=project_slug,
            prior_tick_id=prior_tick_id,
            state_dir=project_state,
            slack_channel=slack_channel,
        )

        if not all_phases_complete(project_slug, prior_tick_id, state_dir=project_state):
            log.info("project %s: prior tick %s still in-flight, skipping",
                     project_slug, prior_tick_id)
            return

        # Prior tick complete — observe outcomes before new selection
        try:
            from .kanban_tasks import get_todo_kanban_status
            status_map = get_todo_kanban_status(project_slug, prior_tick_id)
            observe_outcomes(
                state_dir=project_state,
                tick_id=prior_tick_id,
                status_map=status_map,
            )
            cb.observe_from_outcomes(
                state_dir=project_state,
                prior_tick_id=prior_tick_id,
            )
        except Exception as e:
            log.warning("project %s: observe_outcomes for prior tick %s failed: %s",
                        project_slug, prior_tick_id, e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ship.py -v && uv run pytest tests/ -k tick -v`
Expected: PASS (no existing tick test regressed; `maybe_ship_ready` is a no-op when there is no gate).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_ship.py
git commit -m "feat(cli): fire maybe_ship_ready before all_phases_complete early-return"
```

---

## Task 15: `approve` subcommand (`_cmd_approve` + parser)

**Files:**
- Modify: `hermes_pipeline/cli.py` (add `_cmd_approve` near `_cmd_merge`; add the `approve` subparser in `build_parser`)
- Test: `tests/test_approve_cli.py`

**Interfaces:**
- Consumes: `ship.approve_ship`, `ship.ApproveRefused` (Task 12); `_resolve_project_dir`, `_parse_todo_id` (existing in `cli.py`); `_get_project_state_dir` (existing in `state_migration`).
- Produces: `_cmd_approve(args, config) -> int` (exit 0 success, 3 refused, 2 error); an `approve` subparser with a positional `project`, a required `--todo` (`type=_parse_todo_id`), and `--force` (`action="count", default=0`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_approve_cli.py`:

```python
import pytest

import hermes_pipeline.cli as cli
from hermes_pipeline.ship import ApproveRefused, ShipError


def _config(tmp_path):
    return cli.Config(
        lock_dir=tmp_path / "locks",
        projects_dir=tmp_path / "projects",
        state_dir=tmp_path / "state",
        kanban_adapter="null",
    )


def test_approve_parser_requires_todo_and_counts_force():
    parser = cli.build_parser()
    args = parser.parse_args(["approve", "demo", "--todo", "TODO-5", "--force", "--force"])
    assert args.project == "demo"
    assert args.todo == 5
    assert args.force == 2
    assert args.func is cli._cmd_approve


def test_cmd_approve_success_returns_zero(mocker, tmp_path):
    cfg = _config(tmp_path)
    pdir = tmp_path / "projects" / "demo"
    pdir.mkdir(parents=True)
    mocker.patch.object(cli, "_resolve_project_dir", return_value=pdir)
    mocker.patch("hermes_pipeline.state_migration._get_project_state_dir",
                 return_value=tmp_path / "state")
    approve = mocker.patch("hermes_pipeline.ship.approve_ship", return_value="Shipped TODO-5")
    args = cli.build_parser().parse_args(["approve", "demo", "--todo", "TODO-5"])
    assert cli._cmd_approve(args, cfg) == 0
    _, kwargs = approve.call_args
    assert kwargs["todo_id"] == 5
    assert kwargs["force_count"] == 0


def test_cmd_approve_refused_returns_three(mocker, tmp_path):
    cfg = _config(tmp_path)
    pdir = tmp_path / "projects" / "demo"
    pdir.mkdir(parents=True)
    mocker.patch.object(cli, "_resolve_project_dir", return_value=pdir)
    mocker.patch("hermes_pipeline.state_migration._get_project_state_dir",
                 return_value=tmp_path / "state")
    mocker.patch("hermes_pipeline.ship.approve_ship",
                 side_effect=ApproveRefused("CI not green"))
    args = cli.build_parser().parse_args(["approve", "demo", "--todo", "TODO-5"])
    assert cli._cmd_approve(args, cfg) == 3


def test_cmd_approve_unknown_project_returns_two(mocker, tmp_path):
    cfg = _config(tmp_path)
    mocker.patch.object(cli, "_resolve_project_dir", return_value=None)
    args = cli.build_parser().parse_args(["approve", "nope", "--todo", "TODO-5"])
    assert cli._cmd_approve(args, cfg) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_approve_cli.py::test_approve_parser_requires_todo_and_counts_force -v`
Expected: FAIL — `argparse` error: invalid choice `'approve'` (subparser not registered).

- [ ] **Step 3: Write minimal implementation**

In `hermes_pipeline/cli.py`, add `_cmd_approve` (next to `_cmd_merge`):

```python
def _cmd_approve(args, config: Config) -> int:
    """Handle 'approve' subcommand: deterministically ship a ready TODO.

    Exit codes: 0 shipped, 3 refused by a guard, 2 unexpected error.
    """
    from . import ship
    from .state_migration import _get_project_state_dir

    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    state_dir = _get_project_state_dir(project_dir)
    try:
        summary = ship.approve_ship(
            project_dir=project_dir,
            project_slug=args.project,
            todo_id=args.todo,
            state_dir=state_dir,
            force_count=args.force,
        )
        print(summary)
        return 0
    except ship.ApproveRefused as e:
        print(f"approve refused: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        log.error("approve command failed: %s", e, exc_info=True)
        return 2
```

Then in `build_parser`, after the `merge` subparser block, add:

```python
    # approve: Phase 9 ship gate — bump-in-PR, merge, complete gate
    approve_parser = subparsers.add_parser(
        "approve",
        help="Ship a ready TODO: bump version in PR, merge to main, complete the gate",
    )
    approve_parser.add_argument("project", help="Project name")
    approve_parser.add_argument(
        "--todo", required=True, type=_parse_todo_id,
        help="TODO to ship (e.g. TODO-5)",
    )
    approve_parser.add_argument(
        "--force", action="count", default=0,
        help="Pass twice (--force --force) to bypass ONLY the SHA-staleness guard (audited)",
    )
    approve_parser.set_defaults(func=_cmd_approve)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_approve_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_approve_cli.py
git commit -m "feat(cli): add approve subcommand for the ship gate"
```

---

## Task 16: Update CLI module docstring and help copy

**Files:**
- Modify: `hermes_pipeline/cli.py:1-5` (module docstring), `hermes_pipeline/cli.py:387` (parser description)
- Test: `tests/test_approve_cli.py`

**Interfaces:**
- Produces: documentation only — the module docstring and parser description name the `approve` subcommand.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_approve_cli.py`:

```python
def test_module_docstring_mentions_approve():
    assert "approve" in (cli.__doc__ or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_approve_cli.py::test_module_docstring_mentions_approve -v`
Expected: FAIL — current docstring is "Subcommands: merge, status, kill."

- [ ] **Step 3: Edit the docstring and description**

In `hermes_pipeline/cli.py`, change the module docstring header:

```python
"""Hermes pipeline orchestrator CLI.

Subcommands: merge, approve, status, kill.
Scheduling is owned by the Hermes command repo.
"""
```

And the parser description in `build_parser`:

```python
    parser = argparse.ArgumentParser(
        prog="pipeline-watch",
        description="Hermes pipeline orchestrator: merge, approve, status, and kill commands.",
    )
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: PASS (entire suite green).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_approve_cli.py
git commit -m "docs(cli): document the approve subcommand"
```

---

## Final verification

- [ ] Run the whole suite once more: `uv run pytest tests/ -v`
- [ ] Manual smoke (optional, requires a real project + gh auth): run a tick that registers phases, confirm a `phase_9_ship` task is created `blocked`, confirm `pipeline-watch status` still works, then dry-run `pipeline-watch approve <project> --todo TODO-N` against a throwaway PR.
