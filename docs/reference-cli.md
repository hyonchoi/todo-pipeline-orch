# CLI Reference

Complete reference for `tpo` subcommands.

- `uv run tpo <command> [args]` — Production pipeline orchestration (tick, merge, approve, ...)
- `uv run tpo test [args]` — Mock integration test harness

## tpo Global Flags

| Flag | Description |
|------|-------------|
| `--version` | Print version and exit |
| `--verbose` | Increased log detail: selection results, lock state, agent call summaries |
| `--debug` | Full debug logging: circuit breaker transitions, subprocess output |

Global flags apply before the subcommand: `uv run tpo --verbose tick`.

## Subcommands

### `tick`

Run one pipeline tick: discover active projects, select TODOs, register kanban phases.

```bash
uv run tpo tick              # scan all active projects
uv run tpo tick myproject    # tick one project
```

**Flow per project:**
1. Load pipeline contract from `.hermes/pipeline.toml`
2. Check prior tick outcomes; observe circuit breaker
3. Detect ready-to-ship or plan-gate TODOs; alert via Slack
4. Run Hermes agent selection on TODOS.md
5. Register executable kanban phases with `--parent` dependency chains and detached blocked gates

Without a project argument, scans all subdirectories of the global
`projects_dir` for `TODOS.md` files. Per-project locks isolate failures — one
project's held lock does not block others. Scan order rotates each tick for
fairness.

---

### `approve`

Ship a ready TODO: bump version in PR, squash-merge to main, complete the ship gate.

```bash
uv run tpo approve myproject --todo TODO-5
uv run tpo approve myproject --todo TODO-5 --force --force
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

Initialize `.hermes/todo_id_counter` from tracked `NEXT_TODO_ID` metadata, falling back to a TODOS.md plus TODOS-archive.md scan for legacy files.

```bash
uv run tpo recover-counter myproject
```

Useful when bootstrapping a project with hand-written TODOs but no counter file.

---

### `init`

Write the default pipeline execution contract (`.hermes/pipeline.toml`) for a project.

```bash
uv run tpo init myproject
uv run tpo init myproject --force
uv run tpo init myproject --assignee pipeline
uv run tpo init myproject --profile agent-skills
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
uv run tpo doctor myproject
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
uv run tpo install-profile
uv run tpo install-profile --force
```

Creates a `pipeline` profile cloned from the active Hermes profile, then overlays the bundled `SOUL.md`. With `--force`, deletes an existing `pipeline` profile first.

After install, set the assignee: `uv run tpo init myproject --assignee pipeline`.

---

### `skills`

Install or remove the bundled `todos-manager` skill.

```bash
uv run tpo skills install
uv run tpo skills install --target all --reinstall
uv run tpo skills uninstall --target codex --yes
```

**Install arguments:**
| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `--target` | No | `claude` | Install to `claude`, `codex`, or `all` skill directories |
| `--scope` | No | `user` | Install under the user home directory or the current project |
| `--reinstall` | No | false | Replace an existing installed copy after explicit review |

**Uninstall arguments:**
| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `--target` | No | `claude` | Remove from `claude`, `codex`, or `all` skill directories |
| `--scope` | No | `user` | Remove from the user home directory or the current project |
| `--yes` | Yes | false | Confirm removal without an interactive prompt |

Install refuses to overwrite an existing destination unless `--reinstall` is set. Install and uninstall preflight all selected targets before replacing or removing an installed skill. Reinstall rejects symlink destinations; uninstall removes the link itself without following its target. Both commands return nonzero if rollback or cleanup leaves a recoverable backup behind.

---

## tpo test

Run the mock integration test harness: bootstraps a temporary git project, executes
pipeline phases, and generates a structured findings report. Runs against the real
`hermes kanban` adapter (tenant `mock-project`) — the `--kanban null` no-network mode
was removed along with `runner.py`/`watcher.py` in v0.5.6; the harness now always
requires `hermes login` and access to the `mock-project` tenant.

```bash
uv run tpo test --fixture happy-path
uv run tpo test --fixture happy-path --phase phase_2_autoplan
uv run tpo test --fixture happy-path --convergence-threshold 2
```

**Arguments:**
| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `--fixture` | Yes | — | Fixture name. Only `happy-path` is implemented. |
| `--phase` | No | — | Run only the named phase in isolation (e.g. `phase_2_autoplan`). |
| `--timeout` | No | `86400` | Overall run timeout in seconds. Kills in-flight phase via `killpg` if exceeded. |
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
  [kanban] tenant=mock-project tick_id=01ARZ3... phases={phase_1 kickoff: done, ...} report=/tmp/harness-.../reports/report.json keep=no (temp dir will be removed)
  ```

**`KanbanPreflightError`** — `RuntimeError` subclass raised when the kanban preflight fails. Two triggers:
- `subprocess.TimeoutExpired` after 15 s → actionable timeout message
- Non-zero exit from `hermes kanban list --tenant <tenant>` → authentication/tenant access failure

---

## Environment Variables

Machine-level defaults live in the global config file. Create it with
`tpo config init`, then edit it directly or use `tpo config set <key> <value>`.
The generated file includes active defaults for `projects_dir`, `state_dir`,
`log_file_subpath`, `log_retention_days`, and `slack_channel`.
Individual `PIPELINE_*` environment variables do not override config entries.

| Variable | Default | Description |
|----------|---------|-------------|
| `TPO_CONFIG_FILE` | unset | Path to an alternate complete config file |
| `XDG_CONFIG_DIR` | `~/.config` | Base directory for the default `tpo/config.yaml` path |
| `HERMES_HOME` | `~/.hermes` | Base for the legacy fallback config path `tpo.yaml` |

## See Also

- [Getting-started tutorial](tutorial-getting-started.md) — End-to-end walkthrough
- [How to approve and ship a TODO](howto-approve-and-ship.md) — The full ship workflow
- [How to debug ticks and recover counters](howto-debugging-and-recovery.md) — Using `--verbose`, `--debug`, `recover-counter`
- [Circuit breaker explanation](explanation-circuit-breaker.md) — How no-progress tracking works
- [Pipeline contract explanation](explanation-pipeline-contract.md) — Why versioned contracts exist
