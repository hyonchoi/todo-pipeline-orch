# How to debug pipeline ticks and recover TODO counters

This guide covers the three tools you use when a tick doesn't behave the way you expect or when the TODO ID counter is missing.

- **`--verbose`** — add informational details (selection results, lock state) without noise
- **`--debug`** — surface internal state (agent call summaries, circuit breaker transitions, kanban payloads)
- **`recover-counter`** — initialize the TODO ID counter by scanning TODOS.md

## Prerequisites

- `pipeline-watch` installed and configured (see the [getting-started tutorial](tutorial-getting-started.md))
- `PIPELINE_PROJECTS_DIR` set to a directory containing projects with TODOS.md files

## Using `--verbose` for targeted log detail

The `--verbose` flag enables the `pipeline.verbose` logger, which outputs informational details at key points in the tick flow. Use this when you want to know what the pipeline is doing without the noise of full debug output.

```bash
uv run pipeline-watch --verbose tick my-project
```

**What you'll see (in addition to default INFO output):**

| Source | Message |
|--------|---------|
| Lock acquisition | `acquiring tick lock: lock_dir=<path> tick_id=<id>` |
| Selection result | `selection result: picked=TODO-N rationale=...` |
| Lock release | `tick lock released: tick_id=<id>` |

These messages come from the `pipeline.verbose` logger, which is off by default and only active when `--verbose` is passed.

**Common use:** Verify which TODO was selected and why, without seeing the full agent payload.

## Using `--debug` for full diagnostics

The `--debug` flag lowers the root log level from INFO to DEBUG, surfacing internal state at ~14 strategic points across the pipeline.

```bash
uv run pipeline-watch --debug tick my-project
```

**What you'll see (in addition to --verbose output):**

| Source | Message |
|--------|---------|
| Agent call | `agent prompt (truncated to 2000 chars): ...` |
| Agent call | `agent raw response (truncated to 2000 chars): ...` |
| Lock acquisition | `tick lock acquired: lock_dir=<path> holder_pid=<pid>` |
| Selection | `selection decision: picked=TODO-N candidates=... rationale=...` |
| Circuit breaker | `circuit breaker observe: picked=... counts_as_no_progress=... state=...` |
| Circuit breaker | `circuit breaker: sending slack alert after N consecutive no-progress ticks` |
| Circuit breaker | `circuit breaker: backed off to N min interval` |
| Circuit breaker | `circuit breaker: resuming from backoff (was backed_off=True)` |
| Kanban | `kanban registration payload (raw JSON, truncated): ...` |

**Important:** Debug output truncates at 2000 characters for agent prompts and responses, and at 500 characters for kanban payloads. This prevents a single tick from flooding the log.

**Common use:** Troubleshoot why a specific TODO was or wasn't selected, or why the circuit breaker tripped.

## Recovering the TODO ID counter

When you start a project with hand-written TODOs in TODOS.md but no `.hermes/todo_id_counter` file, the pipeline doesn't know what ID to assign next. The `recover-counter` subcommand scans TODOS.md for the highest TODO-N and initializes the counter.

```bash
uv run pipeline-watch recover-counter my-project
```

Output:
```
Counter set to 5 for project my-project
```

### How it works

1. Reads `TODOS.md` in the project directory and finds all TODO-N patterns using the regex `\bTODO-(\d+)\b`
2. Determines the maximum N (e.g., TODO-5 gives 5)
3. Writes `max(existing_counter, scanned_max)` to `.hermes/todo_id_counter`

### Key behaviors

- **Never decreases the counter.** If the counter file says 8 and TODOS.md has TODO-4, the counter stays at 8. This prevents ID resurrection when completed TODOs were removed.
- **Creates `.hermes/` if needed.** If the directory doesn't exist, it's created automatically.
- **Atomic write.** Uses a temp file + rename so a crash mid-write leaves a partial file (which the reader treats as 0) rather than a corrupted counter.
- **Corrupt counter recovery.** If the counter file contains non-integer text, it's treated as 0 and replaced with the scanned maximum.

### Error cases

- **TODOS.md missing:** Returns exit code 2 with `TODOS.md not found in <path>`
- **No TODO-N entries found:** Writes 0 to the counter (valid state — no TODOs yet)
- **Invalid project slug:** Returns exit code 2 (slug must be alphanumeric, dot, dash, underscore)

### Example

Before:
```
$ cat TODOS.md
# TODOS

- TODO-1: Set up project
- TODO-3: Implement feature A
- TODO-5: Add tests

$ cat .hermes/todo_id_counter
cat: .hermes/todo_id_counter: No such file or directory
```

After:
```
$ uv run pipeline-watch recover-counter my-project
Counter set to 5 for project my-project

$ cat .hermes/todo_id_counter
5
```

## Verification

After using any of these tools, verify the result:

- **`--verbose`/`--debug`:** Check the log output includes the expected detail level. Run `uv run pipeline-watch tick my-project` (no flag) and confirm no verbose or debug output appears.
- **`recover-counter`:** Check `.hermes/todo_id_counter` contains the expected value.

## Troubleshooting

**"verbose output not showing up"**

- Make sure you pass `--verbose` before the subcommand: `uv run pipeline-watch --verbose tick my-project`
- The flags are global root-level arguments — position matters for readability, though they work anywhere before the subcommand

**"debug output not showing up"**

- Same as `--verbose`: use `uv run pipeline-watch --debug tick my-project`
- Debug logging is only available in the `tick` subcommand (the only subcommand that logs at DEBUG level)

**"recover-counter returns error 2"**

- Check that `PIPELINE_PROJECTS_DIR/my-project/TODOS.md` exists
- Check that the project slug is valid (alphanumeric, dot, dash, underscore — no spaces or special characters)
