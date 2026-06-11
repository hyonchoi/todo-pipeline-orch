# TODO Pipeline Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `hermes_pipeline` Python package + `todos-manager` skill that turn per-project `TODOS.md` files into an autonomous, single-user work queue. A 5-minute cron picks the next eligible TODO per project, runs Phases 2-8 of a Claude Code pipeline (autoplan → plan → dev → security → release-docs → finish-branch), mirrors the active task to a hermes kanban board, and halts at a PR ready for human review. A separate explicit `pipeline-watch merge` command performs Phase 9 (semver bump, `VERSION`/`CHANGELOG.md`, merge) only after typed e2e confirmation.

**Architecture:** Six independent lanes (A-F): a Claude Code skill (`todos-manager`) that owns stable `TODO-<n>` ID assignment via a per-project counter file; three independent core modules (`selection.py`, `kanban.py`, `state.py` extension) that share no state between them; `runner.py` and Phase 9 entrypoint that compose them via documented interfaces; and a final docs/CLI lane. Every cross-module boundary (`KanbanClient`, `ready_for_review` record schema, `TODO-<n>` ID semantics) is specified in the design doc and locked in here before the lanes diverge. The pipeline never auto-merges; Phase 8 hands off, Phase 9 only runs on explicit operator invocation.

**Tech Stack:** Python ≥ 3.14, `uv` package manager, `pyyaml` (Phase config), `pytest` + `pytest-mock` (tests), Claude Code (skill author + `claude -p` runtime), `hermes` CLI (kanban + Slack adapters), system `git`.

---

## Source Design Doc

`docs/gstack/hyonchoi-main-design-20260610-195349.md` — **APPROVED**. This plan implements that doc verbatim; deviations are not permitted without amending the doc. Section references in tasks (e.g. "see design §HermesKanbanAdapter") point back to that file.

## File Structure

The repository will gain one top-level package directory and one skill directory. Nothing in the existing repo root (`main.py`, `README.md`, `CHANGELOG.md`, `VERSION`, `TODOS.md`) is touched by this plan except via Phase 9 at runtime; Phase 9 itself is implemented inside the package, not the repo root.

```
todo-pipeline-orchestrator/
├── pyproject.toml                          # MODIFY: add deps + entrypoint
├── hermes-pipeline/
│   ├── pyproject.toml                      # CREATE: package metadata
│   ├── README.md                           # CREATE: install + run notes
│   ├── src/hermes_pipeline/
│   │   ├── __init__.py                     # CREATE: __version__
│   │   ├── config.py                       # CREATE: Config + env overrides
│   │   ├── state.py                        # CREATE: State + ReadyForReview record
│   │   ├── phases.py                       # CREATE: Phase dataclass + load_phases
│   │   ├── selection.py                    # CREATE: dep graph + cycle + sort
│   │   ├── kanban.py                       # CREATE: KanbanClient + adapters + outbox
│   │   ├── runner.py                       # CREATE: PipelineRunner + branch naming
│   │   ├── watcher.py                      # CREATE: --auto tick loop
│   │   ├── merge.py                        # CREATE: Phase 9 entrypoint
│   │   ├── status.py                       # CREATE: ready_for_review table printer
│   │   ├── slack.py                        # CREATE: hermes chan message wrapper
│   │   ├── logging_setup.py                # CREATE: tick_id + file/stderr routing
│   │   └── cli.py                          # CREATE: argparse subcommands
│   ├── configs/
│   │   └── phases.yaml                     # CREATE: Phases 2-8 prompts/tools/turns
│   └── tests/
│       ├── conftest.py                     # CREATE: tmp_project fixture
│       ├── test_state.py
│       ├── test_phases.py
│       ├── test_selection.py
│       ├── test_kanban.py
│       ├── test_runner.py
│       ├── test_watcher.py
│       ├── test_merge.py
│       ├── test_status.py
│       ├── test_slack.py
│       ├── test_logging_setup.py
│       ├── test_cli.py
│       └── test_chaos.py                   # kill-mid-write chaos scenarios
├── .claude/skills/todos-manager/
│   └── SKILL.md                            # CREATE: skill spec per design §TODOS Manager Skill
└── docs/
    ├── pipeline-modularization-plan.md     # MODIFY: footnote pointing here
    └── superpowers/plans/
        └── 2026-06-11-todo-pipeline-orchestrator.md   # THIS FILE
```

**Per-project runtime files** (created by the pipeline, *not* by this plan; documented for reference):

```
<project>/
├── TODOS.md                                # owned by todos-manager + selection.py reader
└── .hermes/
    ├── todo_id_counter                     # plain integer, atomic write
    ├── pipeline_branch.txt                 # current branch (Phase 2 writes)
    ├── pipeline_checkpoints/<todo_id>.json # per-phase done markers
    └── ready_for_review/<todo_id>.json     # post-Phase-8 record

~/.hermes/                                  # user-global, lock + outbox + global state
├── pipeline_locks/<project>.lock
├── kanban_active_tasks.json                # project → hermes_task_id
├── kanban_outbox.jsonl                     # capped 500, collapse-latest-per-project
├── last_cycle_warning.json                 # project → [TODO-IDs] for dedup
└── pipeline.log                            # rotated daily, 7-day retention
```

## Lanes and Sequencing

| Lane | Module(s) | Depends on | Tasks |
|---|---|---|---|
| **0** | Package skeleton, `config.py`, `phases.py`, `slack.py`, `logging_setup.py`, test fixtures | — | T0.* |
| **A** | `.claude/skills/todos-manager/SKILL.md` | Lane 0 (logging conventions only) | TA.* (covers design §todos-manager + T10 preview gate + T8 error msgs) |
| **B** | `selection.py` | Lane 0 | TB.* (covers T3 cycle dedup + T4 parse isolation) |
| **C** | `kanban.py` | Lane 0 | TC.* (covers T1 outbox collapse + T7 outcome arg) |
| **D** | `state.py` (incl. `ReadyForReview`), then `merge.py` | Lane 0 | TD.* (covers T2 `failed`+`error`, T9 typed confirm) |
| **E** | `runner.py` | Lanes 0, C interface, D interface — interfaces are locked in this plan | TE.* (covers T14 scan+max+1 branch naming) |
| **F** | `watcher.py`, `status.py`, `cli.py`, docs/config updates | Lanes 0, A-E | TF.* (covers T6 subcommands, T11 tick_id, T12 log routing, T13 --help) |

**Execution order:** Lane 0 first. Then A, B, C, D, E in parallel. Then F.

---

## Lane 0: Package Skeleton + Shared Foundations

### T0.1: Create package metadata and uv-managed dev deps

**Files:**
- Create: `hermes-pipeline/pyproject.toml`
- Create: `hermes-pipeline/src/hermes_pipeline/__init__.py`
- Create: `hermes-pipeline/README.md`
- Modify: `pyproject.toml` (root) — add path dep on hermes-pipeline

- [ ] **Step 1: Write `hermes-pipeline/pyproject.toml`**

```toml
[project]
name = "hermes-pipeline"
version = "0.1.0"
description = "Autonomous TODOS.md pipeline orchestrator (selection, kanban, merge-gating)"
requires-python = ">=3.14"
dependencies = [
    "pyyaml>=6.0",
]

[project.scripts]
pipeline-watch = "hermes_pipeline.cli:main"

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-mock>=3.12",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/hermes_pipeline"]
```

- [ ] **Step 2: Write `hermes-pipeline/src/hermes_pipeline/__init__.py`**

```python
"""Hermes pipeline orchestrator package."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Write `hermes-pipeline/README.md`**

```markdown
# hermes-pipeline

Autonomous TODOS.md pipeline orchestrator. See
`../docs/gstack/hyonchoi-main-design-20260610-195349.md` for the full design and
`../docs/superpowers/plans/2026-06-11-todo-pipeline-orchestrator.md` for the
implementation plan.

## Install (dev)

    uv sync
    uv pip install -e ./hermes-pipeline

## Run

    pipeline-watch --help
```

- [ ] **Step 4: Update root `pyproject.toml`**

```toml
[project]
name = "todo-pipeline-orchestrator"
version = "0.1.0"
description = "Pipeline watcher and TODOS manager orchestration toolkit"
requires-python = ">=3.14"
dependencies = [
    "hermes-pipeline",
]

[tool.uv.sources]
hermes-pipeline = { path = "hermes-pipeline", editable = true }
```

- [ ] **Step 5: Run `uv sync` and verify install**

Run: `uv sync && uv run python -c "import hermes_pipeline; print(hermes_pipeline.__version__)"`
Expected: prints `0.1.0` with no errors.

- [ ] **Step 6: Commit**

```bash
git add hermes-pipeline/pyproject.toml hermes-pipeline/src/hermes_pipeline/__init__.py hermes-pipeline/README.md pyproject.toml uv.lock
git commit -m "feat(pipeline): scaffold hermes-pipeline package"
```

### T0.2: `config.py` — Config dataclass + env overrides

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/config.py`
- Test: `hermes-pipeline/tests/test_config.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_config.py
import os
from pathlib import Path
from hermes_pipeline.config import Config

def test_defaults():
    c = Config.default()
    assert c.lock_dir == Path.home() / ".hermes" / "pipeline_locks"
    assert c.projects_dir == Path.home() / "projects"
    assert c.claude_cmd == "claude"
    assert c.kanban_adapter == "null"

def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "projs"))
    monkeypatch.setenv("PIPELINE_CLAUDE_CMD", "/usr/bin/claude")
    monkeypatch.setenv("PIPELINE_KANBAN_ADAPTER", "hermes")
    c = Config.from_env()
    assert c.lock_dir == tmp_path / "locks"
    assert c.projects_dir == tmp_path / "projs"
    assert c.claude_cmd == "/usr/bin/claude"
    assert c.kanban_adapter == "hermes"
```

- [ ] **Step 2: Run test, expect ImportError/failure**

Run: `uv run pytest hermes-pipeline/tests/test_config.py -v`
Expected: FAIL — `hermes_pipeline.config` not found.

- [ ] **Step 3: Implement `config.py`**

```python
# src/hermes_pipeline/config.py
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

KanbanAdapterName = Literal["null", "hermes"]

@dataclass(frozen=True)
class Config:
    lock_dir: Path = field(default_factory=lambda: Path.home() / ".hermes" / "pipeline_locks")
    projects_dir: Path = field(default_factory=lambda: Path.home() / "projects")
    state_dir: Path = field(default_factory=lambda: Path.home() / ".hermes")
    claude_cmd: str = "claude"
    checkpoint_subdir: str = ".hermes/pipeline_checkpoints"
    ready_for_review_subdir: str = ".hermes/ready_for_review"
    counter_file_subpath: str = ".hermes/todo_id_counter"
    default_timeout: int = 1800
    kanban_adapter: KanbanAdapterName = "null"
    kanban_outbox_cap: int = 500
    log_file_subpath: str = "pipeline.log"
    log_retention_days: int = 7
    slack_channel: str = ""

    @classmethod
    def default(cls) -> "Config":
        return cls()

    @classmethod
    def from_env(cls) -> "Config":
        c = cls.default()
        env_map = {
            "PIPELINE_LOCK_DIR": ("lock_dir", Path),
            "PIPELINE_PROJECTS_DIR": ("projects_dir", Path),
            "PIPELINE_STATE_DIR": ("state_dir", Path),
            "PIPELINE_CLAUDE_CMD": ("claude_cmd", str),
            "PIPELINE_KANBAN_ADAPTER": ("kanban_adapter", str),
            "PIPELINE_SLACK_CHANNEL": ("slack_channel", str),
        }
        overrides = {}
        for env_key, (attr, ctor) in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                overrides[attr] = ctor(val)
        if not overrides:
            return c
        from dataclasses import replace
        return replace(c, **overrides)
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_config.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/config.py hermes-pipeline/tests/test_config.py
git commit -m "feat(pipeline): add Config with env overrides"
```

### T0.3: `phases.py` — Phase dataclass + YAML loader

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/phases.py`
- Create: `hermes-pipeline/configs/phases.yaml`
- Test: `hermes-pipeline/tests/test_phases.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_phases.py
from pathlib import Path
from hermes_pipeline.phases import Phase, load_phases

FIXTURE = """
phases:
  - phase_key: "phase_2_autoplan"
    name: "Phase 2: Autoplan"
    prompt: "do autoplan"
    tools: "Read,Write,Bash"
    turns: 20
    timeout: 1800
  - phase_key: "phase_8_finish"
    name: "Phase 8: Finish Branch"
    prompt: "finish branch"
    tools: "Read,Write,Bash"
    turns: 15
"""

def test_load_phases_from_yaml(tmp_path):
    p = tmp_path / "phases.yaml"
    p.write_text(FIXTURE)
    phases = load_phases(p)
    assert len(phases) == 2
    assert phases[0].phase_key == "phase_2_autoplan"
    assert phases[0].name == "Phase 2: Autoplan"
    assert phases[0].turns == 20
    assert phases[1].timeout == 1800  # default
```

- [ ] **Step 2: Run test, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_phases.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `phases.py`**

```python
# src/hermes_pipeline/phases.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import yaml

@dataclass(frozen=True)
class Phase:
    phase_key: str
    name: str
    prompt: str
    tools: str
    turns: int
    timeout: int = 1800

def load_phases(config_path: Path | str | None = None) -> list[Phase]:
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent.parent / "configs" / "phases.yaml"
    config_path = Path(config_path)
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return [Phase(**p) for p in data["phases"]]
```

- [ ] **Step 4: Create `configs/phases.yaml` with all phases 2-8**

```yaml
phases:
  - phase_key: "phase_2_autoplan"
    name: "Phase 2: Autoplan"
    prompt: |
      Use the gstack autoplan skill.
      1. Read TODOS.md and confirm the in-flight TODO is the one passed via context.
      2. Run CEO/Eng/UI/DX reviews.
      3. Write the plan into .hermes/plans/.
      4. Create a new branch from main and commit the plan.
      5. Save the branch name to .hermes/pipeline_branch.txt.
    tools: "Read,Write,Bash"
    turns: 20
    timeout: 1800
  - phase_key: "phase_3_writing_plan"
    name: "Phase 3: Writing Plan"
    prompt: "Use superpowers writing-plans to convert .hermes/plans/ into superpowers plan format."
    tools: "Read,Write,Bash"
    turns: 15
    timeout: 1800
  - phase_key: "phase_4_development"
    name: "Phase 4: Development"
    prompt: "Use superpowers subagent-driven-development or executing-plans to implement the plan."
    tools: "Read,Write,Edit,Bash"
    turns: 60
    timeout: 3600
  - phase_key: "phase_6_1_cso"
    name: "Phase 6.1: CSO Security Review"
    prompt: "Use the gstack cso skill. Run a security review of the current branch."
    tools: "Read,Write,Bash"
    turns: 20
    timeout: 1800
  - phase_key: "phase_7_document_release"
    name: "Phase 7: Document Release"
    prompt: |
      Generate release docs.
      1. Update CHANGELOG.md.
      2. Update README.md project structure if changed.
      3. Commit.
    tools: "Read,Write,Edit,Bash"
    turns: 15
    timeout: 1800
  - phase_key: "phase_8_finish_branch"
    name: "Phase 8: Finish Branch"
    prompt: "Use superpowers finishing-a-development-branch. Open a PR and HALT — do NOT merge."
    tools: "Read,Write,Bash"
    turns: 15
    timeout: 1800
```

- [ ] **Step 5: Run test, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_phases.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/phases.py hermes-pipeline/configs/phases.yaml hermes-pipeline/tests/test_phases.py
git commit -m "feat(pipeline): add Phase loader + phases.yaml (Phases 2-8)"
```

### T0.4: `slack.py` — hermes chan message wrapper (best-effort)

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/slack.py`
- Test: `hermes-pipeline/tests/test_slack.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_slack.py
import subprocess
from unittest.mock import patch
from hermes_pipeline.slack import notify

def test_notify_calls_hermes_chan_message():
    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        notify("ops", "📝 hello")
        run.assert_called_once_with(
            ["hermes", "chan", "message", "ops", "📝 hello"],
            capture_output=True, text=True, timeout=10,
        )

def test_notify_swallows_failure():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        notify("ops", "msg")  # must not raise

def test_notify_skips_when_channel_empty():
    with patch("subprocess.run") as run:
        notify("", "msg")
        run.assert_not_called()
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_slack.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `slack.py`**

```python
# src/hermes_pipeline/slack.py
from __future__ import annotations
import logging
import subprocess

log = logging.getLogger(__name__)

def notify(channel: str, message: str) -> None:
    """Send a Slack message via `hermes chan message`. Best-effort, never raises."""
    if not channel:
        return
    try:
        subprocess.run(
            ["hermes", "chan", "message", channel, message],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log.warning("slack notify failed: %s", e)
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_slack.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/slack.py hermes-pipeline/tests/test_slack.py
git commit -m "feat(pipeline): add slack.notify (best-effort hermes chan message)"
```

### T0.5: `logging_setup.py` — tick_id + file/stderr routing (T11, T12)

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/logging_setup.py`
- Test: `hermes-pipeline/tests/test_logging_setup.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_logging_setup.py
import logging
import re
from pathlib import Path
from hermes_pipeline.logging_setup import configure, new_tick_id, set_tick_id

def test_new_tick_id_is_ulid_like():
    tid = new_tick_id()
    assert re.fullmatch(r"[0-9A-Z]{26}", tid)

def test_configure_writes_to_file(tmp_path):
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7)
    set_tick_id("01TESTTICKULID0000000000AA")
    logging.getLogger("hermes_pipeline.test").info("hello world")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "hello world" in text
    assert "tick_id=01TESTTICKULID0000000000AA" in text

def test_tick_id_absent_when_unset(tmp_path):
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7)
    set_tick_id(None)
    logging.getLogger("hermes_pipeline.test").info("standalone")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "standalone" in text
    assert "tick_id=" not in text or "tick_id=-" in text
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_logging_setup.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `logging_setup.py`**

```python
# src/hermes_pipeline/logging_setup.py
from __future__ import annotations
import logging
import logging.handlers
import secrets
import sys
import time
from pathlib import Path

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_tick_id: str | None = None

def new_tick_id() -> str:
    """ULID-ish: 26 chars, Crockford base32, time-sortable prefix."""
    t = int(time.time() * 1000)
    time_part = ""
    for _ in range(10):
        time_part = _CROCKFORD[t & 0x1F] + time_part
        t >>= 5
    rand_part = "".join(_CROCKFORD[b & 0x1F] for b in secrets.token_bytes(16))
    return (time_part + rand_part)[:26]

def set_tick_id(tid: str | None) -> None:
    global _tick_id
    _tick_id = tid

def _current_tick_id() -> str:
    return _tick_id or "-"

class _TickFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.tick_id = _current_tick_id()
        return True

def configure(log_path: Path, retention_days: int = 7) -> None:
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
    root.setLevel(logging.INFO)
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_logging_setup.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/logging_setup.py hermes-pipeline/tests/test_logging_setup.py
git commit -m "feat(pipeline): add tick_id correlation + rotated file logging (T11/T12)"
```

### T0.6: `conftest.py` — shared `tmp_project` fixture

**Files:**
- Create: `hermes-pipeline/tests/conftest.py`

- [ ] **Step 1: Write the fixture**

```python
# tests/conftest.py
import pytest
from pathlib import Path

@pytest.fixture
def tmp_project(tmp_path):
    """A scratch project dir with TODOS.md + .hermes/."""
    proj = tmp_path / "demo"
    (proj / ".hermes").mkdir(parents=True)
    (proj / "TODOS.md").write_text("# TODOS\n\n")
    (proj / ".hermes" / "todo_id_counter").write_text("0")
    return proj

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A scratch ~/.hermes/ replacement."""
    sd = tmp_path / "state"
    (sd / "pipeline_locks").mkdir(parents=True)
    return sd
```

- [ ] **Step 2: Commit (no test to run yet)**

```bash
git add hermes-pipeline/tests/conftest.py
git commit -m "test(pipeline): add tmp_project + state_dir fixtures"
```

---

## Lane A: `todos-manager` Skill

Implements design §"TODOS Manager Skill". Skills are markdown specifications, not Python — the verification is by re-reading the SKILL.md against the design schema and running through one acceptance scenario by hand.

### TA.1: Scaffold the skill directory + frontmatter

**Files:**
- Create: `.claude/skills/todos-manager/SKILL.md`

- [ ] **Step 1: Write the file with frontmatter + Purpose section**

```markdown
---
name: todos-manager
description: "TODOS.md 항목 추가 및 관리 — gstack 형식 기반, TODO-<n> 안정 ID 자동 부여, 핵심 결정 사항 사전 정의"
version: 2.0.0
author: hyonchoi
license: MIT
metadata:
  hermes:
    tags: [todos, gstack, planning, pipeline]
    related_skills: [gstack-plan-eng-review, gstack-office-hours]
---

# todos-manager

Add a new entry to a project's `TODOS.md`, in gstack format, with a stable
`TODO-<n>` ID and `Decisions` metadata pre-filled so `pipeline-watch auto` can
pick it up on the next 5-minute tick with no further input.

This skill is the *only* writer of `.hermes/todo_id_counter`. `pipeline-watch`
never reads or writes it.

## When to use

The user wants to add a new TODO to a project. Triggers:
- "add a TODO to <project>"
- "새로운 TODO 항목"
- "queue up work in TODOS.md"
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/todos-manager/SKILL.md
git commit -m "feat(skill): scaffold todos-manager SKILL.md"
```

### TA.2: TODOS.md schema section (locked-in from design)

**Files:**
- Modify: `.claude/skills/todos-manager/SKILL.md`

- [ ] **Step 1: Append the Schema section verbatim from the design doc**

```markdown

## TODOS.md Schema

| Field | Description | Required |
|---|---|---|
| `TODO-<n>` (heading) | Stable ID, assigned once, never reused/renumbered | auto |
| **What** | Task description | yes |
| **Why** | Rationale / background | yes |
| **Pros** | Benefits | yes |
| **Cons** | Drawbacks / risks | yes |
| **Context** | Implementation starting point | yes |
| **Depends on** | `TODO-<n>` reference(s), same project only; `없음`/`none` if none | conditional |
| **Decisions** | Priority (P1-P4), Effort (S/M/L/XL), Phase, Branch, Test Coverage, Security Review | yes |

Status markers (unchanged):
- `[ ]` pending
- `[→]` in progress
- `[x]` done — deps count as **satisfied**
- `[~]` on hold — deps count as **unsatisfied** (selection.py owns this rule)

## Entry Template

```markdown
## TODO-<n>: [작업명]

**What:** [작업 내용]

**Why:** [이유]

**Pros:**
- [장점1]

**Cons:**
- [단점1]

**Context:** [구현 시작점/맥락]

**Depends on:** [TODO-<n>, 없으면 없음]

**Decisions:**
- Priority: P1 / P2 / P3 / P4
- Effort: S / M / L / XL
- Phase: 4 (Development) / 6.1 (CSO) / ...
- Branch: feature/short-desc
- Test Coverage: 필요 / 불필요
- Security Review: 필요 / 불필요
```
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/todos-manager/SKILL.md
git commit -m "docs(skill): add TODOS.md schema + entry template"
```

### TA.3: Stable ID assignment + counter + first-run bootstrap

**Files:**
- Modify: `.claude/skills/todos-manager/SKILL.md`

- [ ] **Step 1: Append the ID assignment + bootstrap sections from design**

```markdown

## Stable TODO-<n> ID Assignment

**Counter file:** `<project>/.hermes/todo_id_counter` — a per-project plain-text
file containing the most recently assigned integer ID. One counter per project;
no global counter.

**Assignment algorithm (no lock, atomic write):**

1. If `<project>/.hermes/todo_id_counter` does not exist → bootstrap (see below).
2. `n = read_int(counter)`            # e.g. 4
3. `new_id = n + 1`                   # 5
4. Write `new_id` to `counter.tmp`
5. `os.rename(counter.tmp, counter)`  # atomic, crash-safe
6. Append the `TODO-<new_id>` entry to `TODOS.md`.

`pipeline-watch` never touches this file — no contention, no lock needed. The
accepted risk is two simultaneous `todos-manager` invocations for the same
project at the literal same instant (vanishingly unlikely for interactive use).

**ID gap policy:** If step 6 fails after step 5 succeeded, the counter has
already advanced — that ID is permanently burned (gap in the sequence). This
is consistent with "never reused, never renumbered" (Premise 5: IDs are stable,
not necessarily contiguous) and requires no rollback.

## First-Run Bootstrap

If `<project>/TODOS.md` and/or `<project>/.hermes/todo_id_counter` are missing,
create both with no confirmation prompt:

    <project>/
      TODOS.md             ← "# TODOS\n\n"
      .hermes/
        todo_id_counter    ← "0"

Then proceed — the first entry becomes `TODO-1`.

**Out of scope:** counter recovery from existing `TODO-<n>` headings in a
hand-written TODOS.md (the "TODOS.md exists, counter doesn't" migration case).
Deferred TODO #2 in the design doc.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/todos-manager/SKILL.md
git commit -m "docs(skill): add stable ID + bootstrap algorithm"
```

### TA.4: Workflow with T10 preview/confirm gate

**Files:**
- Modify: `.claude/skills/todos-manager/SKILL.md`

- [ ] **Step 1: Append the Workflow section with the preview gate at step 7.5**

```markdown

## Workflow

1. Resolve `<project>` directory (cwd or skill argument).
2. Bootstrap `TODOS.md` + `.hermes/todo_id_counter` if missing (see above).
3. Take natural-language task description from the user.
4. Derive: What / Why / Pros / Cons / Context.
5. Ask user for **Depends on** (`TODO-<n>` or `none`).
   - If a `TODO-<n>` is given, validate it exists in `TODOS.md` (any state).
   - Not found → reject and suggest the nearest existing `TODO-<m>` by
     **numeric distance on the integer ID** (smallest `|m - n|`, ties go to
     the lower number). No cycle detection here — `selection.py` owns that.
6. Ask user for **Decisions**: Priority / Effort / Phase / Branch /
   Test Coverage / Security Review.
7. **Sanitize** all free-text fields (see Field Sanitization).
7.5. **Preview gate (T10).** Show the assembled entry exactly as it will be
   written and prompt `[y / edit / cancel]`:
   - `y` → proceed to step 8.
   - `edit` → jump back to step 4 (no ID burned, no files written).
   - `cancel` → abort entirely (no ID burned, no Slack notify).
8. Assign `TODO-<n>` (atomic counter increment).
9. Append the formatted entry to `TODOS.md`, status `[ ]`.
10. Notify: `hermes chan message <channel> "📝 TODO-<n> added to <project>: <What>"`.
    Best-effort; failure is logged but does not roll back steps 8-9.

## Field Sanitization

Before writing, escape any line within **What/Why/Pros/Cons/Context** that
begins with one or more `#` by prefixing a backslash (`\#`). This prevents
pasted text containing `## TODO-99: fake` from injecting fake headings that
`selection.py`'s parser would mis-attribute.

    Input  What: "## TODO-99: fake\nDo the thing"
    Output What: "\## TODO-99: fake\nDo the thing"
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/todos-manager/SKILL.md
git commit -m "docs(skill): add workflow with preview/confirm gate (T10)"
```

### TA.5: Error & Rescue map + T8 error message conventions

**Files:**
- Modify: `.claude/skills/todos-manager/SKILL.md`

- [ ] **Step 1: Append the Error & Rescue section verbatim from design + T8 conventions**

```markdown

## Error Messages (T8 convention)

Every error message **names the absolute path and a one-line remediation verb**
("Edit", "Check", "Set to", "Re-run"). Examples:

- counter corrupted →
  `"<project>/.hermes/todo_id_counter is corrupted (got '<content>', expected an integer). Edit the file and set it to the highest existing TODO-<n> in TODOS.md."`
- `.hermes/` unreadable →
  `"Cannot read/write <project>/.hermes/ — check permissions (read+write+execute for the current user)."`
- Depends-on not found →
  `"TODO-<n> not found in <project>/TODOS.md — did you mean TODO-<m>?"` (numeric-distance suggestion).
- counter write OK but append failed →
  `"TODO-<n> reserved in counter but TODOS.md write failed: <error>. Re-run todos-manager — the next entry will become TODO-<n+1>."`

## Error & Rescue Map

| Codepath | What can go wrong | Exception | Rescue | User sees |
|---|---|---|---|---|
| `read_counter()` | file missing | (bootstrap) | bootstrap to 0 | — |
| `read_counter()` | unreadable | `PermissionError` | abort, no writes | path + "check permissions" |
| `read_counter()` | not an integer | `ValueError` | abort, no writes | path + "Edit the file..." |
| `write_counter()` (tmp+rename) | disk full | `OSError` | abort before append | path + "<error>" |
| `read TODOS.md` | unreadable | `PermissionError` | abort | path + "check permissions" |
| Depends-on validation | unknown TODO-<n> | (handled) | reject + suggest | "did you mean TODO-<m>?" |
| `append_todo()` | disk full mid-write | `OSError` | log "TODO-<n> burned" | "TODO-<n> reserved but write failed... Re-run" |
| `notify` | `hermes` missing/fails | `CalledProcessError` | log warning | nothing (TODO already written) |

## Observability

One structured log line per workflow step (per design): `project`, input
description, bootstrap status, ID assigned, depends-on validity, write result,
notify result.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/todos-manager/SKILL.md
git commit -m "docs(skill): add error map + T8 path-naming convention"
```

### TA.6: Acceptance walkthrough (chaos + edge cases) in the skill body

**Files:**
- Modify: `.claude/skills/todos-manager/SKILL.md`

- [ ] **Step 1: Append the acceptance scenarios section**

```markdown

## Acceptance Scenarios

These are checks for the skill author and reviewers, not the runtime user:

1. **Happy path, first run in a fresh repo.** `TODOS.md` and counter both
   missing → bootstrap → `TODO-1` appended → Slack notified.
2. **Subsequent run.** Counter = 4 → new entry becomes `TODO-5` → counter
   becomes 5 atomically.
3. **Depends-on typo.** User answers `TODO-99` for deps; only `TODO-3,4,5,17`
   exist → suggestion is `TODO-17` (smallest `|99-17|=82` beats `|99-5|=94`...
   wait, smallest distance is `TODO-17` since `|99-17|=82 < |99-5|=94`; check
   the math at implementation time but the rule is "smallest absolute
   difference, ties to lower number").
4. **Preview cancel.** User types `cancel` at step 7.5 → counter unchanged,
   no Slack notify, no TODOS.md write.
5. **Chaos.** Kill the process between step 5 (counter rename) and step 9
   (TODOS.md append). Re-running `todos-manager` produces `TODO-<n+1>` (gap
   accepted) and the prior `TODO-<n>` is logged as burned.
6. **Field sanitization.** Pasted What that contains `## TODO-99: ...` is
   stored as `\## TODO-99: ...` so the parser sees plain text.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/skills/todos-manager/SKILL.md
git commit -m "docs(skill): add acceptance scenarios"
```

---

## Lane B: `selection.py`

Owns dep-graph build, cycle detection with dedup notify, parse-failure isolation, and the eligibility sort.

### TB.1: TODOS.md parser

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/selection.py`
- Test: `hermes-pipeline/tests/test_selection.py`

- [ ] **Step 1: Write failing parser test**

```python
# tests/test_selection.py
from hermes_pipeline.selection import parse_todos, Todo

TODOS = """\
# TODOS

## TODO-1: First
**What:** A
**Why:** B
**Pros:** -
**Cons:** -
**Context:** -
**Depends on:** none
**Decisions:**
- Priority: P2
- Effort: S
- Status: [x]

## TODO-2: Second
**What:** C
**Why:** D
**Pros:** -
**Cons:** -
**Context:** -
**Depends on:** TODO-1
**Decisions:**
- Priority: P1
- Effort: M
- Status: [ ]
"""

def test_parse_two_todos():
    todos = parse_todos(TODOS)
    assert len(todos) == 2
    assert todos[0] == Todo(id=1, title="First", status="x", priority=2, effort="S", depends_on=[])
    assert todos[1].id == 2
    assert todos[1].status == " "
    assert todos[1].priority == 1
    assert todos[1].depends_on == [1]
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py::test_parse_two_todos -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement parser stub**

```python
# src/hermes_pipeline/selection.py
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

_EFFORT_ORDER = {"S": 0, "M": 1, "L": 2, "XL": 3}

@dataclass(frozen=True)
class Todo:
    id: int
    title: str
    status: str           # " ", "→", "x", "~"
    priority: int         # 1..4
    effort: str           # S/M/L/XL
    depends_on: list[int] = field(default_factory=list)
    raw_index: int = 0    # declaration order

class TodosParseError(Exception):
    pass

_HEADING = re.compile(r"^##\s+TODO-(\d+):\s*(.+?)\s*$")
_DEPS = re.compile(r"^\*\*Depends on:\*\*\s*(.*)$", re.IGNORECASE)
_PRIO = re.compile(r"-\s*Priority:\s*P([1-4])")
_EFFORT = re.compile(r"-\s*Effort:\s*(XL|S|M|L)")
_STATUS = re.compile(r"-\s*Status:\s*\[(.)\]")

def parse_todos(text: str) -> list[Todo]:
    todos: list[Todo] = []
    lines = text.splitlines()
    i = 0
    raw_index = 0
    while i < len(lines):
        m = _HEADING.match(lines[i])
        if not m:
            i += 1
            continue
        tid = int(m.group(1))
        title = m.group(2).strip()
        block: list[str] = []
        i += 1
        while i < len(lines) and not _HEADING.match(lines[i]):
            block.append(lines[i])
            i += 1
        body = "\n".join(block)
        deps = []
        prio = 4
        effort = "M"
        status = " "
        for line in block:
            d = _DEPS.match(line)
            if d:
                raw = d.group(1).strip()
                if raw and raw.lower() not in ("none", "없음"):
                    deps = [int(x) for x in re.findall(r"TODO-(\d+)", raw)]
            p = _PRIO.search(line)
            if p:
                prio = int(p.group(1))
            e = _EFFORT.search(line)
            if e:
                effort = e.group(1)
            s = _STATUS.search(line)
            if s:
                status = s.group(1)
        if status not in (" ", "→", "x", "~"):
            raise TodosParseError(f"TODO-{tid} has invalid status marker '{status}'")
        if effort not in _EFFORT_ORDER:
            raise TodosParseError(f"TODO-{tid} has invalid effort '{effort}'")
        todos.append(Todo(
            id=tid, title=title, status=status,
            priority=prio, effort=effort, depends_on=deps, raw_index=raw_index,
        ))
        raw_index += 1
    return todos
```

- [ ] **Step 4: Run test, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py::test_parse_two_todos -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/selection.py hermes-pipeline/tests/test_selection.py
git commit -m "feat(selection): parse TODOS.md into Todo records"
```

### TB.2: Dependency graph + cycle detection

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/selection.py`
- Modify: `hermes-pipeline/tests/test_selection.py`

- [ ] **Step 1: Write failing cycle test**

```python
def test_detect_cycle_self_reference():
    from hermes_pipeline.selection import detect_cycles
    todos = [Todo(id=1, title="x", status=" ", priority=1, effort="S", depends_on=[1])]
    cycles = detect_cycles(todos)
    assert cycles == {frozenset({1})}

def test_detect_cycle_two_node():
    from hermes_pipeline.selection import detect_cycles
    todos = [
        Todo(id=1, title="a", status=" ", priority=1, effort="S", depends_on=[2]),
        Todo(id=2, title="b", status=" ", priority=1, effort="S", depends_on=[1]),
    ]
    assert detect_cycles(todos) == {frozenset({1, 2})}

def test_no_cycle():
    from hermes_pipeline.selection import detect_cycles
    todos = [
        Todo(id=1, title="a", status="x", priority=2, effort="S", depends_on=[]),
        Todo(id=2, title="b", status=" ", priority=1, effort="S", depends_on=[1]),
    ]
    assert detect_cycles(todos) == set()
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py -k cycle -v`
Expected: FAIL.

- [ ] **Step 3: Implement `detect_cycles`**

Append to `selection.py`:

```python
def detect_cycles(todos: Iterable[Todo]) -> set[frozenset[int]]:
    """Return strongly-connected components of size > 1, plus self-loops."""
    by_id = {t.id: t for t in todos}
    cycles: set[frozenset[int]] = set()
    # self-loops
    for t in by_id.values():
        if t.id in t.depends_on:
            cycles.add(frozenset({t.id}))
    # Tarjan's SCC
    index_counter = [0]
    stack: list[int] = []
    lowlinks: dict[int, int] = {}
    index: dict[int, int] = {}
    on_stack: dict[int, bool] = {}

    def strongconnect(v: int):
        index[v] = index_counter[0]
        lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in by_id.get(v, Todo(0, "", " ", 4, "S")).depends_on:
            if w not in by_id:
                continue
            if w not in index:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif on_stack.get(w):
                lowlinks[v] = min(lowlinks[v], index[w])
        if lowlinks[v] == index[v]:
            comp: list[int] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cycles.add(frozenset(comp))

    for v in by_id:
        if v not in index:
            strongconnect(v)
    return cycles
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py -k cycle -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/selection.py hermes-pipeline/tests/test_selection.py
git commit -m "feat(selection): cycle detection (Tarjan SCC + self-loops)"
```

### TB.3: Eligibility filter + sort

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/selection.py`
- Modify: `hermes-pipeline/tests/test_selection.py`

- [ ] **Step 1: Write failing tests**

```python
def _mk(id, status=" ", prio=2, effort="S", deps=None, raw=0, title="t"):
    return Todo(id=id, title=title, status=status, priority=prio,
                effort=effort, depends_on=deps or [], raw_index=raw)

def test_filter_eligible_respects_satisfied_deps():
    from hermes_pipeline.selection import filter_eligible
    todos = [
        _mk(1, status="x"),
        _mk(2, deps=[1]),
        _mk(3, deps=[5]),     # 5 doesn't exist → unsatisfied
        _mk(4, status="~"),
        _mk(5, deps=[4]),     # 4 is on hold → unsatisfied
    ]
    eligible = filter_eligible(todos, locked=False, cyclic_ids=set())
    assert {t.id for t in eligible} == {2}

def test_filter_eligible_project_locked():
    from hermes_pipeline.selection import filter_eligible
    todos = [_mk(1)]
    assert filter_eligible(todos, locked=True, cyclic_ids=set()) == []

def test_sort_eligible_priority_then_effort_then_unblocks_then_order():
    from hermes_pipeline.selection import sort_eligible
    a = _mk(1, prio=2, effort="S", raw=0)
    b = _mk(2, prio=1, effort="M", raw=1)
    c = _mk(3, prio=1, effort="S", raw=2)
    d = _mk(4, prio=1, effort="S", raw=3)
    todos = [a, b, c, d]
    # c unblocks something, d does not; both prio=1 effort=S
    out = sort_eligible(todos, unblocks={3})
    assert [t.id for t in out] == [3, 4, 2, 1]

def test_unblocks_something():
    from hermes_pipeline.selection import compute_unblocks
    todos = [
        _mk(1, status="x"),
        _mk(2),                        # eligible
        _mk(3, deps=[2]),              # would become eligible if 2 done → 2 unblocks 3
        _mk(4, deps=[2, 99]),          # never eligible (99 unknown) → 2 doesn't unblock 4
    ]
    unblocks = compute_unblocks(todos)
    assert unblocks == {2}
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py -k 'filter or sort or unblocks' -v`
Expected: FAIL.

- [ ] **Step 3: Implement filter/sort/unblocks**

Append to `selection.py`:

```python
def filter_eligible(
    todos: Iterable[Todo],
    *,
    locked: bool,
    cyclic_ids: set[int],
) -> list[Todo]:
    if locked:
        return []
    by_id = {t.id: t for t in todos}
    out: list[Todo] = []
    for t in by_id.values():
        if t.status != " ":
            continue
        if t.id in cyclic_ids:
            continue
        ok = True
        for dep in t.depends_on:
            d = by_id.get(dep)
            if d is None or d.status != "x":
                ok = False
                break
        if ok:
            out.append(t)
    return out

def compute_unblocks(todos: Iterable[Todo]) -> set[int]:
    """Set of TODO IDs whose completion would make at least one [ ] TODO's deps fully satisfied."""
    by_id = {t.id: t for t in todos}
    result: set[int] = set()
    for downstream in by_id.values():
        if downstream.status != " ":
            continue
        unmet = [d for d in downstream.depends_on
                 if d not in by_id or by_id[d].status != "x"]
        if len(unmet) == 1:
            candidate = unmet[0]
            cand_todo = by_id.get(candidate)
            if cand_todo and cand_todo.status == " ":
                result.add(candidate)
    return result

def sort_eligible(todos: Iterable[Todo], *, unblocks: set[int]) -> list[Todo]:
    def key(t: Todo):
        return (
            t.priority,
            _EFFORT_ORDER[t.effort],
            0 if t.id in unblocks else 1,   # unblocks_something desc
            t.id,                            # TODO-<n> asc (≡ raw_index in well-formed files)
        )
    return sorted(todos, key=key)
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/selection.py hermes-pipeline/tests/test_selection.py
git commit -m "feat(selection): eligibility filter + priority/effort/unblock/order sort"
```

### TB.4: Cycle-notification dedup via `last_cycle_warning.json` (T3)

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/selection.py`
- Modify: `hermes-pipeline/tests/test_selection.py`

- [ ] **Step 1: Write failing test**

```python
def test_cycle_notify_dedup(tmp_path, monkeypatch):
    from hermes_pipeline.selection import notify_cycle_changes
    notified: list[str] = []
    def fake_notify(channel, msg): notified.append(msg)
    warning_file = tmp_path / "last_cycle_warning.json"

    # First call: cycle {1,2}
    notify_cycle_changes("proj", {frozenset({1, 2})}, warning_file, fake_notify, channel="ops")
    assert len(notified) == 1
    # Second call: same composition → no re-notify
    notify_cycle_changes("proj", {frozenset({1, 2})}, warning_file, fake_notify, channel="ops")
    assert len(notified) == 1
    # Third call: changed composition → re-notify
    notify_cycle_changes("proj", {frozenset({1, 2, 3})}, warning_file, fake_notify, channel="ops")
    assert len(notified) == 2
    # Fourth call: cycle resolved → clear entry (no notification)
    notify_cycle_changes("proj", set(), warning_file, fake_notify, channel="ops")
    assert len(notified) == 2
    # Fifth call: same {1,2} regression after resolution → re-notify (NOT suppressed)
    notify_cycle_changes("proj", {frozenset({1, 2})}, warning_file, fake_notify, channel="ops")
    assert len(notified) == 3
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py::test_cycle_notify_dedup -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `selection.py`:

```python
import json

def _load_last_warning(path: Path) -> dict[str, list[list[int]]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

def _save_last_warning(path: Path, data: dict[str, list[list[int]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    tmp.replace(path)

def _normalize(cycles: set[frozenset[int]]) -> list[list[int]]:
    return sorted([sorted(list(c)) for c in cycles])

def notify_cycle_changes(
    project: str,
    cycles: set[frozenset[int]],
    warning_file: Path,
    notify_fn,
    *,
    channel: str,
) -> None:
    """Notify only if cycle composition changed since the last tick. Clears on resolution."""
    state = _load_last_warning(warning_file)
    current = _normalize(cycles)
    previous = state.get(project, [])
    if not cycles:
        if project in state:
            del state[project]
            _save_last_warning(warning_file, state)
        return
    if current == previous:
        return
    ids = sorted({tid for c in cycles for tid in c})
    notify_fn(channel, f"⚠️ {project}: TODOS.md dependency cycle detected involving {ids}")
    state[project] = current
    _save_last_warning(warning_file, state)
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py::test_cycle_notify_dedup -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/selection.py hermes-pipeline/tests/test_selection.py
git commit -m "feat(selection): cycle notification dedup with clear-on-resolve (T3)"
```

### TB.5: `select_for_project` orchestrator with parse isolation (T4)

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/selection.py`
- Modify: `hermes-pipeline/tests/test_selection.py`

- [ ] **Step 1: Write failing tests**

```python
def test_select_for_project_returns_first_eligible(tmp_project):
    from hermes_pipeline.selection import select_for_project
    (tmp_project / "TODOS.md").write_text("""\
## TODO-1: a
**What:** -
**Why:** -
**Pros:** -
**Cons:** -
**Context:** -
**Depends on:** none
**Decisions:**
- Priority: P1
- Effort: S
- Status: [ ]
""")
    chosen, parse_error = select_for_project(
        project_dir=tmp_project, project_name="demo",
        locked=False, cyclic_ids=set(),
    )
    assert chosen is not None and chosen.id == 1
    assert parse_error is None

def test_select_for_project_returns_none_on_parse_error(tmp_project):
    from hermes_pipeline.selection import select_for_project
    (tmp_project / "TODOS.md").write_text("""\
## TODO-1: bad
**What:** -
**Why:** -
**Pros:** -
**Cons:** -
**Context:** -
**Depends on:** none
**Decisions:**
- Priority: P1
- Effort: BOGUS
- Status: [ ]
""")
    chosen, parse_error = select_for_project(
        project_dir=tmp_project, project_name="demo",
        locked=False, cyclic_ids=set(),
    )
    assert chosen is None
    assert parse_error is not None
    assert "BOGUS" in str(parse_error) or "demo" in str(parse_error)
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py -k select_for_project -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `selection.py`:

```python
def select_for_project(
    *,
    project_dir: Path,
    project_name: str,
    locked: bool,
    cyclic_ids: set[int],
) -> tuple[Todo | None, Exception | None]:
    """Read+parse+select one TODO for a project. Returns (todo or None, parse_error or None)."""
    todos_path = project_dir / "TODOS.md"
    try:
        text = todos_path.read_text()
        todos = parse_todos(text)
    except (OSError, TodosParseError) as e:
        log.error("parse failure in %s: %s", project_name, e)
        return None, e
    unblocks = compute_unblocks(todos)
    eligible = filter_eligible(todos, locked=locked, cyclic_ids=cyclic_ids)
    if not eligible:
        log.info("no eligible TODO for project=%s", project_name)
        return None, None
    ranked = sort_eligible(eligible, unblocks=unblocks)
    return ranked[0], None
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_selection.py -k select_for_project -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/selection.py hermes-pipeline/tests/test_selection.py
git commit -m "feat(selection): per-project orchestrator with parse-error isolation (T4)"
```

---

## Lane C: `kanban.py`

KanbanClient interface, NullKanbanAdapter, HermesKanbanAdapter, outbox with create-preserving collapse (T1), outcome arg (T7).

### TC.1: Interface + result type + `NullKanbanAdapter`

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/kanban.py`
- Test: `hermes-pipeline/tests/test_kanban.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_kanban.py
from hermes_pipeline.kanban import (
    NullKanbanAdapter, SyncResult, KanbanOutcome,
)

def test_null_adapter_set_active_task_returns_ok():
    a = NullKanbanAdapter()
    r = a.set_active_task("proj", todo_id=1, title="t", phase="Phase 2")
    assert r.ok is True and r.task_id is None

def test_null_adapter_update_phase():
    a = NullKanbanAdapter()
    assert a.update_phase("proj", phase="Phase 4", status="running").ok

def test_null_adapter_clear_active_task_outcomes():
    a = NullKanbanAdapter()
    for outcome in ("merged", "rejected", "abandoned"):
        assert a.clear_active_task("proj", outcome=outcome).ok  # type: ignore[arg-type]
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement interface + null adapter**

```python
# src/hermes_pipeline/kanban.py
from __future__ import annotations
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

log = logging.getLogger(__name__)

KanbanOutcome = Literal["merged", "rejected", "abandoned"]
PhaseStatus = Literal["running", "done", "failed", "ready_for_review"]

@dataclass(frozen=True)
class SyncResult:
    ok: bool
    task_id: str | None = None
    error: str | None = None

class KanbanClient(Protocol):
    def set_active_task(self, project: str, *, todo_id: int, title: str, phase: str) -> SyncResult: ...
    def update_phase(self, project: str, *, phase: str, status: PhaseStatus) -> SyncResult: ...
    def clear_active_task(self, project: str, *, outcome: KanbanOutcome) -> SyncResult: ...

class NullKanbanAdapter:
    def set_active_task(self, project, *, todo_id, title, phase):
        log.info("null kanban: set_active_task project=%s todo=%s phase=%s", project, todo_id, phase)
        return SyncResult(ok=True)
    def update_phase(self, project, *, phase, status):
        log.info("null kanban: update_phase project=%s phase=%s status=%s", project, phase, status)
        return SyncResult(ok=True)
    def clear_active_task(self, project, *, outcome):
        log.info("null kanban: clear_active_task project=%s outcome=%s", project, outcome)
        return SyncResult(ok=True)
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/kanban.py hermes-pipeline/tests/test_kanban.py
git commit -m "feat(kanban): KanbanClient interface + NullKanbanAdapter"
```

### TC.2: Active-tasks mapping store

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/kanban.py`
- Modify: `hermes-pipeline/tests/test_kanban.py`

- [ ] **Step 1: Write failing tests**

```python
def test_active_tasks_store_roundtrip(tmp_path):
    from hermes_pipeline.kanban import ActiveTasksStore
    store = ActiveTasksStore(tmp_path / "kanban_active_tasks.json")
    assert store.get("proj") is None
    store.set("proj", "task-abc")
    assert store.get("proj") == "task-abc"
    store.drop("proj")
    assert store.get("proj") is None

def test_active_tasks_store_atomic(tmp_path):
    from hermes_pipeline.kanban import ActiveTasksStore
    p = tmp_path / "k.json"
    store = ActiveTasksStore(p)
    store.set("a", "1")
    store.set("b", "2")
    assert json.loads(p.read_text()) == {"a": "1", "b": "2"}
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -k active_tasks -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `kanban.py`:

```python
class ActiveTasksStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        tmp.replace(self.path)

    def get(self, project: str) -> str | None:
        return self._load().get(project)

    def set(self, project: str, task_id: str) -> None:
        d = self._load()
        d[project] = task_id
        self._save(d)

    def drop(self, project: str) -> None:
        d = self._load()
        d.pop(project, None)
        self._save(d)
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -k active_tasks -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/kanban.py hermes-pipeline/tests/test_kanban.py
git commit -m "feat(kanban): ActiveTasksStore (atomic JSON map of project → task id)"
```

### TC.3: Outbox with create-preserving collapse (T1)

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/kanban.py`
- Modify: `hermes-pipeline/tests/test_kanban.py`

- [ ] **Step 1: Write failing tests**

```python
def test_outbox_collapses_non_create_for_same_project(tmp_path):
    from hermes_pipeline.kanban import KanbanOutbox
    ob = KanbanOutbox(tmp_path / "outbox.jsonl", cap=500)
    ob.enqueue({"op": "update_phase", "project": "p", "phase": "Phase 2", "status": "running"}, has_task_id=True)
    ob.enqueue({"op": "update_phase", "project": "p", "phase": "Phase 4", "status": "running"}, has_task_id=True)
    entries = ob.entries_for("p")
    assert len(entries) == 1
    assert entries[0]["phase"] == "Phase 4"

def test_outbox_preserves_pending_create(tmp_path):
    from hermes_pipeline.kanban import KanbanOutbox
    ob = KanbanOutbox(tmp_path / "outbox.jsonl", cap=500)
    ob.enqueue({"op": "set_active_task", "project": "p", "todo_id": 1,
                "title": "t", "phase": "Phase 2"}, has_task_id=False)
    # A later update_phase failure must not overwrite the queued create.
    ob.enqueue({"op": "update_phase", "project": "p", "phase": "Phase 4", "status": "running"},
               has_task_id=False)
    entries = ob.entries_for("p")
    assert len(entries) == 1
    assert entries[0]["op"] == "set_active_task"
    # ... but it should fold the latest phase into the create's payload.
    assert entries[0]["phase"] == "Phase 4"

def test_outbox_cap_drops_oldest_across_projects(tmp_path):
    from hermes_pipeline.kanban import KanbanOutbox
    ob = KanbanOutbox(tmp_path / "outbox.jsonl", cap=3)
    for i, proj in enumerate(["a", "b", "c", "d"]):
        ob.enqueue({"op": "update_phase", "project": proj, "phase": "Phase 2", "status": "running"}, has_task_id=True)
    projs = {e["project"] for e in ob.all()}
    assert projs == {"b", "c", "d"}

def test_outbox_dequeue(tmp_path):
    from hermes_pipeline.kanban import KanbanOutbox
    ob = KanbanOutbox(tmp_path / "outbox.jsonl", cap=500)
    ob.enqueue({"op": "update_phase", "project": "p", "phase": "P", "status": "running"}, has_task_id=True)
    ob.dequeue_for("p")
    assert ob.entries_for("p") == []
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -k outbox -v`
Expected: FAIL.

- [ ] **Step 3: Implement outbox**

Append to `kanban.py`:

```python
class KanbanOutbox:
    """Append-only file with collapse-latest-per-project semantics.

    Rules (T1):
    - If queueing a non-create op while a pending create (no task_id captured)
      exists for the same project, fold the new op's phase/status into the
      create and keep the create.
    - Otherwise: replace any existing entry for that project with the new one.
    - Cap total entries; drop oldest first across all projects.
    """
    def __init__(self, path: Path, cap: int = 500):
        self.path = path
        self.cap = cap

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def entries_for(self, project: str) -> list[dict]:
        return [e for e in self.all() if e.get("project") == project]

    def _save(self, entries: list[dict]) -> None:
        if len(entries) > self.cap:
            entries = entries[-self.cap:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("\n".join(json.dumps(e, sort_keys=True) for e in entries) + ("\n" if entries else ""))
        tmp.replace(self.path)

    def enqueue(self, entry: dict, *, has_task_id: bool) -> None:
        entries = self.all()
        project = entry["project"]
        existing_idx = next((i for i, e in enumerate(entries) if e.get("project") == project), None)
        if existing_idx is None:
            entries.append(entry)
        else:
            existing = entries[existing_idx]
            if (
                existing.get("op") == "set_active_task"
                and not has_task_id
                and entry.get("op") != "set_active_task"
            ):
                # carve-out: keep the create, fold in latest phase/status
                for k in ("phase", "status"):
                    if k in entry:
                        existing[k] = entry[k]
                entries[existing_idx] = existing
            else:
                entries[existing_idx] = entry
        self._save(entries)

    def dequeue_for(self, project: str) -> None:
        entries = [e for e in self.all() if e.get("project") != project]
        self._save(entries)
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -k outbox -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/kanban.py hermes-pipeline/tests/test_kanban.py
git commit -m "feat(kanban): outbox with create-preserving collapse + cap (T1)"
```

### TC.4: HermesKanbanAdapter

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/kanban.py`
- Modify: `hermes-pipeline/tests/test_kanban.py`

- [ ] **Step 1: Write failing tests**

```python
def test_hermes_set_active_task_first_run(tmp_path):
    from hermes_pipeline.kanban import HermesKanbanAdapter, ActiveTasksStore, KanbanOutbox
    from unittest.mock import patch, MagicMock
    store = ActiveTasksStore(tmp_path / "active.json")
    outbox = KanbanOutbox(tmp_path / "outbox.jsonl")
    adapter = HermesKanbanAdapter(store=store, outbox=outbox)
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["hermes", "kanban", "boards"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["hermes", "kanban", "create"]:
            return MagicMock(returncode=0, stdout="task-xyz\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", side_effect=fake_run):
        r = adapter.set_active_task("proj", todo_id=7, title="hello", phase="Phase 2: Autoplan")
    assert r.ok and r.task_id == "task-xyz"
    assert store.get("proj") == "task-xyz"
    # bootstrap then create
    assert any(c[:3] == ["hermes", "kanban", "boards"] for c in calls)
    assert any(c[:3] == ["hermes", "kanban", "create"] for c in calls)

def test_hermes_set_active_task_resume_routes_to_update_phase(tmp_path):
    from hermes_pipeline.kanban import HermesKanbanAdapter, ActiveTasksStore, KanbanOutbox
    from unittest.mock import patch, MagicMock
    store = ActiveTasksStore(tmp_path / "active.json")
    store.set("proj", "task-existing")
    adapter = HermesKanbanAdapter(store=store, outbox=KanbanOutbox(tmp_path / "outbox.jsonl"))
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        r = adapter.set_active_task("proj", todo_id=7, title="hello", phase="Phase 2: Autoplan")
    assert r.ok
    # No 'create' call; only 'comment'
    invoked_ops = [c[2] for c in [args.args[0] for args in run.call_args_list]]
    assert "create" not in invoked_ops
    assert "comment" in invoked_ops

def test_hermes_update_phase_before_set_active_task_fails(tmp_path):
    from hermes_pipeline.kanban import HermesKanbanAdapter, ActiveTasksStore, KanbanOutbox
    store = ActiveTasksStore(tmp_path / "active.json")
    outbox = KanbanOutbox(tmp_path / "outbox.jsonl")
    adapter = HermesKanbanAdapter(store=store, outbox=outbox)
    r = adapter.update_phase("proj", phase="Phase 4", status="running")
    assert not r.ok
    assert "no active task" in (r.error or "").lower()

def test_hermes_clear_merged_calls_complete(tmp_path):
    from hermes_pipeline.kanban import HermesKanbanAdapter, ActiveTasksStore, KanbanOutbox
    from unittest.mock import patch, MagicMock
    store = ActiveTasksStore(tmp_path / "active.json")
    store.set("proj", "task-1")
    adapter = HermesKanbanAdapter(store=store, outbox=KanbanOutbox(tmp_path / "outbox.jsonl"))
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        r = adapter.clear_active_task("proj", outcome="merged")
    assert r.ok
    assert store.get("proj") is None
    args0 = run.call_args_list[0].args[0]
    assert args0[:3] == ["hermes", "kanban", "complete"]

def test_hermes_clear_abandoned_calls_archive(tmp_path):
    from hermes_pipeline.kanban import HermesKanbanAdapter, ActiveTasksStore, KanbanOutbox
    from unittest.mock import patch, MagicMock
    store = ActiveTasksStore(tmp_path / "active.json")
    store.set("proj", "task-1")
    adapter = HermesKanbanAdapter(store=store, outbox=KanbanOutbox(tmp_path / "outbox.jsonl"))
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        adapter.clear_active_task("proj", outcome="abandoned")
    args0 = run.call_args_list[0].args[0]
    assert args0[:3] == ["hermes", "kanban", "archive"]

def test_hermes_failure_enqueues_to_outbox(tmp_path):
    from hermes_pipeline.kanban import HermesKanbanAdapter, ActiveTasksStore, KanbanOutbox
    from unittest.mock import patch, MagicMock
    store = ActiveTasksStore(tmp_path / "active.json")
    outbox = KanbanOutbox(tmp_path / "outbox.jsonl")
    adapter = HermesKanbanAdapter(store=store, outbox=outbox)
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        r = adapter.set_active_task("proj", todo_id=1, title="t", phase="Phase 2")
    assert not r.ok
    queued = outbox.entries_for("proj")
    assert len(queued) == 1 and queued[0]["op"] == "set_active_task"
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -k hermes -v`
Expected: FAIL.

- [ ] **Step 3: Implement adapter**

Append to `kanban.py`:

```python
class HermesKanbanAdapter:
    def __init__(self, store: "ActiveTasksStore", outbox: "KanbanOutbox", *, timeout: int = 10):
        self.store = store
        self.outbox = outbox
        self.timeout = timeout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=self.timeout)

    def _bootstrap_board(self, project: str) -> bool:
        # 'hermes kanban boards' subcommand surface is treated as idempotent.
        # Treat any nonzero exit that includes "exists" as success; otherwise propagate.
        try:
            r = self._run(["hermes", "kanban", "boards", "create", project])
        except Exception as e:
            log.warning("board bootstrap call raised: %s", e)
            return False
        if r.returncode == 0:
            return True
        if "exists" in (r.stderr or "").lower() or "exists" in (r.stdout or "").lower():
            return True
        log.warning("board bootstrap nonzero: rc=%s stderr=%s", r.returncode, r.stderr)
        return False

    def set_active_task(self, project, *, todo_id, title, phase):
        existing = self.store.get(project)
        if existing:
            # resume case: do not create a second card; treat as update.
            return self.update_phase(project, phase=phase, status="running")
        if not self._bootstrap_board(project):
            payload = {"op": "set_active_task", "project": project,
                       "todo_id": todo_id, "title": title, "phase": phase}
            self.outbox.enqueue(payload, has_task_id=False)
            return SyncResult(ok=False, error="board bootstrap failed; queued")
        try:
            body = f"TODO-{todo_id}: {title}\n{phase}"
            r = self._run(["hermes", "kanban", "create",
                           "--board", project, "--title", f"TODO-{todo_id}: {title}",
                           "--body", body])
        except Exception as e:
            payload = {"op": "set_active_task", "project": project,
                       "todo_id": todo_id, "title": title, "phase": phase}
            self.outbox.enqueue(payload, has_task_id=False)
            return SyncResult(ok=False, error=str(e))
        if r.returncode != 0:
            payload = {"op": "set_active_task", "project": project,
                       "todo_id": todo_id, "title": title, "phase": phase}
            self.outbox.enqueue(payload, has_task_id=False)
            return SyncResult(ok=False, error=r.stderr or f"exit {r.returncode}")
        task_id = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
        if not task_id:
            payload = {"op": "set_active_task", "project": project,
                       "todo_id": todo_id, "title": title, "phase": phase}
            self.outbox.enqueue(payload, has_task_id=False)
            return SyncResult(ok=False, error="empty task id from hermes")
        self.store.set(project, task_id)
        return SyncResult(ok=True, task_id=task_id)

    def update_phase(self, project, *, phase, status):
        task_id = self.store.get(project)
        if not task_id:
            return SyncResult(ok=False, error=f"no active task for {project} — set_active_task first")
        comment = f"{phase} — {status}"
        try:
            r = self._run(["hermes", "kanban", "comment", task_id, comment])
        except Exception as e:
            self.outbox.enqueue(
                {"op": "update_phase", "project": project, "phase": phase, "status": status},
                has_task_id=True,
            )
            return SyncResult(ok=False, error=str(e))
        if r.returncode != 0:
            self.outbox.enqueue(
                {"op": "update_phase", "project": project, "phase": phase, "status": status},
                has_task_id=True,
            )
            return SyncResult(ok=False, error=r.stderr or f"exit {r.returncode}")
        return SyncResult(ok=True, task_id=task_id)

    def clear_active_task(self, project, *, outcome):
        task_id = self.store.get(project)
        if not task_id:
            return SyncResult(ok=True)  # already cleared
        sub = "complete" if outcome == "merged" else "archive"
        try:
            r = self._run(["hermes", "kanban", sub, task_id])
        except Exception as e:
            self.outbox.enqueue(
                {"op": "clear_active_task", "project": project, "outcome": outcome},
                has_task_id=True,
            )
            return SyncResult(ok=False, error=str(e))
        if r.returncode != 0:
            self.outbox.enqueue(
                {"op": "clear_active_task", "project": project, "outcome": outcome},
                has_task_id=True,
            )
            return SyncResult(ok=False, error=r.stderr or f"exit {r.returncode}")
        self.store.drop(project)
        return SyncResult(ok=True)
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -k hermes -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/kanban.py hermes-pipeline/tests/test_kanban.py
git commit -m "feat(kanban): HermesKanbanAdapter (create/comment/complete/archive)"
```

### TC.5: Outbox retry driver

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/kanban.py`
- Modify: `hermes-pipeline/tests/test_kanban.py`

- [ ] **Step 1: Write failing test**

```python
def test_outbox_drain_retries_and_dequeues_on_success(tmp_path):
    from hermes_pipeline.kanban import HermesKanbanAdapter, ActiveTasksStore, KanbanOutbox, drain_outbox
    from unittest.mock import patch, MagicMock
    store = ActiveTasksStore(tmp_path / "active.json")
    outbox = KanbanOutbox(tmp_path / "outbox.jsonl")
    outbox.enqueue(
        {"op": "set_active_task", "project": "p", "todo_id": 1, "title": "t", "phase": "Phase 2"},
        has_task_id=False,
    )
    adapter = HermesKanbanAdapter(store=store, outbox=outbox)
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="task-1\n", stderr="")
        drain_outbox(adapter, outbox)
    assert outbox.entries_for("p") == []
    assert store.get("p") == "task-1"
```

- [ ] **Step 2: Implement `drain_outbox`**

Append:

```python
def drain_outbox(adapter, outbox: "KanbanOutbox") -> None:
    """Retry each queued op; drop on success, leave queued on failure."""
    for entry in list(outbox.all()):
        project = entry["project"]
        op = entry["op"]
        if op == "set_active_task":
            r = adapter.set_active_task(
                project, todo_id=entry["todo_id"], title=entry["title"], phase=entry["phase"],
            )
        elif op == "update_phase":
            r = adapter.update_phase(project, phase=entry["phase"], status=entry["status"])
        elif op == "clear_active_task":
            r = adapter.clear_active_task(project, outcome=entry["outcome"])
        else:
            continue
        if r.ok:
            outbox.dequeue_for(project)
```

- [ ] **Step 3: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_kanban.py -k outbox_drain -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/kanban.py hermes-pipeline/tests/test_kanban.py
git commit -m "feat(kanban): drain_outbox retry driver"
```

---

## Lane D: `state.py` + Phase 9 (`merge.py`)

### TD.1: `State` — lock + hash + checkpoint

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/state.py`
- Test: `hermes-pipeline/tests/test_state.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_state.py
import json
from pathlib import Path
from hermes_pipeline.state import State

def test_lock_unlock(tmp_path):
    lock_dir = tmp_path / "locks"
    s = State(project="proj", lock_dir=lock_dir, checkpoint_dir=tmp_path / "cp", ready_dir=tmp_path / "rfr")
    assert not s.is_locked()
    s.lock()
    assert s.is_locked()
    s.unlock()
    assert not s.is_locked()

def test_hash_roundtrip(tmp_path):
    s = State("p", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    assert s.get_saved_hash() is None
    s.save_hash("abc")
    assert s.get_saved_hash() == "abc"

def test_checkpoint_phase_progress(tmp_path):
    s = State("p", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    assert s.last_completed_phase_index(todo_id=1) == -1
    s.mark_phase_done(todo_id=1, phase_key="phase_2_autoplan", phase_index=0)
    s.mark_phase_done(todo_id=1, phase_key="phase_3_writing_plan", phase_index=1)
    assert s.last_completed_phase_index(todo_id=1) == 1

def test_reset_clears_checkpoints(tmp_path):
    s = State("p", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    s.mark_phase_done(todo_id=1, phase_key="phase_2_autoplan", phase_index=0)
    s.reset(todo_id=1)
    assert s.last_completed_phase_index(todo_id=1) == -1
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_state.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `State`**

```python
# src/hermes_pipeline/state.py
from __future__ import annotations
import json
import os
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

MergeStatus = Literal["pending", "merged", "rejected", "abandoned", "failed"]

class State:
    def __init__(self, project: str, lock_dir: Path, checkpoint_dir: Path, ready_dir: Path):
        self.project = project
        self.lock_dir = Path(lock_dir)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.ready_dir = Path(ready_dir)

    @property
    def lock_path(self) -> Path:
        return self.lock_dir / f"{self.project}.lock"

    @property
    def hash_path(self) -> Path:
        return self.checkpoint_dir / "todos_hash.txt"

    def _cp_path(self, todo_id: int) -> Path:
        return self.checkpoint_dir / f"todo-{todo_id}.json"

    def is_locked(self) -> bool:
        return self.lock_path.exists()

    def lock(self) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        # O_EXCL guarantees we never silently re-acquire
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            raise RuntimeError(f"project {self.project} is already locked at {self.lock_path}")

    def unlock(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def get_saved_hash(self) -> str | None:
        if not self.hash_path.exists():
            return None
        return self.hash_path.read_text().strip()

    def save_hash(self, h: str) -> None:
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.hash_path.write_text(h)

    def last_completed_phase_index(self, *, todo_id: int) -> int:
        p = self._cp_path(todo_id)
        if not p.exists():
            return -1
        try:
            d = json.loads(p.read_text())
            return int(d.get("last_completed_index", -1))
        except (json.JSONDecodeError, OSError):
            return -1

    def mark_phase_done(self, *, todo_id: int, phase_key: str, phase_index: int) -> None:
        p = self._cp_path(todo_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        d = {"todo_id": todo_id, "phases": {}}
        if p.exists():
            try:
                d = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        d.setdefault("phases", {})[phase_key] = phase_index
        d["last_completed_index"] = max(int(d.get("last_completed_index", -1)), phase_index)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, sort_keys=True))
        tmp.replace(p)

    def reset(self, *, todo_id: int) -> None:
        p = self._cp_path(todo_id)
        if p.exists():
            p.unlink()
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_state.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/state.py hermes-pipeline/tests/test_state.py
git commit -m "feat(state): State class — lock, hash, checkpoints (per-TODO)"
```

### TD.2: `ReadyForReview` record + `merge_status: failed` + `error` (T2)

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/state.py`
- Modify: `hermes-pipeline/tests/test_state.py`

- [ ] **Step 1: Write failing tests**

```python
def test_ready_for_review_create_and_read(tmp_path):
    from hermes_pipeline.state import State, ReadyForReview
    s = State("p", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    rec = ReadyForReview(
        project="p", todo_id=7, branch="feat/0.1.0-x",
        pr_url="https://example/pr/1",
        phase_summaries={"phase_2_autoplan": "ok"},
        kanban_task_id="task-1",
        merge_status="pending",
    )
    s.write_ready_for_review(rec)
    got = s.read_ready_for_review(todo_id=7)
    assert got == rec

def test_ready_for_review_status_transitions(tmp_path):
    from hermes_pipeline.state import State
    s = State("p", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    s.write_ready_for_review_min(todo_id=3, branch="b", pr_url="u", kanban_task_id="k")
    s.set_merge_status(todo_id=3, status="failed", error="merge conflict on VERSION")
    rec = s.read_ready_for_review(todo_id=3)
    assert rec.merge_status == "failed"
    assert rec.error == "merge conflict on VERSION"
    s.set_merge_status(todo_id=3, status="merged", error=None)
    rec = s.read_ready_for_review(todo_id=3)
    assert rec.merge_status == "merged" and rec.error is None

def test_list_pending_records(tmp_path):
    from hermes_pipeline.state import State
    s = State("p", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    s.write_ready_for_review_min(todo_id=1, branch="b1", pr_url="u1", kanban_task_id="k1")
    s.write_ready_for_review_min(todo_id=2, branch="b2", pr_url="u2", kanban_task_id="k2")
    s.set_merge_status(todo_id=2, status="merged", error=None)
    pending = s.list_ready_for_review_pending()
    assert {r.todo_id for r in pending} == {1}
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_state.py -k ready_for_review -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `state.py`:

```python
@dataclass
class ReadyForReview:
    project: str
    todo_id: int
    branch: str
    pr_url: str
    phase_summaries: dict[str, str]
    kanban_task_id: str | None
    merge_status: MergeStatus = "pending"
    error: str | None = None
    created_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2)

    @staticmethod
    def from_json(s: str) -> "ReadyForReview":
        return ReadyForReview(**json.loads(s))

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

# Methods added to State (open class — same file as above):
def _rfr_path(self: "State", todo_id: int) -> Path:
    return self.ready_dir / f"todo-{todo_id}.json"
State._rfr_path = _rfr_path  # type: ignore[attr-defined]

def write_ready_for_review(self: "State", rec: "ReadyForReview") -> None:
    self.ready_dir.mkdir(parents=True, exist_ok=True)
    if not rec.created_at:
        rec.created_at = _now()
    p = self._rfr_path(rec.todo_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(rec.to_json())
    tmp.replace(p)
State.write_ready_for_review = write_ready_for_review  # type: ignore[attr-defined]

def write_ready_for_review_min(self: "State", *, todo_id: int, branch: str,
                                pr_url: str, kanban_task_id: str | None) -> None:
    rec = ReadyForReview(
        project=self.project, todo_id=todo_id, branch=branch, pr_url=pr_url,
        phase_summaries={}, kanban_task_id=kanban_task_id, merge_status="pending",
    )
    self.write_ready_for_review(rec)
State.write_ready_for_review_min = write_ready_for_review_min  # type: ignore[attr-defined]

def read_ready_for_review(self: "State", *, todo_id: int) -> "ReadyForReview | None":
    p = self._rfr_path(todo_id)
    if not p.exists():
        return None
    return ReadyForReview.from_json(p.read_text())
State.read_ready_for_review = read_ready_for_review  # type: ignore[attr-defined]

def set_merge_status(self: "State", *, todo_id: int, status: MergeStatus, error: str | None) -> None:
    rec = self.read_ready_for_review(todo_id=todo_id)
    if rec is None:
        raise FileNotFoundError(f"no ready_for_review record for todo {todo_id} in {self.project}")
    rec.merge_status = status
    rec.error = error
    self.write_ready_for_review(rec)
State.set_merge_status = set_merge_status  # type: ignore[attr-defined]

def list_ready_for_review_pending(self: "State") -> list["ReadyForReview"]:
    if not self.ready_dir.exists():
        return []
    out: list[ReadyForReview] = []
    for p in sorted(self.ready_dir.glob("todo-*.json")):
        try:
            rec = ReadyForReview.from_json(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if rec.merge_status in ("pending", "failed"):
            out.append(rec)
    return out
State.list_ready_for_review_pending = list_ready_for_review_pending  # type: ignore[attr-defined]
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_state.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/state.py hermes-pipeline/tests/test_state.py
git commit -m "feat(state): ReadyForReview record + merge_status incl. failed+error (T2)"
```

### TD.3: Phase 9 `merge.py` with typed e2e confirmation (T9)

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/merge.py`
- Test: `hermes-pipeline/tests/test_merge.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_merge.py
from unittest.mock import patch, MagicMock
from hermes_pipeline.state import State, ReadyForReview
from hermes_pipeline.kanban import NullKanbanAdapter
from hermes_pipeline.merge import run_phase9, MergeError

def _state(tmp_path):
    s = State("demo", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    s.write_ready_for_review_min(todo_id=7, branch="feat/0.1.0-x",
                                  pr_url="https://example/pr/9", kanban_task_id="k")
    return s

def test_phase9_no_record(tmp_path):
    s = State("demo", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    try:
        run_phase9(state=s, project_dir=tmp_path, todo_id=42,
                   kanban=NullKanbanAdapter(), confirm_fn=lambda tid: True)
    except MergeError as e:
        assert "no ready_for_review" in str(e).lower()
    else:
        assert False, "should have raised"

def test_phase9_typed_confirm_wrong_input_aborts(tmp_path):
    s = _state(tmp_path)
    with patch("subprocess.run"):
        run_phase9(state=s, project_dir=tmp_path, todo_id=7,
                   kanban=NullKanbanAdapter(), confirm_fn=lambda tid: False)
    rec = s.read_ready_for_review(todo_id=7)
    assert rec.merge_status == "pending"  # unchanged

def test_phase9_happy_path_marks_merged(tmp_path):
    s = _state(tmp_path)
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_phase9(state=s, project_dir=tmp_path, todo_id=7,
                   kanban=NullKanbanAdapter(), confirm_fn=lambda tid: True,
                   bump_fn=lambda rec: ("0.2.0", "minor"))
    rec = s.read_ready_for_review(todo_id=7)
    assert rec.merge_status == "merged"
    assert rec.error is None

def test_phase9_merge_conflict_sets_failed(tmp_path):
    s = _state(tmp_path)
    calls = {"n": 0}
    def fake_run(cmd, **kw):
        calls["n"] += 1
        # let VERSION/CHANGELOG writes succeed (these are file writes, not subprocess)
        # First subprocess.run is `git merge` here; make it conflict
        return MagicMock(returncode=1, stdout="", stderr="merge conflict")
    with patch("subprocess.run", side_effect=fake_run):
        run_phase9(state=s, project_dir=tmp_path, todo_id=7,
                   kanban=NullKanbanAdapter(), confirm_fn=lambda tid: True,
                   bump_fn=lambda rec: ("0.2.0", "minor"))
    rec = s.read_ready_for_review(todo_id=7)
    assert rec.merge_status == "failed"
    assert rec.error and "merge conflict" in rec.error
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_merge.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `merge.py`**

```python
# src/hermes_pipeline/merge.py
from __future__ import annotations
import logging
import subprocess
from pathlib import Path
from typing import Callable
from .state import State, ReadyForReview
from .kanban import KanbanClient

log = logging.getLogger(__name__)

class MergeError(Exception):
    pass

ConfirmFn = Callable[[int], bool]                          # (todo_id) -> bool
BumpFn = Callable[[ReadyForReview], tuple[str, str]]       # (record) -> (new_version, bump_label)

def typed_confirm_default(todo_id: int) -> bool:
    """Anti-reflex: user must type the TODO id verbatim."""
    print(f"Type the TODO ID to confirm merge (e.g. 'TODO-{todo_id}' or '{todo_id}'):")
    answer = input().strip()
    return answer in (str(todo_id), f"TODO-{todo_id}")

def default_bump(rec: ReadyForReview) -> tuple[str, str]:
    """Placeholder mapping — Open Question in the design doc; default to patch."""
    # TODO(deferred): parse `Decisions` from the TODO entry to choose major/minor/patch.
    return "0.0.0+todo." + str(rec.todo_id), "patch"

def _write_version(project_dir: Path, version: str) -> None:
    (project_dir / "VERSION").write_text(version + "\n")

def _append_changelog(project_dir: Path, version: str, todo_id: int, bump: str) -> None:
    p = project_dir / "CHANGELOG.md"
    existing = p.read_text() if p.exists() else "# Changelog\n\n"
    entry = f"\n## {version} ({bump}) — TODO-{todo_id}\n\n"
    p.write_text(existing.rstrip() + "\n" + entry)

def run_phase9(
    *,
    state: State,
    project_dir: Path,
    todo_id: int,
    kanban: KanbanClient,
    confirm_fn: ConfirmFn = typed_confirm_default,
    bump_fn: BumpFn = default_bump,
) -> None:
    rec = state.read_ready_for_review(todo_id=todo_id)
    if rec is None:
        raise MergeError(
            f"no ready_for_review record for TODO-{todo_id} in {state.project}. "
            f"Check {state.ready_dir} — has Phase 8 completed?"
        )

    if rec.merge_status not in ("pending", "failed"):
        raise MergeError(
            f"TODO-{todo_id} merge_status is already {rec.merge_status}; nothing to do."
        )

    confirmed = confirm_fn(todo_id)
    if not confirmed:
        # Treat declined as 'rejected' — but only on a 'pending' record.
        if rec.merge_status == "pending":
            state.set_merge_status(todo_id=todo_id, status="rejected", error=None)
            kanban.clear_active_task(state.project, outcome="rejected")
            state.unlock()
        log.info("merge declined for TODO-%s; merge_status=rejected", todo_id)
        return

    new_version, bump_label = bump_fn(rec)
    try:
        _write_version(project_dir, new_version)
        _append_changelog(project_dir, new_version, todo_id, bump_label)
    except OSError as e:
        raise MergeError(f"failed to write VERSION/CHANGELOG.md: {e}") from e

    r = subprocess.run(
        ["git", "merge", "--no-ff", rec.branch],
        cwd=project_dir, capture_output=True, text=True,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "git merge failed").strip()
        state.set_merge_status(
            todo_id=todo_id, status="failed",
            error=f"git merge of {rec.branch} failed: {msg}",
        )
        # lock stays held — see design Eng Review §2
        return

    state.set_merge_status(todo_id=todo_id, status="merged", error=None)
    kanban.clear_active_task(state.project, outcome="merged")
    state.unlock()

def abandon(*, state: State, todo_id: int, kanban: KanbanClient) -> None:
    rec = state.read_ready_for_review(todo_id=todo_id)
    if rec is None:
        raise MergeError(f"no ready_for_review record for TODO-{todo_id}")
    state.set_merge_status(todo_id=todo_id, status="abandoned", error=None)
    kanban.clear_active_task(state.project, outcome="abandoned")
    state.unlock()
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_merge.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/merge.py hermes-pipeline/tests/test_merge.py
git commit -m "feat(merge): Phase 9 run_phase9 with typed confirm + failed/error (T9, T2)"
```

### TD.4: Chaos test — VERSION written but merge interrupted

**Files:**
- Create: `hermes-pipeline/tests/test_chaos.py`

- [ ] **Step 1: Write the chaos test**

```python
# tests/test_chaos.py
from unittest.mock import patch
from hermes_pipeline.state import State
from hermes_pipeline.kanban import NullKanbanAdapter
from hermes_pipeline.merge import run_phase9

def test_chaos_simulate_kill_between_version_and_merge(tmp_path):
    """VERSION+CHANGELOG written, then `git merge` raises (simulating process kill).
    Re-running run_phase9 must reach 'merged' (or 'failed' with an error), never
    leaving merge_status=pending with VERSION already bumped and no record."""
    s = State("demo", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    s.write_ready_for_review_min(todo_id=1, branch="feat/0.1.0-x",
                                  pr_url="u", kanban_task_id="k")

    # First attempt — git merge "killed"
    with patch("subprocess.run", side_effect=KeyboardInterrupt):
        try:
            run_phase9(state=s, project_dir=tmp_path, todo_id=1,
                       kanban=NullKanbanAdapter(), confirm_fn=lambda tid: True,
                       bump_fn=lambda rec: ("0.2.0", "minor"))
        except KeyboardInterrupt:
            pass

    rec = s.read_ready_for_review(todo_id=1)
    # After the kill the record is still 'pending' but VERSION is bumped on disk.
    assert (tmp_path / "VERSION").exists()

    # Second attempt — succeeds.
    from unittest.mock import MagicMock
    with patch("subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_phase9(state=s, project_dir=tmp_path, todo_id=1,
                   kanban=NullKanbanAdapter(), confirm_fn=lambda tid: True,
                   bump_fn=lambda rec: ("0.2.0", "minor"))
    rec = s.read_ready_for_review(todo_id=1)
    assert rec.merge_status == "merged"
```

- [ ] **Step 2: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_chaos.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add hermes-pipeline/tests/test_chaos.py
git commit -m "test(merge): chaos — VERSION written, git merge killed, retry reaches merged"
```

---

## Lane E: `runner.py`

Owns branch-name decision (T14 scan+max+1) and phase-loop wiring of `set_active_task`/`update_phase`. Codes against the documented `KanbanClient` and `State` interfaces.

### TE.1: Branch name decision with scan+max+1 collision avoidance (T14)

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/runner.py`
- Test: `hermes-pipeline/tests/test_runner.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_runner.py
from unittest.mock import patch, MagicMock
from hermes_pipeline.runner import decide_branch_name

def _git_branch_list(out: str):
    return MagicMock(returncode=0, stdout=out, stderr="")

def test_branch_first_attempt(tmp_path):
    with patch("subprocess.run", return_value=_git_branch_list("")):
        name = decide_branch_name(
            project_dir=tmp_path, base_version="0.1.0", slug="cool",
            is_new_attempt=False, prior_attempt_branch_existed=False,
        )
    assert name == "feat/0.1.0-cool"

def test_branch_new_attempt_increments_with_no_existing(tmp_path):
    with patch("subprocess.run", return_value=_git_branch_list("")):
        name = decide_branch_name(
            project_dir=tmp_path, base_version="0.1.0", slug="cool",
            is_new_attempt=True, prior_attempt_branch_existed=True,
        )
    assert name == "feat/0.1.0-cool-attempt2"

def test_branch_scan_max_plus_one(tmp_path):
    listing = "  feat/0.1.0-cool-attempt2\n* feat/0.1.0-cool-attempt3\n"
    with patch("subprocess.run", return_value=_git_branch_list(listing)):
        name = decide_branch_name(
            project_dir=tmp_path, base_version="0.1.0", slug="cool",
            is_new_attempt=True, prior_attempt_branch_existed=True,
        )
    assert name == "feat/0.1.0-cool-attempt4"

def test_branch_checkpoint_resume_reuses_name(tmp_path):
    name = decide_branch_name(
        project_dir=tmp_path, base_version="0.1.0", slug="cool",
        is_new_attempt=False, prior_attempt_branch_existed=True,
    )
    assert name == "feat/0.1.0-cool"
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_runner.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# src/hermes_pipeline/runner.py
from __future__ import annotations
import logging
import re
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_ATTEMPT_RE = re.compile(r"-attempt(\d+)$")

def _scan_existing_attempt_numbers(project_dir: Path, prefix: str) -> list[int]:
    pattern = f"{prefix}-attempt*"
    r = subprocess.run(
        ["git", "branch", "--list", pattern],
        cwd=project_dir, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    out: list[int] = []
    for line in (r.stdout or "").splitlines():
        name = line.replace("*", "").strip()
        m = _ATTEMPT_RE.search(name)
        if m:
            out.append(int(m.group(1)))
    return out

def decide_branch_name(
    *,
    project_dir: Path,
    base_version: str,
    slug: str,
    is_new_attempt: bool,
    prior_attempt_branch_existed: bool,
) -> str:
    """Per design doc §runner.py branch naming.

    - First attempt: feat/{base}-{slug}
    - Checkpoint resume (is_new_attempt=False) with a prior branch: reuse the same name.
    - New attempt after rejection/abandon: feat/{base}-{slug}-attempt<max(existing)+1>,
      or -attempt2 if no -attemptN exists yet (T14: scan+max+1).
    """
    base = f"feat/{base_version}-{slug}"
    if not is_new_attempt:
        return base
    if not prior_attempt_branch_existed:
        return base
    existing = _scan_existing_attempt_numbers(project_dir, base)
    next_n = max(existing) + 1 if existing else 2
    return f"{base}-attempt{next_n}"
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_runner.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/runner.py hermes-pipeline/tests/test_runner.py
git commit -m "feat(runner): decide_branch_name with scan+max+1 attempt numbering (T14)"
```

### TE.2: `PipelineRunner.run` — phase loop with kanban wiring

**Files:**
- Modify: `hermes-pipeline/src/hermes_pipeline/runner.py`
- Modify: `hermes-pipeline/tests/test_runner.py`

- [ ] **Step 1: Write failing tests**

```python
def test_runner_calls_set_active_task_and_update_phase(tmp_path):
    from hermes_pipeline.runner import PipelineRunner
    from hermes_pipeline.state import State
    from hermes_pipeline.phases import Phase

    class FakeKanban:
        def __init__(self):
            self.calls = []
        def set_active_task(self, project, *, todo_id, title, phase):
            self.calls.append(("set", project, todo_id, title, phase))
            from hermes_pipeline.kanban import SyncResult
            return SyncResult(ok=True, task_id="task-1")
        def update_phase(self, project, *, phase, status):
            self.calls.append(("upd", project, phase, status))
            from hermes_pipeline.kanban import SyncResult
            return SyncResult(ok=True)
        def clear_active_task(self, project, *, outcome):
            self.calls.append(("clr", project, outcome))
            from hermes_pipeline.kanban import SyncResult
            return SyncResult(ok=True)

    phases = [
        Phase(phase_key="p2", name="Phase 2", prompt="x", tools="Read", turns=1),
        Phase(phase_key="p8", name="Phase 8", prompt="x", tools="Read", turns=1),
    ]
    state = State("demo", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    kanban = FakeKanban()
    runner = PipelineRunner(
        project="demo", project_dir=tmp_path, branch="feat/0.1.0-x",
        todo_id=7, title="cool", phases=phases, state=state, kanban=kanban,
        run_phase_fn=lambda phase: 0,  # always succeed
    )
    runner.run()
    ops = [c[0] for c in kanban.calls]
    assert "set" in ops
    assert ops.count("upd") >= 2

def test_runner_writes_ready_for_review_after_phase_8(tmp_path):
    from hermes_pipeline.runner import PipelineRunner
    from hermes_pipeline.state import State
    from hermes_pipeline.phases import Phase
    from hermes_pipeline.kanban import NullKanbanAdapter

    phases = [Phase(phase_key="p8", name="Phase 8: Finish Branch",
                    prompt="x", tools="Read", turns=1)]
    state = State("demo", tmp_path / "l", tmp_path / "cp", tmp_path / "rfr")
    runner = PipelineRunner(
        project="demo", project_dir=tmp_path, branch="feat/0.1.0-x",
        todo_id=7, title="cool", phases=phases, state=state,
        kanban=NullKanbanAdapter(),
        run_phase_fn=lambda phase: 0,
        pr_url_resolver=lambda: "https://example/pr/42",
    )
    runner.run()
    rec = state.read_ready_for_review(todo_id=7)
    assert rec is not None and rec.branch == "feat/0.1.0-x"
    assert rec.pr_url == "https://example/pr/42"
    assert rec.merge_status == "pending"
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_runner.py -k 'kanban or ready' -v`
Expected: FAIL.

- [ ] **Step 3: Implement `PipelineRunner`**

Append to `runner.py`:

```python
from dataclasses import dataclass
from .state import State
from .phases import Phase
from .kanban import KanbanClient

@dataclass
class PipelineRunner:
    project: str
    project_dir: Path
    branch: str
    todo_id: int
    title: str
    phases: list[Phase]
    state: State
    kanban: KanbanClient
    run_phase_fn: Callable[[Phase], int]              # injected for testability
    pr_url_resolver: Callable[[], str] = lambda: ""    # default empty

    def run(self) -> bool:
        # Best-effort kanban — failures don't block the pipeline
        first_phase_name = self.phases[0].name if self.phases else "Phase 2"
        self.kanban.set_active_task(
            self.project, todo_id=self.todo_id, title=self.title, phase=first_phase_name,
        )
        for idx, phase in enumerate(self.phases):
            self.kanban.update_phase(self.project, phase=phase.name, status="running")
            rc = self.run_phase_fn(phase)
            if rc != 0:
                self.kanban.update_phase(self.project, phase=phase.name, status="failed")
                log.error("phase %s failed rc=%s", phase.phase_key, rc)
                return False
            self.state.mark_phase_done(todo_id=self.todo_id, phase_key=phase.phase_key, phase_index=idx)
            self.kanban.update_phase(self.project, phase=phase.name, status="done")

        # After Phase 8: persist ready_for_review, mark kanban, KEEP THE LOCK HELD.
        pr_url = self.pr_url_resolver()
        self.state.write_ready_for_review_min(
            todo_id=self.todo_id, branch=self.branch, pr_url=pr_url,
            kanban_task_id=getattr(self.kanban, "last_task_id", None),
        )
        self.kanban.update_phase(self.project, phase="Phase 8: Finish Branch", status="ready_for_review")
        return True
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_runner.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/runner.py hermes-pipeline/tests/test_runner.py
git commit -m "feat(runner): PipelineRunner phase loop with kanban wiring + ready_for_review handoff"
```

---

## Lane F: CLI, watcher, status, docs

### TF.1: `watcher.py` — `--auto` tick with `tick_id` (T11)

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/watcher.py`
- Test: `hermes-pipeline/tests/test_watcher.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_watcher.py
from pathlib import Path
from unittest.mock import patch
from hermes_pipeline.watcher import discover_projects, auto_tick

def test_discover_projects_finds_dirs_with_todos_md(tmp_path):
    (tmp_path / "a" / ".hermes").mkdir(parents=True)
    (tmp_path / "a" / "TODOS.md").write_text("# TODOS")
    (tmp_path / "b").mkdir()                       # no TODOS.md
    (tmp_path / "c" / ".hermes").mkdir(parents=True)
    (tmp_path / "c" / "TODOS.md").write_text("# TODOS")
    projects = discover_projects(tmp_path)
    assert {p.name for p in projects} == {"a", "c"}

def test_auto_tick_isolates_parse_errors_per_project(tmp_path, monkeypatch):
    # Two projects, one malformed.
    for name, body in [("good", "## TODO-1: ok\n**What:** -\n**Why:** -\n**Pros:** -\n"
                                "**Cons:** -\n**Context:** -\n**Depends on:** none\n"
                                "**Decisions:**\n- Priority: P1\n- Effort: S\n- Status: [ ]\n"),
                       ("bad", "## TODO-1: bad\n**Decisions:**\n- Effort: BOGUS\n- Status: [ ]\n")]:
        d = tmp_path / name
        (d / ".hermes").mkdir(parents=True)
        (d / "TODOS.md").write_text(body)
    notified = []
    selected = []
    auto_tick(
        projects_dir=tmp_path,
        lock_dir=tmp_path / "locks",
        state_dir=tmp_path / "state",
        on_selected=lambda project, todo: selected.append((project, todo.id)),
        notify_fn=lambda channel, msg: notified.append(msg),
        slack_channel="ops",
    )
    assert any(p == "good" for p, _ in selected)
    assert any("bad" in m for m in notified)
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_watcher.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `watcher.py`**

```python
# src/hermes_pipeline/watcher.py
from __future__ import annotations
import logging
from pathlib import Path
from typing import Callable
from .logging_setup import new_tick_id, set_tick_id
from .selection import select_for_project, detect_cycles, parse_todos, notify_cycle_changes
from .state import State

log = logging.getLogger(__name__)

def discover_projects(projects_dir: Path) -> list[Path]:
    out: list[Path] = []
    if not projects_dir.exists():
        return out
    for child in sorted(projects_dir.iterdir()):
        if child.is_dir() and (child / "TODOS.md").exists():
            out.append(child)
    return out

def auto_tick(
    *,
    projects_dir: Path,
    lock_dir: Path,
    state_dir: Path,
    on_selected: Callable[[str, object], None],
    notify_fn: Callable[[str, str], None],
    slack_channel: str,
) -> None:
    tid = new_tick_id()
    set_tick_id(tid)
    log.info("auto tick start projects_dir=%s", projects_dir)
    warning_file = state_dir / "last_cycle_warning.json"
    for proj_dir in discover_projects(projects_dir):
        try:
            text = (proj_dir / "TODOS.md").read_text()
            all_todos = parse_todos(text)
            cycles = detect_cycles(all_todos)
            notify_cycle_changes(
                proj_dir.name, cycles, warning_file, notify_fn, channel=slack_channel,
            )
            cyclic_ids = {tid for c in cycles for tid in c}
            state = State(
                project=proj_dir.name, lock_dir=lock_dir,
                checkpoint_dir=proj_dir / ".hermes" / "pipeline_checkpoints",
                ready_dir=proj_dir / ".hermes" / "ready_for_review",
            )
            chosen, parse_err = select_for_project(
                project_dir=proj_dir, project_name=proj_dir.name,
                locked=state.is_locked(), cyclic_ids=cyclic_ids,
            )
            if parse_err is not None:
                notify_fn(slack_channel,
                    f"⚠️ TODOS.md parse error in {proj_dir.name}: {parse_err}")
                continue
            if chosen is not None:
                on_selected(proj_dir.name, chosen)
        except Exception as e:
            log.exception("project %s tick failed", proj_dir.name)
            notify_fn(slack_channel,
                f"⚠️ {proj_dir.name} tick failed: {e}")
    set_tick_id(None)
    log.info("auto tick done")
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_watcher.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/watcher.py hermes-pipeline/tests/test_watcher.py
git commit -m "feat(watcher): auto tick with discovery + per-project isolation (T4, T11)"
```

### TF.2: `status.py` — `pipeline-watch status` table (T6/F2)

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/status.py`
- Test: `hermes-pipeline/tests/test_status.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_status.py
from pathlib import Path
from hermes_pipeline.status import collect_pending, format_table
from hermes_pipeline.state import State

def test_collect_and_format_table(tmp_path):
    for name in ("alpha", "beta"):
        d = tmp_path / name
        (d / ".hermes").mkdir(parents=True)
        (d / "TODOS.md").write_text("# TODOS")
        st = State(project=name, lock_dir=tmp_path / "locks",
                   checkpoint_dir=d / ".hermes" / "pipeline_checkpoints",
                   ready_dir=d / ".hermes" / "ready_for_review")
        st.write_ready_for_review_min(todo_id=1, branch=f"feat/0.1.0-{name}",
                                       pr_url=f"https://example/{name}", kanban_task_id="k")
    rows = collect_pending(tmp_path, lock_dir=tmp_path / "locks")
    assert len(rows) == 2
    out = format_table(rows)
    assert "PROJECT" in out and "alpha" in out and "beta" in out
    assert "pending" in out
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_status.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# src/hermes_pipeline/status.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from .state import State, ReadyForReview

@dataclass
class StatusRow:
    project: str
    todo_id: int
    branch: str
    pr_url: str
    merge_status: str
    age: str

def _age(iso: str) -> str:
    if not iso:
        return "-"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "-"
    delta = datetime.now(timezone.utc) - t
    secs = int(delta.total_seconds())
    if secs < 3600: return f"{secs // 60}m"
    if secs < 86400: return f"{secs // 3600}h"
    return f"{secs // 86400}d"

def collect_pending(projects_dir: Path, *, lock_dir: Path) -> list[StatusRow]:
    rows: list[StatusRow] = []
    if not projects_dir.exists():
        return rows
    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue
        st = State(project=proj_dir.name, lock_dir=lock_dir,
                   checkpoint_dir=proj_dir / ".hermes" / "pipeline_checkpoints",
                   ready_dir=proj_dir / ".hermes" / "ready_for_review")
        for rec in st.list_ready_for_review_pending():
            rows.append(StatusRow(
                project=rec.project, todo_id=rec.todo_id, branch=rec.branch,
                pr_url=rec.pr_url, merge_status=rec.merge_status, age=_age(rec.created_at),
            ))
    return rows

def format_table(rows: list[StatusRow]) -> str:
    headers = ["PROJECT", "TODO", "BRANCH", "PR", "STATUS", "AGE"]
    data = [[r.project, f"TODO-{r.todo_id}", r.branch, r.pr_url, r.merge_status, r.age] for r in rows]
    widths = [max(len(str(c)) for c in (col_headers + col_data))
              for col_headers, col_data in zip([[h] for h in headers], list(zip(*data)) if data else [[] for _ in headers])]
    def fmt(row): return "  ".join(str(c).ljust(w) for c, w in zip(row, widths))
    return "\n".join([fmt(headers)] + [fmt(r) for r in data])
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_status.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/status.py hermes-pipeline/tests/test_status.py
git commit -m "feat(status): pipeline-watch status pending-records table (T6/F2)"
```

### TF.3: `cli.py` — argparse subcommands + populated `--help` (T6, T13)

**Files:**
- Create: `hermes-pipeline/src/hermes_pipeline/cli.py`
- Test: `hermes-pipeline/tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli.py
import sys
from hermes_pipeline.cli import build_parser

def test_build_parser_has_three_subcommands():
    parser = build_parser()
    actions = {a.dest: a for a in parser._actions}
    sub = actions["command"]
    names = set(sub.choices.keys())
    assert names == {"auto", "merge", "status"}

def test_auto_help_includes_example():
    parser = build_parser()
    help_text = parser.parse_args(["auto", "--help"]) if False else parser._subparsers._group_actions[0].choices["auto"].format_help()
    assert "Example" in help_text or "example" in help_text

def test_merge_requires_project_and_todo():
    parser = build_parser()
    try:
        parser.parse_args(["merge"])
    except SystemExit:
        return
    assert False

def test_status_runs_without_args():
    parser = build_parser()
    ns = parser.parse_args(["status"])
    assert ns.command == "status"
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest hermes-pipeline/tests/test_cli.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `cli.py`**

```python
# src/hermes_pipeline/cli.py
from __future__ import annotations
import argparse
import logging
import sys
from pathlib import Path
from .config import Config
from .logging_setup import configure as configure_logging
from .slack import notify
from .watcher import auto_tick
from .status import collect_pending, format_table
from .state import State
from .kanban import NullKanbanAdapter, HermesKanbanAdapter, ActiveTasksStore, KanbanOutbox
from .merge import run_phase9, abandon, MergeError

log = logging.getLogger(__name__)

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline-watch",
        description=(
            "Autonomous TODOS.md pipeline orchestrator. Subcommands: auto (tick, run by cron); "
            "merge (Phase 9, explicit human gate); status (list pending-review records)."
        ),
        epilog=(
            "Examples:\n"
            "  pipeline-watch auto\n"
            "  pipeline-watch merge demo 7\n"
            "  pipeline-watch status\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    auto = sub.add_parser(
        "auto",
        description="Run one tick: scan projects, select an eligible TODO per project, run Phases 2-8.",
        epilog="Example: pipeline-watch auto",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    merge = sub.add_parser(
        "merge",
        description="Phase 9: e2e-confirm, bump VERSION/CHANGELOG.md, git merge, clear kanban.",
        epilog="Example: pipeline-watch merge demo 7",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    merge.add_argument("project", help="project directory name (under PIPELINE_PROJECTS_DIR)")
    merge.add_argument("todo_id", type=int, help="TODO-<n> integer id")
    merge.add_argument("--abandon", action="store_true",
                       help="mark merge_status=abandoned and release the lock; no merge attempted")

    status = sub.add_parser(
        "status",
        description="Print a table of all pending ready_for_review records across projects.",
        epilog="Example: pipeline-watch status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    return p

def _make_kanban(config: Config) -> "KanbanClient":
    if config.kanban_adapter == "hermes":
        return HermesKanbanAdapter(
            store=ActiveTasksStore(config.state_dir / "kanban_active_tasks.json"),
            outbox=KanbanOutbox(config.state_dir / "kanban_outbox.jsonl",
                                 cap=config.kanban_outbox_cap),
        )
    return NullKanbanAdapter()

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    config = Config.from_env()
    configure_logging(config.state_dir / config.log_file_subpath, config.log_retention_days)

    if args.command == "auto":
        def _on_selected(project: str, todo):
            log.info("would dispatch project=%s TODO-%s (runner wiring is repo-side)",
                     project, todo.id)
        auto_tick(
            projects_dir=config.projects_dir,
            lock_dir=config.lock_dir,
            state_dir=config.state_dir,
            on_selected=_on_selected,
            notify_fn=lambda ch, m: notify(ch, m),
            slack_channel=config.slack_channel,
        )
        return 0

    if args.command == "status":
        rows = collect_pending(config.projects_dir, lock_dir=config.lock_dir)
        print(format_table(rows))
        return 0

    if args.command == "merge":
        project_dir = config.projects_dir / args.project
        state = State(
            project=args.project, lock_dir=config.lock_dir,
            checkpoint_dir=project_dir / ".hermes" / "pipeline_checkpoints",
            ready_dir=project_dir / ".hermes" / "ready_for_review",
        )
        kanban = _make_kanban(config)
        try:
            if args.abandon:
                abandon(state=state, todo_id=args.todo_id, kanban=kanban)
            else:
                run_phase9(state=state, project_dir=project_dir, todo_id=args.todo_id, kanban=kanban)
        except MergeError as e:
            print(f"merge: {e}", file=sys.stderr)
            return 2
        return 0

    parser.error("unknown command")
    return 2

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run, expect PASS**

Run: `uv run pytest hermes-pipeline/tests/test_cli.py -v`
Expected: 4 passed.

- [ ] **Step 5: Manually verify --help**

Run: `uv run pipeline-watch --help && uv run pipeline-watch auto --help && uv run pipeline-watch merge --help && uv run pipeline-watch status --help`
Expected: each shows description + at least one example invocation.

- [ ] **Step 6: Commit**

```bash
git add hermes-pipeline/src/hermes_pipeline/cli.py hermes-pipeline/tests/test_cli.py
git commit -m "feat(cli): auto/merge/status subcommands with populated --help (T6, T13)"
```

### TF.4: Cron registration helper script

**Files:**
- Create: `hermes-pipeline/scripts/install-cron.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Register the 5-minute --auto tick. Idempotent; safe to re-run.
set -euo pipefail
CRON_LINE="*/5 * * * * /usr/bin/env -i HOME=$HOME PATH=$PATH bash -lc 'pipeline-watch auto' >> $HOME/.hermes/cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'pipeline-watch auto' ; echo "$CRON_LINE" ) | crontab -
echo "Installed: $CRON_LINE"
```

- [ ] **Step 2: Make it executable + commit**

```bash
chmod +x hermes-pipeline/scripts/install-cron.sh
git add hermes-pipeline/scripts/install-cron.sh
git commit -m "feat(cli): install-cron.sh helper (idempotent crontab)"
```

### TF.5: Documentation updates

**Files:**
- Modify: `docs/pipeline-modularization-plan.md` (add pointer to new plan)
- Modify: `README.md`

- [ ] **Step 1: Append a footnote to `docs/pipeline-modularization-plan.md`**

```markdown

---

> **Update (2026-06-11):** This plan has been superseded for selection,
> versioning, kanban, concurrency, and merge-gating by the design at
> `docs/gstack/hyonchoi-main-design-20260610-195349.md` and the implementation
> plan at `docs/superpowers/plans/2026-06-11-todo-pipeline-orchestrator.md`.
> Part 1's package layout is unchanged; Part 2 (`todos-manager`) is replaced
> by the v2.0.0 skill spec in the design doc.
```

- [ ] **Step 2: Refresh `README.md`**

```markdown
# todo-pipeline-orchestrator

A uv-managed Python package (`hermes_pipeline`) + Claude Code skill
(`todos-manager`) that turn per-project `TODOS.md` into an autonomous work
queue. See `docs/gstack/hyonchoi-main-design-20260610-195349.md` for design and
`docs/superpowers/plans/2026-06-11-todo-pipeline-orchestrator.md` for the
implementation plan.

## Install (dev)

    uv sync
    uv pip install -e ./hermes-pipeline

## Run

    pipeline-watch --help
    pipeline-watch auto              # one tick
    pipeline-watch status            # list pending reviews
    pipeline-watch merge <p> <id>    # Phase 9, gated on typed confirm

## Cron

    bash hermes-pipeline/scripts/install-cron.sh
```

- [ ] **Step 3: Commit**

```bash
git add docs/pipeline-modularization-plan.md README.md
git commit -m "docs: point modularization plan + README at the new design/plan"
```

### TF.6: Full-suite verification before merge

**Files:** — (none — verification step)

- [ ] **Step 1: Run all tests**

Run: `uv run pytest hermes-pipeline/tests -v`
Expected: all green; no skipped tests except those explicitly marked as `[→E2E]` integration tests requiring a live `hermes` binary.

- [ ] **Step 2: Confirm `pipeline-watch --help` works end-to-end**

Run: `uv run pipeline-watch --help`
Expected: top-level help shows `auto`, `merge`, `status` with descriptions and examples.

- [ ] **Step 3: Use superpowers:finishing-a-development-branch to wrap up**

Per the skill's workflow: confirm the branch is in a shippable state, open a PR, and HALT for review (do not merge — this is exactly the Phase 8 / Phase 9 split this plan implements).

---

## Self-Review

### Spec coverage

Cross-checked against each design-doc section and Implementation Task T1-T14:

| Design / Task | Covered by |
|---|---|
| Selection policy (Approach A) | TB.1-TB.5 |
| Versioning (`feat/{base_version}-{slug}[-attemptN]`) | TE.1 |
| Kanban (one card per project, NullKanban default, Hermes mapping table) | TC.1-TC.5 |
| Concurrency (per-project lock held selection→merge) | TD.1 (`State.lock`/`unlock`) used by TE.2 + TD.3 |
| Merge gating (Phase 8 halts; Phase 9 explicit) | TE.2 (ready_for_review write) + TD.3 (`run_phase9`) |
| Stable `TODO-<n>` IDs + counter | TA.1-TA.6 |
| First-run bootstrap | TA.3 |
| Field sanitization | TA.4 |
| Error & Rescue map + T8 messaging | TA.5 |
| T1 — outbox collapse, create-preserving | TC.3 |
| T2 — `merge_status: failed` + `error` | TD.2 |
| T3 — cycle-notify dedup with clear-on-resolve | TB.4 |
| T4 — per-project parse isolation | TB.5 + TF.1 |
| T5/T14 — `-attemptN` scan+max+1 | TE.1 |
| T6 — subcommand CLI shape | TF.3 |
| T7 — `clear_active_task(outcome=...)` | TC.1, TC.4 |
| T8 — path-named error messages with remediation | TA.5 (skill side) + raised messages in `merge.py` TD.3, `state.py` TD.2 |
| T9 — typed-TODO-id e2e confirm | TD.3 (`typed_confirm_default`) |
| T10 — todos-manager preview gate at step 7.5 | TA.4 |
| T11 — `tick_id` correlation | T0.5, TF.1 |
| T12 — file+stderr logging, rotated, 7-day | T0.5 |
| T13 — populated `--help` per subcommand | TF.3 |
| Phase config (phases.yaml) | T0.3 |
| Cron registration | TF.4 |
| Doc updates | TF.5 |

Open Question §"semver bump mapping" is left as a deferred TODO in `merge.py::default_bump` (matches the design doc explicitly leaving the mapping TBD).

Open Question §"Next iteration dependency" stays as `unblocks_something` boolean per Approach A; revisit only if XL starvation appears (design doc's stated trigger).

### Placeholder scan

No "TBD" / "implement later" / "add appropriate error handling" / generic placeholders. The single explicit TODO is in `merge.default_bump`, which matches an open question the design doc kept open *by name* — that's a deferral, not a placeholder.

### Type consistency

- `Todo`, `Phase`, `ReadyForReview`, `SyncResult`, `KanbanOutcome`, `PhaseStatus`, `MergeStatus` are defined once and used consistently downstream.
- `KanbanClient` Protocol signatures match `NullKanbanAdapter` and `HermesKanbanAdapter` implementations (`set_active_task`, `update_phase`, `clear_active_task` — all with the same keyword-only signatures throughout including TF.3 `cli.py`).
- `State` method names (`is_locked`, `lock`, `unlock`, `mark_phase_done`, `last_completed_phase_index`, `write_ready_for_review`, `read_ready_for_review`, `set_merge_status`, `list_ready_for_review_pending`) are referenced under the same names everywhere they appear.
- `decide_branch_name` parameters (`is_new_attempt`, `prior_attempt_branch_existed`) are referenced under the same names in tests.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-11-todo-pipeline-orchestrator.md`. Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task (or per lane), review between tasks, fast iteration. Best fit given the six independent lanes (A-F) — Lanes A, B, C, D, E can each be a parallel subagent dispatch after Lane 0 settles.

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch with checkpoints for review.

Which approach?
