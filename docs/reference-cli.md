# pipeline-watch CLI Reference

Complete reference for all `pipeline-watch` subcommands. Run every command via `uv run pipeline-watch <command> [args]`.

## Global Flags

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

### `status`

Display pending ready-for-review records as a table.

```bash
uv run pipeline-watch status
```

Shows TODOs that passed all pipeline phases and are waiting for Phase 9 (ship gate).

---

### `merge`

Execute Phase 9: merge a ready TODO to main.

```bash
uv run pipeline-watch merge myproject 5
uv run pipeline-watch merge myproject 5 --abandon
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `todo_id` | Yes | TODO number (integer, e.g. `5`, not `TODO-5`) |
| `--abandon` | No | Skip confirmation; reject the merge |

**Exit codes:** 0 success, 2 error.

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

### `approve-plan`

Approve or reject a plan-gate decision sheet for a TODO (unblocks phase_2b_plan_gate).

```bash
uv run pipeline-watch approve-plan myproject --todo TODO-5 --approve
uv run pipeline-watch approve-plan myproject --todo TODO-5 --reject --reason "..."
uv run pipeline-watch approve-plan myproject --todo TODO-5 --approve --override q1=B
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--todo` | Yes | TODO whose plan to approve/reject |
| `--approve` | One of | Approve the plan |
| `--reject` | One of | Reject the plan (requires `--reason`) |
| `--override` | No | Override a recommendation (repeatable, e.g. `--override q1=B`). Only valid with `--approve` |
| `--reason` | No | Rejection reason (required with `--reject`) |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | Plan approved or rejected successfully |
| 2 | Unexpected error |
| 3 | Refused by a guard (e.g. `--reject` without `--reason`) |

---

### `kill`

Kill in-flight phase(s): SIGTERMs the recorded subprocess, writes `killed_by_operator` outcome sidecar, and releases the tick lock if held by the killed tick.

```bash
uv run pipeline-watch kill --all                    # all projects, all phases
uv run pipeline-watch kill --all myproject          # one project, all phases
uv run pipeline-watch kill --todo TODO-5            # all projects, specific TODO
uv run pipeline-watch kill --todo TODO-5 myproject  # one project, specific TODO
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `--all` | One of | Kill all in-flight phases |
| `--todo` | One of | Kill a specific TODO (e.g. `TODO-1`) |
| `project` | No | Project slug (omit to scan all projects) |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | All kills confirmed |
| 1 | Some kills unconfirmed (marker left in place) |
| 2 | TODO not found or invalid arguments |

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
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--force` | No | Overwrite an existing contract |
| `--assignee` | No | Set the assignee field (e.g. `--assignee pipeline`) |

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
- [How to kill a stuck phase](howto-kill-stuck-phase.md) — `kill` subcommand deep dive
- [Circuit breaker explanation](explanation-circuit-breaker.md) — How no-progress tracking works
- [Pipeline contract explanation](explanation-pipeline-contract.md) — Why versioned contracts exist
