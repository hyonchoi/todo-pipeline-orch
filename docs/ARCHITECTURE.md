# Architecture

todo-pipeline-orchestrator is a uv-managed Python package that automates the lifecycle of TODOS.md entries through a multi-phase pipeline driven by Hermes agents, kanban task dependencies, and circuit breaker protections.

## Overview

```
Tick Loop (Hermes cron or manual)
    |
    v
[Selection] -- Hermes agent picks a TODO from TODOS.md
    |
    v
[Kanban Registration] -- Create phases as kanban tasks with --parent chains
    |
    v
Phase 2: Autoplan --> Phase 3: Writing Plan --> Phase 4: Development
    |
    v
Phase 5: Code Review (gstack /review, v0.4+)
    |
    v
Phase 6.1: CSO Security Review --> Phase 6.2: QA
    |
    v
Phase 7: Document Release --> Phase 8: Finish Branch
```

## Lane Structure

The package is organized into lanes — loosely coupled subsystems with well-defined interfaces.

```
hermes_pipeline/
├── cli.py                    # CLI entry point (argparse subcommands)
├── config.py                 # Configuration loading (global config, env overrides, TOML overlay)
├── circuit.py                # Circuit breaker (no-progress tracking, Slack alerts)
├── counter.py                # TODO ID counter management
├── contract.py               # Pipeline execution contract (schema, load, validate, capabilities)
├── hermes_adapter.py         # Hermes CLI wrapper (replaces direct Anthropic SDK)
├── kanban.py                 # Kanban adapter (hermes kanban commands)
├── kanban_tasks.py           # Task registration with --parent chains and manual gates
├── ship.py                   # Legacy ship-gate helper for existing sidecars
├── outcomes.py               # Outcome sidecar writing/reading
├── phases.py                 # Phase definitions + hermes subprocess invocation
├── project_config.py         # Multi-project discovery & per-project config
```

### Lane A: Hermes-Agent Selection
`decision/` — LLM-driven TODO pick via `hermes chat -q`. SHA-pinned prompt, immutable decision records, outcome sidecars. The deterministic `selection.py` was retired in v0.2.

### Lane B: State Management
`state.py` — Locks, checkpoints, ready-for-review records, atomic tmp+rename writes. All state files are written atomically to prevent partial reads.

### Lane C: Kanban Integration
`kanban.py`, `kanban_tasks.py` — Executable phases as kanban tasks with `--parent` dependency chains; human gates as detached blocked tasks. Kanban status queries drive the tick loop.

### Lane D: Runner & Phases
`phases.py`, `tick.py` — Phase execution via Hermes subprocess. Atomic-mkdir tick lock prevents duplicate ticks. Phase 5 (code review) is a plain kanban-dispatched phase like any other — the code-owned pre/post review lifecycle (`review_phase.py`) was removed in v0.5.6 as dead code; the prompt instructs `/review` directly with no code-side snapshot/restore machinery.

### Lane E: Finish Branch
Phase 8 runs `/ship`, opens or updates a PR, pushes all intended branch changes, and completes normally without merging. The legacy `ship.py` helper remains for old ship-gate sidecars but is no longer part of the default gstack phase profile.

### Lane F: CLI, Multi-Project, State Migration
`cli.py`, `project_config.py`, `state_migration.py` — User-facing commands, multi-project scanning, per-project state migration. (`watcher.py` and `status.py` were removed in v0.5.6 — the `__main__.py` event loop and `cli.py` subcommands cover their roles.)

### Lane G: Hermes Adapter
`hermes_adapter.py` — Wraps `hermes chat -q` for all LLM calls. Replaces direct Anthropic SDK usage.

## Phase Execution Flow

Phase execution is fully kanban-dispatched — there is no in-process Python loop that invokes
Hermes per phase. `kanban_tasks.py`'s `register_todo_phases` builds one kanban task per phase
with a rendered prompt as the task body, chains executable phases with `--parent`, creates gate
phases as detached blocked tasks, and hands them off to `hermes kanban
create`; the actual Hermes agent run happens outside this codebase, dispatched by the kanban
system.

```
register_todo_phases(todo_id, ...)
    |
    +-- load_phases() -- read phases.yaml for the active profile
    |
    +-- for each phase:
    |       _render_phase_prompt(phase.prompt, todo_id, tick_id, project_slug)
    |       |
    |       +-- hermes kanban create --tenant <slug> [--parent <prev_task_id>] --body <rendered prompt>
    |
    +-- task_ids[] -- returned for --parent chaining of the next executable phase
```

Phase 5 (code review) is not special-cased in code — its prompt instructs the agent to run
`/review`, and the review lifecycle (pass/fail/revert) is entirely the agent's responsibility,
not tracked via code-owned pre/post snapshots (that machinery was removed in v0.5.6; see Lane D).

## Data Flow

### State Files
All pipeline state lives under `<project>/.hermes/`:

```
<project>/.hermes/
├── decisions/                 # Immutable selection decisions (write-once)
├── outcomes/                  # Phase completion/failure sidecars
├── ready_for_review/          # Legacy ship-gate sidecars
├── phase_started/             # In-flight phase markers
├── tick.lock/                 # Global tick lock (atomic mkdir)
├── todo_id_counter            # Compatibility cache for tracked TODO IDs
├── config.toml                # Per-project config overlay
├── project.toml               # Project marker (enabled/slack_channel)
├── pipeline.toml              # Pipeline execution contract (assignee, capabilities)
└── circuit.json               # Circuit breaker state
```

### Decision Immutability
`.hermes/decisions/<tick_id>.json` is written exactly once. Outcomes attach via sidecars; the decision file is never edited. Rejection sidecars (`.hermes/decisions/<tick_id>-rejected.json`) are written only on rejection.

### Outcome Types
| Status | Outcome Written |
|--------|----------------|
| `done` | `phase_complete` |
| `failed` | `failed_at_phase_<key>` |
| `archived` | `failed_at_phase_<key>` with `kanban_status: "archived"` |

## Circuit Breaker

- Tracks consecutive no-progress ticks (selection returns `picked=None`)
- After threshold (default: 3), fires Slack alert
- Alert dedup: one alert per `alert_dedup_hours` (default: 24)
- Gateway service manages tick scheduling and cron backoff

## Key Design Decisions

1. **Kanban as scheduler** — Executable phases are kanban tasks with `--parent` chains. Profiles may define detached blocked gates, but the default `gstack` profile ends at Phase 8 PR handoff.
2. **Atomic state writes** — All state files use tmp+rename to prevent partial reads.
3. **Code-owned review lifecycle removed (v0.5.6)** — Phase 5 was briefly code-owned (pre/post pytest, deterministic commit/restore) in v0.4+, but that machinery (`review_phase.py`) was dead code by v0.5.6 and was deleted. Phase 5 today is a plain kanban-dispatched phase: the prompt instructs `/review`, and the agent owns the review lifecycle end-to-end.
4. **Hermes as sole LLM surface** — All LLM traffic routes through `hermes chat -q`, not direct SDK calls.
5. **Multi-project scan** — Single global lock, per-project selection under one tick execution.

### TODOS Manager Skill (v2.1)

The `todos-manager` skill enforces the canonical TODOS.md schema and provides seven subcommands:
- `--init`: Initialize TODOS.md with `## Metadata`, `## Entry Schema`, and `## Entries`, then create TODOS-archive.md
- `--add`: Add new entry with schema enforcement and preview gate
- `--convert`: Convert existing TODOS.md to the canonical sectioned format and validate entries
- `--audit`: Audit TODOS.md for format compliance and reconcile invalid tracked ID metadata
- `--archive`: Move completed `[x]` entries to TODOS-archive.md
- `--list`: List active TODO entries (optional `--all` flag shows archived entries)
- `--revise`: Revise an existing entry — fill missing or weak fields with AI-pre-filled suggestions

The skill source lives at `hermes_pipeline/data/skills/todos-manager/SKILL.md` (platform-neutral, git-tracked) and is installed to user-level skill directories via `tpo skills install --target all`. The skill enforces:
- Required fields: **What:**, **Why:**, **Decisions:**
- Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**
- Stable TODO-<n> IDs: assigned `TODO-<n>` IDs are immutable once committed; `NEXT_TODO_ID` under `## Metadata` is the common-path source and advances after each successful add, while active and archived IDs are scanned only for reconciliation
- `TODOS.md` stores tracked ID state under `## Metadata` as `NEXT_TODO_ID: <n>`. The `## Entry Schema` section documents the format and is never parsed as active TODO content. Active entries live under `## Entries`.

The skill's deterministic logic (ID sequencing, entry parsing, format validation, archive logic) has a structural unit test suite at `tests/skill-test-environment/` — golden YAML assertions run against a demo-project fixture, zero token cost. This Phase 1 harness provides pure-Python implementations of skill rules that serve as the test oracle, enabling instant feedback without API tokens. See:
- [Reference: Skill Test Harness API](reference-skill-test-harness.md) — Complete function signatures, assertion types, fixtures
- [How To: Skill Test Environment](howto-skill-test-environment.md) — Add unit tests, golden files, debug failures
- [Explanation: Skill Test Harness Design](explanation-skill-test-harness-design.md) — Why pure-Python + golden files, Phase 1 vs. Phase 2 plans
- [tests/skill-test-environment/README.md](../tests/skill-test-environment/README.md) — Quick start

## See Also
- [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) — How kanban drives phase state
- [Pipeline State Machine](hermes-state-machine.md) — Full tick lifecycle transitions
- [Modularization Plan](pipeline-modularization-plan.md) — Design history and rationale
- [Multi-Project Scan](explanation-multi-project-scan.md) — Why single global lock, state migration decisions
