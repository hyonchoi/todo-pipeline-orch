# How to run the Mock Integration Test Harness

Exercise the real pipeline end-to-end against mock project data and get a structured
findings report. The harness bootstraps a temporary git project, registers pipeline
phases on a real Hermes kanban board, polls for phase transitions, and produces
`report.json`.

## Prerequisites

- `uv sync` has run at the repo root (installs `pytest`, `pytest-cov`, `pytest-mock`)
- `git` on PATH
- `hermes` CLI installed and authenticated (`hermes login`)
- The configured prompt client (`claude` or `codex`) installed and authenticated
- Hermes and the selected client must be on PATH — preflight does not require
  the unselected client

## Steps

### 1. Run the full pipeline (happy-path fixture)

```bash
uv run tpo test --fixture happy-path --timeout 120
```

This creates a temporary project with a single TODO entry, registers all pipeline
phases on the kanban board, and polls until completion. Expect ~30 seconds with
`claude-haiku-4-5` pinned in the fixture config.

The fixture intentionally has no Git remote. Its explicit harness phase profile
keeps every production phase key but replaces `phase_8_finish_branch` with a
local terminal workflow: test, commit, record the branch, and verify a clean
worktree without invoking `/ship`, pushing, or opening a PR.

Output:
```
INFO registering kanban phases for TODO-1 tick 01ARZ3... (assignee=pipeline)
INFO initial phase status: phase_1 kickoff=running, phase_2_autoplan=blocked, ...
INFO phase phase_1 kickoff: none -> running
INFO phase phase_1 kickoff: running -> done
INFO phase phase_2_autoplan: blocked -> running
INFO phase phase_2_autoplan: running -> done
...
[kanban] tenant=mock-project tick_id=01ARZ3... phases={...} report=~/.hermes/tmp/harness-.../artifacts/reports/report.json keep=no (temp dir will be removed)
```

Exit code 0 = all phases passed. Exit code 1 = one or more phases failed or the run timed out. Exit code 2 = preflight or setup error (missing dependency, `mock-project` tenant unreachable).

### 2. Run a single phase

```bash
uv run tpo test --fixture happy-path --phase phase_2_autoplan
```

Runs only the named phase in isolation. Useful for debugging a phase dispatch or
checking that a specific step works with a real Hermes subprocess call.

### 3. Run with a custom convergence threshold

```bash
uv run tpo test --fixture happy-path --convergence-threshold 2
```

The convergence detector halts the run if N consecutive phases fail with the same
error class. The default is 3. Lowering it catches cascading failures faster;
raising it lets more phases run before the circuit breaker trips.

Error classes: `dependency_error`, `hermes_error`, `claude_error`, `timeout`, `phase_failure`.

### 4. Run with --keep to inspect temp artifacts

```bash
uv run tpo test --fixture happy-path --keep --timeout 120

# Find the retained harness workspace
find ~/.hermes/tmp -maxdepth 1 -name 'harness-*' -type d
```

The temp directory contains:
```
harness-xxxxxxxx/
  project/
    .git/
    TODOS.md
    README.md
    .hermes/
      pipeline.toml
  artifacts/
    events.jsonl         # Per-phase event log (raw)
    reports/
      report.json        # Structured findings report
      report.md          # Human-readable findings report
```

### 5. Run in loop mode to retain a numbered snapshot

```bash
uv run tpo test --fixture happy-path --loop --keep --timeout 120
```

The `--loop` flag writes a numbered snapshot such as
`artifacts/happy-path-report.1.json` beside the reports directory. Each CLI
invocation creates a fresh harness workspace, so snapshots do not carry across
separate invocations and cross-invocation auto-diff is not currently available.
Requires `--keep` so the workspace and its snapshot survive the run.

### 6. Run the pytest test suite

The harness includes unit and e2e tests that mock kanban to avoid real API calls:

```bash
# Full test suite
uv run pytest -v

# Harness tests (convergence, monitor, phase polling, isolation)
uv run pytest tests/test_harness.py tests/test_harness_e2e.py -v

# Report generation
uv run pytest tests/test_report.py -v
```

## Verification

A successful full run:
- Exit code 0
- Log lines showing phase transitions: `phase phase_X_key: blocked -> running`, `phase phase_X_key: running -> done`
- Final `[kanban]` summary line with `phases={...}` status map
- `artifacts/reports/report.json` in the retained workspace (use `--keep` to preserve)

A run that hit convergence halt:
- Exit code 1
- Log line: "convergence detector: N+ consecutive phase_failure, halting"
- Partial `artifacts/reports/report.json` in the retained workspace (check with `--keep`)

A run that hit the overall timeout:
- Exit code 1
- The poll wait is cooperatively cancelled and its thread is joined
- Running Hermes tasks are reclaimed, their run metadata must confirm worker
  termination, and the task chain is archived child-first
- `phase_timed_out` event written to `artifacts/events.jsonl`
- Report generation and workspace deletion occur only after termination is
  confirmed. Otherwise the command fails closed and reports the retained
  workspace path.

## Troubleshooting

**`Missing dependency: git/hermes/<selected client>`**
Preflight check failed. Install the missing tool and ensure it is on PATH. Run
`which git`, `which hermes`, and either `which claude` or `which codex` to
verify the configured `prompt_client`.

**`Unknown fixture: my-fixture`**
Only `happy-path` is currently implemented. To add new fixtures, edit
`_get_todos_for_fixture()` and `_get_todo_id_for_fixture()` in `harness.py`.

**Exit code 1 but no clear failure message**
The CLI handler (`_cmd_test`) returns exit codes but doesn't print the report
summary to stdout. Re-run with `--keep`, then read the report:
```bash
HARNESS_DIR=$(find ~/.hermes/tmp -maxdepth 1 -name 'harness-*' -type d | head -1)
python -m json.tool \
  "$HARNESS_DIR/artifacts/reports/report.json"
```

**`HermesCallError` / `ClaudeCallError` from a phase**
The harness invokes real `hermes chat -q` subprocesses (not stubs). Verify:
```bash
hermes chat -q "echo hello"
claude --version
```

**Workspace retained after `--timeout`**
The harness could not prove that every Hermes worker and run stopped. Inspect
the retained path from the error, then use `hermes kanban show <task> --json`
and `hermes kanban runs <task> --json` before removing it.

## Architecture Overview

The harness is a single-module system: `harness.py`. It registers phases on a real
kanban board and polls for transitions rather than dispatching phases directly.

| Module | Purpose |
|--------|---------|
| `harness.py` | Fixture factory, preflight, convergence detector, phase registration, poll loop, `run_harness()` orchestrator |
| `report.py` | JSONL event log → `report.json` + summary + diff |
| `cli.py` | `test` subcommand — argparse wiring, exit code dispatch |

Key flow:
1. `preflight_check(prompt_client=...)` verifies git, Hermes, and the selected
   Claude/Codex executable on PATH
2. `create_mock_project()` initializes a temp git repo with TODOS.md + .hermes config
3. `isolate_config()` writes a temporary config file and points `TPO_CONFIG_FILE` at it
4. `register_todo_phases()` creates kanban tasks for all pipeline phases
5. Initial phase status is printed via `log.info()` to console
6. Poll loop calls `get_todo_kanban_status()` on each interval, logging phase transitions
7. Gate tasks are auto-completed when their parent phase finishes (`_auto_complete_gate_tasks`)
8. `_ConvergenceMonitor` wraps the event callback, feeds the `ConvergenceDetector`,
   and raises `ConvergenceHaltError` if the threshold is reached
9. `observe_outcomes()` writes the final state after all phases reach terminal status
10. `generate_report()` transforms `artifacts/events.jsonl` into
    `artifacts/reports/report.json` and `artifacts/reports/report.md`
11. Temp directory is cleaned up unless `--keep` is set

The entire run is threaded with a `--timeout` watchdog (default 86400s / 24h).
At timeout, a cancellation event interrupts the poll wait and the thread is
joined. Hermes running tasks are reclaimed and checked for ended runs and
confirmed worker termination before the chain is archived. Only then is
`phase_timed_out` reported and the report generated. Unconfirmed cleanup
retains the workspace and raises an error instead of deleting live state.

## Kanban Integration

The harness always uses the real `HermesKanbanAdapter` — the `--kanban null` no-network
mode was removed in v0.5.6. Every run creates a kanban card in the `mock-project` tenant.

**Precondition:** you must be logged in (`hermes login`) with access to the `mock-project`
tenant. The harness runs a preflight check (`hermes kanban list --tenant mock-project`) before
starting any phase and fails fast with an actionable error if this doesn't succeed — it will not
silently exit 0 with no card and no local evidence.

**Tenant is never suffixed.** Every run creates a card in the same
`mock-project` tenant; runs are distinguished by a `tick_id` recorded in each card's body, not by
a separate tenant per run.

**Phase transition logging** — the poll loop logs each phase state change to console via
`log.info()`, so you can follow progress without tailing `artifacts/events.jsonl`:

```
INFO phase phase_1 kickoff: none -> running
INFO phase phase_1 kickoff: running -> done
INFO phase phase_2_autoplan: blocked -> running
INFO phase phase_2_autoplan: running -> done
```

Fast phases (ready/None → done between polls) are also logged.

**Terminal-state table** — what the board looks like after a run ends:

| Terminal state | Board state |
|---|---|
| Success (ready for review) | Card **live** — not archived; a later `merge`/`abandon` step clears it |
| Phase failure | Card **archived** — inspectable, not deleted |
| Convergence-halt (3+ consecutive same-class failures) | Card **archived** |
| Overall `--timeout` fires | Card **live** — genuinely orphaned; intentional debug signal |
| Process crash | Card **live** — genuinely orphaned; intentional debug signal |

**Output.** After report generation, the harness prints:

```
[kanban] tenant=mock-project tick_id=01ARZ3ND... phases={phase_1 kickoff: done, phase_2_autoplan: done, ...} report=~/.hermes/tmp/harness-.../artifacts/reports/report.json keep=no (temp dir will be removed)
```

Pass `--keep` to retain the temp directory for post-run inspection.

**Known limitation:** the outbox retry path (`drain_outbox`) does not currently carry the
`tick_id`/fixture metadata on a queued-and-later-retried card — only the initial synchronous
create attempt includes it. This is a pre-existing outbox-fidelity gap, not introduced by this
feature.

## Related

- [Explanation: Skill Test Harness Design](explanation-skill-test-harness-design.md) — Design rationale, phase 2 plans
- [Reference: Skill Test Harness API](reference-skill-test-harness.md) — Complete function signatures
- [How to: Skill Test Environment](howto-skill-test-environment.md) — Unit tests for TODOS.md skill logic
- [How to: Eval Suite](howto-eval-suite.md) — Live API selection agent tests
- [Implementation Plan](superpowers/plans/2026-07-14-mock-integration-test-harness.md) — Full task breakdown
