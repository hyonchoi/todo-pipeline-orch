# How to run the Mock Integration Test Harness

Exercise the real pipeline end-to-end against mock project data and get a structured
findings report. The harness bootstraps a temporary git project, runs the 9-phase
pipeline through `PipelineRunner`, monitors each phase, and produces `report.json`.

## Prerequisites

- `uv sync` has run at the repo root (installs `pytest`, `pytest-cov`, `pytest-mock`)
- `git` on PATH
- `hermes` CLI installed and authenticated (`hermes login`)
- `claude` CLI installed and authenticated
- Hermes and Claude Code must be on PATH — the preflight check verifies all three

## Steps

### 1. Run the full pipeline (happy-path fixture)

```bash
uv run hermes-pipeline test --fixture happy-path --timeout 120
```

This creates a temporary project with a single TODO entry, runs all 9 phases (minus the deleted plan-gate), and
cleans up. Expect ~30 seconds with `claude-haiku-4-5` pinned in the fixture config.

Output:
```
2026-07-15 18:40:23,101 INFO hermes_pipeline.runner Running phase 1/9: Phase 2: Autoplan (key=phase_2_autoplan)
2026-07-15 18:40:26,916 INFO hermes_pipeline.runner Running phase 2/9: Phase 3: Writing Plan (key=phase_3_writing_plan)
...
2026-07-15 18:40:43,782 INFO hermes_pipeline.runner All phases completed successfully; moving to ready_for_review
```

Exit code 0 = all phases passed. Exit code 1 = one or more phases failed or the run timed out. Exit code 2 = preflight or setup error (missing dependency, `--kanban hermes` tenant unreachable).

### 2. Run a single phase

```bash
uv run hermes-pipeline test --fixture happy-path --phase phase_2_autoplan
```

Runs only the named phase in isolation. Useful for debugging a phase dispatch or
checking that a specific step works with a real Hermes subprocess call.

### 3. Run with a custom convergence threshold

```bash
uv run hermes-pipeline test --fixture happy-path --convergence-threshold 2
```

The convergence detector halts the run if N consecutive phases fail with the same
error class. The default is 3. Lowering it catches cascading failures faster;
raising it lets more phases run before the circuit breaker trips.

Error classes: `dependency_error`, `hermes_error`, `claude_error`, `timeout`, `phase_failure`.

### 4. Run with --keep to inspect temp artifacts

```bash
uv run hermes-pipeline test --fixture happy-path --keep --timeout 120

# Find the temp directory the harness left behind
find /tmp -maxdepth 1 -name 'harness-*' -type d
```

The temp directory contains:
```
harness-xxxxxxxx/
  TODOS.md              # Mock TODO entries
  README.md
  .hermes/
    config.toml          # Pinned to claude-haiku-4-5
    todo_id_counter
    locks/
    pipeline_checkpoints/
    ready_for_review/
  events.jsonl           # Per-phase event log (raw)
  reports/
    report.json          # Structured findings report
```

### 5. Run in loop mode to diff reports across iterations

```bash
# First run
uv run hermes-pipeline test --fixture happy-path --loop --keep --timeout 120

# Second run (auto-diffs against first run report)
uv run hermes-pipeline test --fixture happy-path --loop --keep --timeout 120
```

The `--loop` flag persists numbered report files (`happy-path-report.1.json`,
`happy-path-report.2.json`, ...) in the temp directory parent and diffs them
after each run. Requires `--keep` so the temp directory survives between runs.

### 6. Run the pytest test suite

The harness includes unit and e2e tests that mock `phases.run` to avoid real API calls:

```bash
# Full test suite
uv run pytest tests/test_harness.py tests/test_harness_e2e.py tests/test_report.py tests/test_runner.py tests/test_cli.py tests/test_cli_entrypoint.py -v

# E2e only (full orchestration, mocked phases)
uv run pytest tests/test_harness_e2e.py -v

# Unit tests only (convergence, monitor, dispatch, isolation)
uv run pytest tests/test_harness.py -v
```

## Verification

A successful full run:
- Exit code 0
- Log lines showing all 9 phases executed in order
- Gates (`phase_2b_plan_gate`, `phase_9_ship`) auto-approved in `continue_on_failure` mode
- Final line: "All phases completed successfully; moving to ready_for_review"

A run that hit convergence halt:
- Exit code 1
- Log line: "convergence detector: N+ consecutive [error_class] failures, halting run"
- Partial `report.json` in temp directory (check with `--keep`)

A run that hit the overall timeout:
- Exit code 1
- The in-flight phase is killed via `killpg`
- `phase_timed_out` event written to `events.jsonl`

## Troubleshooting

**`Missing dependency: git/hermes/claude`**
Preflight check failed. Install the missing tool and ensure it is on PATH. Run
`which git`, `which hermes`, `which claude` to verify.

**`Unknown fixture: my-fixture`**
Only `happy-path` is currently implemented. To add new fixtures, edit
`_get_todos_for_fixture()` and `_get_todo_id_for_fixture()` in `harness.py`.

**Exit code 1 but no clear failure message**
The CLI handler (`_cmd_test`) returns exit codes but doesn't print the report
summary to stdout. Re-run with `--keep`, then read the report:
```bash
HARNESS_DIR=$(find /tmp -maxdepth 1 -name 'harness-*' -type d | head -1)
cat "$HARNESS_DIR/reports/report.json" | python -m json.tool
```

**`HermesCallError` / `ClaudeCallError` from a phase**
The harness invokes real `hermes chat -q` subprocesses (not stubs). Verify:
```bash
hermes chat -q "echo hello"
claude --version
```

**Stale subprocesses after --timeout kill**
`_kill_hung_phase_subprocess()` uses `killpg` to clean up the session group of
the in-flight phase subprocess. If the PID has already exited or been reused, the
LSP-safe `os.kill(pid, 0)` check prevents hitting an unrelated process.

## Architecture Overview

The harness has four modules:

| Module | Purpose |
|--------|---------|
| `harness.py` | Fixture factory, preflight, convergence detector, monitor wrapper, `run_harness()` orchestrator |
| `runner.py` | `PipelineRunner` — phase loop, kanban wiring, checkpoint tracking, gate auto-approval |
| `test_report.py` | JSONL event log → `report.json` + summary + diff |
| `cli.py` | `test` subcommand — argparse wiring, exit code dispatch |

Key flow:
1. `preflight_check()` verifies git/hermes/claude on PATH
2. `create_mock_project()` initializes a temp git repo with TODOS.md + .hermes config
3. `isolate_config()` sets `PIPELINE_STATE_DIR` / `PIPELINE_LOCK_DIR` env vars
4. `PipelineRunner.run()` loops through phases, calling `_dispatch_phase()` which
   invokes the real `phases.run()` entrypoint
5. Gates are auto-approved when `continue_on_failure=True`
6. `_ConvergenceMonitor` wraps the event callback, feeds the `ConvergenceDetector`,
   and raises `ConvergenceHaltError` if the threshold is reached
7. `generate_report()` transforms `events.jsonl` into `report.json`
8. Temp directory is cleaned up unless `--keep` is set

The entire run is threaded with a `--timeout` watchdog. If the worker thread is
still alive after the timeout, the subprocess is killed via `killpg` and a
`phase_timed_out` event is recorded before report generation.

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

**Output.** If the run reaches report generation (preflight succeeded), a `--kanban hermes` run prints:

```
[kanban] tenant=mock-project tick_id=01ARZ3ND... task_id=abc123 report=/tmp/harness-.../reports/report.json keep=no (temp dir will be removed)
```

Pass `--keep` to retain the temp directory (which may include `kanban_outbox.jsonl` and
`active_tasks.json` if a task was created or enqueued) for post-run inspection.

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
