# CLI Reference

Complete reference for `pipeline-watch` and `hermes-pipeline` subcommands.

- `uv run pipeline-watch <command> [args]` — Production pipeline orchestration (tick, merge, approve, ...)
- `uv run hermes-pipeline test [args]` — Mock integration test harness

## pipeline-watch Global Flags

| Flag | Description |
|------|-------------|
| `--version` | Print version and exit |
| `--verbose` | Increased log detail: selection results, lock state, agent call summaries |
| `--debug` | Full debug logging: circuit breaker transitions, subprocess output |

Global flags apply before the subcommand: `uv run pipeline-watch --verbose tick`.

## Subcommands

### `tick`

Run one pipeline tick: discover active projects, select TODOs, register kanban phases.

```bash
uv run pipeline-watch tick              # scan all active projects
uv run pipeline-watch tick myproject    # tick one project
```

**Flow per project:**
1. Load pipeline contract from `.hermes/pipeline.toml`
2. Check prior tick outcomes; observe circuit breaker
3. Detect ready-to-ship or plan-gate TODOs; alert via Slack
4. Run Hermes agent selection on TODOS.md
5. Register kanban phases with `--parent` dependency chains

Without a project argument, scans all subdirectories of `PIPELINE_PROJECTS_DIR` for `TODOS.md` files. Per-project locks isolate failures — one project's held lock does not block others. Scan order rotates each tick for fairness.

---

### `approve`

Ship a ready TODO: bump version in PR, squash-merge to main, complete the ship gate.

```bash
uv run pipeline-watch approve myproject --todo TODO-5
uv run pipeline-watch approve myproject --todo TODO-5 --force --force
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--todo` | Yes | TODO to ship (e.g. `TODO-5` or `5`) |
| `--force` | No | Pass twice to bypass ONLY the SHA-staleness guard (audited) |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | Shipped successfully |
| 2 | Unexpected subprocess error |
| 3 | Refused by a guard (dirty tree, SHA mismatch, no ship sidecar) |

**Guards:**
- Dirty working tree: always refuses, cannot be bypassed
- PR head SHA changed since review: refuses unless `--force --force` (logged to `approve_audit.log`)
- CI not green: refuses; re-run once checks pass (bump commit already pushed)
- No ship sidecar or gate task: refuses

---

### `recover-counter`

Scan TODOS.md and initialize `.hermes/todo_id_counter` by finding the highest TODO-N.

```bash
uv run pipeline-watch recover-counter myproject
```

Useful when bootstrapping a project with hand-written TODOs but no counter file.

---

### `init`

Write the default pipeline execution contract (`.hermes/pipeline.toml`) for a project.

```bash
uv run pipeline-watch init myproject
uv run pipeline-watch init myproject --force
uv run pipeline-watch init myproject --assignee pipeline
uv run pipeline-watch init myproject --profile agent-skills
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--force` | No | Overwrite an existing contract |
| `--assignee` | No | Set the assignee field (e.g. `--assignee pipeline`) |
| `--profile` | No | Pipeline skill-set profile (`gstack` or `agent-skills`). Default: `gstack`. Determines which `phases.yaml` (and required capabilities) the contract is written against — see [Use the agent-skills profile](howto-agent-skills-profile.md). |

Capabilities are computed from `phases.yaml` at write time, not hardcoded.

---

### `doctor`

Verify a project's pipeline execution contract against `phases.yaml`.

```bash
uv run pipeline-watch doctor myproject
```

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | Clean: schema version, assignee, capabilities all match |
| 1 | Drift: contract missing capabilities required by phases.yaml |
| 2 | Missing/invalid contract, unknown project, or missing profile |

If the contract assignee is non-default (e.g. `pipeline`), verifies the Hermes profile exists.

---

### `install-profile`

Install the bundled pipeline Hermes profile for unattended kanban execution.

```bash
uv run pipeline-watch install-profile
uv run pipeline-watch install-profile --force
```

Creates a `pipeline` profile cloned from the active Hermes profile, then overlays the bundled `SOUL.md`. With `--force`, deletes an existing `pipeline` profile first.

After install, set the assignee: `uv run pipeline-watch init myproject --assignee pipeline`.

---

## hermes-pipeline test

Run the mock integration test harness: bootstraps a temporary git project, executes
pipeline phases, and generates a structured findings report. Runs against the real
`hermes kanban` adapter (tenant `mock-project`) — the `--kanban null` no-network mode
was removed along with `runner.py`/`watcher.py` in v0.5.6; the harness now always
requires `hermes login` and access to the `mock-project` tenant.

```bash
uv run hermes-pipeline test --fixture happy-path
uv run hermes-pipeline test --fixture happy-path --phase phase_2_autoplan
uv run hermes-pipeline test --fixture happy-path --convergence-threshold 2
```

**Arguments:**
| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `--fixture` | Yes | — | Fixture name. Only `happy-path` is implemented. |
| `--phase` | No | — | Run only the named phase in isolation (e.g. `phase_2_autoplan`). |
| `--timeout` | No | `3600` | Overall run timeout in seconds. Kills in-flight phase via `killpg` if exceeded. |
| `--convergence-threshold` | No | `3` | Consecutive same-class phase failures before circuit breaker halts the run. |
| `--keep` | No | — | Preserve the temporary directory after the run for inspection. |
| `--loop` | No | — | Persist numbered report files and diff them across runs. Requires `--keep`. |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | All phases passed |
| 1 | Phase failure, convergence halt, or timeout |
| 2 | Preflight or setup error (missing dependency, `mock-project` tenant unreachable) |

**Kanban preflight behavior:**
- Runs a preflight check (`hermes kanban list --tenant mock-project`) before phase execution. Timeouts after 15 s.
- Creates a kanban card in the fixture's `mock-project` tenant (never suffixed with tick ID).
- Card body includes `tick_id`, `fixture_name`, and `state_dir` metadata for debug tracing.
- On convergence halt, clears the active task with `outcome="abandoned"`.
- Prints a `[kanban]` summary line after report generation:
  ```
  [kanban] tenant=<tenant> tick_id=<id> task_id=<id or none> report=<path> keep=<yes|no>
  ```

**`KanbanPreflightError`** — `RuntimeError` subclass raised when the kanban preflight fails. Two triggers:
- `subprocess.TimeoutExpired` after 15 s → actionable timeout message
- Non-zero exit from `hermes kanban list --tenant <tenant>` → authentication/tenant access failure

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPELINE_PROJECTS_DIR` | `~/projects` | Directory to scan for project `TODOS.md` files |
| `PIPELINE_STATE_DIR` | `~/.hermes` | Global state directory |
| `PIPELINE_LOCK_DIR` | `~/.hermes/pipeline_locks` | Directory for merge operation locks |
| `PIPELINE_SLACK_CHANNEL` | `#alert` | Default Slack channel (overridden by per-project config) |
| `PIPELINE_CLAUDE_CMD` | `claude` | Claude Code command (deprecated in v0.3) |
| `PIPELINE_KANBAN_ADAPTER` | `null` | Kanban adapter: `hermes` or `null` |

## See Also

- [Getting-started tutorial](tutorial-getting-started.md) — End-to-end walkthrough
- [How to approve and ship a TODO](howto-approve-and-ship.md) — The full ship workflow
- [How to debug ticks and recover counters](howto-debugging-and-recovery.md) — Using `--verbose`, `--debug`, `recover-counter`
- [Circuit breaker explanation](explanation-circuit-breaker.md) — How no-progress tracking works
- [Pipeline contract explanation](explanation-pipeline-contract.md) — Why versioned contracts exist
