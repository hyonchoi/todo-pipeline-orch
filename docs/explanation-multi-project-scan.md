# How the multi-project scan loop works

The multi-project scan loop replaces per-project cron entries with one
global lock and one selection per project. This doc explains why it was
built this way and what was traded off.

## The problem

Before multi-project scanning, each project needed its own cron entry:

```bash
0 * * * * pipeline-watch tick project-a
0 * * * * pipeline-watch tick project-b
0 * * * * pipeline-watch tick project-c
```

Three problems:
1. **Hard to manage** — adding or removing projects means editing the crontab.
2. **Race conditions** — two cron entries fire at the same time, each tries to
   acquire the same global tick lock, one fails. The cron doesn't know the
   other project is running.
3. **State drift** — the old global `~/.hermes/` directory held state for one
   project. When a second project started, its state overwrote the first
   project's state.

The scan loop solves all three by making the tick itself discover the projects
and iterate over them under one lock.

## The approach

The scan loop lives in `_cmd_tick()` in `hermes_pipeline/cli.py`. It follows
four phases:

```
Tick:
  1. Acquire global TickLock (atomic mkdir)
  2. Discover active projects
  3. One-time global state migration (if exactly one project)
  4. Per-project tick loop
  5. Release lock
```

### Phase 1: Single global lock

The tick lock lives in the global state directory (`~/.hermes/tick.lock/`),
not per-project. There is one lock because the scan loop needs to serialize
access to the global state migration step and prevent two cron entries from
running at the same time.

If the lock is held, the tick exits early with "tick already in flight,
skipping". The stale-lock sweep checks the holder's PID and releases the lock
after `max_tick_duration_min` (default: 10 minutes).

**Trade-off:** If one project's selection takes a long time (e.g., a large
TODOS.md with many candidates), the other projects wait for it. This is
acceptable because the lock is only held during selection and kanban
registration — not during phase execution. Phase execution runs outside the
lock via the kanban adapter.

### Phase 2: Project discovery

`_discover_projects()` in `hermes_pipeline/project_config.py` scans the
`projects_dir` (default: `~/projects`) and returns a sorted list of project
directories that pass three filters:

1. **Has a `TODOS.md`** — the presence of `TODOS.md` is the canonical signal
   that a directory is a pipeline-watched project.
2. **Valid slug** — the directory name must pass `_validate_project_slug()`.
   Rejects `..`, `.` and single-character names to prevent path traversal.
3. **Not archived** — `enabled = true` (default if `project.toml` is missing).
   Setting `enabled = false` in `<project>/.hermes/project.toml` pauses
   selection without deleting `TODOS.md`.

```
~/projects/
  demo-app/           ← discovered (TODOS.md, enabled=true)
    TODOS.md
    .hermes/
  second-app/         ← discovered (TODOS.md, enabled=true)
    TODOS.md
    .hermes/
      project.toml    ← slack_channel = "project__second-app"
  archived-project/   ← skipped (enabled=false)
    TODOS.md
    .hermes/
      project.toml    ← enabled = false
  not-a-project/      ← skipped (no TODOS.md)
```

**Trade-off:** The project slug is the directory name. You cannot have two
projects with the same name under different parents — the kanban board uses
the slug as the tenant identifier.

### Phase 3: State migration

When the first tick runs without a project argument, global state files
(`current_tick_id.txt`, `circuit.json`, `outcomes/`) in `~/.hermes/` need to
move to `<project>/.hermes/`. The migration copies (not moves) the files so
every project gets its own copy.

**Key decision: migrate to the first project only.** With exactly one project,
the migration is automatic. With multiple projects, it's skipped.

Why? Imagine two projects share the global state. Project-a has tick_id `01HA`,
project-b would inherit the same tick_id. The next tick for project-b sees
"prior tick in-flight" and stalls permanently.

The migration copies files only if the destination doesn't already exist. If
the files are already there, the migration is a no-op.

**Trade-off:** The operator needs to manually decide which project owned the
global state when there are multiple projects. The tick warns:

```
global state exists at ~/.hermes/ but 3 projects were discovered —
can't determine which project owns the old state. Migrate manually.
```

### Phase 4: Per-project tick

For each project, `_tick_project()` runs the same flow as the single-project
tick:

1. **Check prior tick** — is `current_tick_id.txt` present? If yes, are all
   phases complete? If not, skip the project.
2. **Observe outcomes** — if the prior tick completed, read the outcomes and
   update the circuit breaker.
3. **Run selection** — the Hermes agent evaluates `TODOS.md` and picks a TODO
   (or returns `picked=None`).
4. **Register kanban phases** — create kanban tasks with `--parent` dependency
   chains for the selected TODO.

**Error isolation:** If project-a's selection fails (e.g., malformed TODOS.md),
the error is logged and the scan continues to project-b. One project's failure
does not block the others.

**Trade-off:** A failed project doesn't count toward the circuit breaker in
other projects. The circuit breaker is per-project — it lives in
`<project>/.hermes/circuit.json`.

## Alternatives considered

**Per-project locks.** Instead of one global lock, each project has its own
lock. Pro: projects run in parallel. Con: requires concurrent execution
(e.g., spawning subprocesses per project), which adds complexity and makes
error handling harder. The current design is sequential — simple and
predictable.

**Global selection, per-project state.** One Hermes agent call evaluates all
TODOS.md files together and picks one TODO across all projects. Pro: single
LLM call. Con: the agent needs context from all projects at once, which
increases token cost and makes the prompt harder to maintain. The current
design calls the agent once per project — more calls, but each call is
self-contained.

**Migration with heuristic ownership.** Try to guess which project owned the
global state (e.g., the oldest project directory, or the one with the matching
circuit breaker state). Pro: less manual work. Con: the heuristic could be
wrong, causing the wrong project to inherit stale state. The current design
requires the operator to decide — explicit is better than implicit.

## Related

- [How to set up multiple projects](howto-multi-project-setup.md) — configuring project.toml
- [How to troubleshoot state migration](howto-troubleshoot-state-migration.md) — fixing migration issues
- [Pipeline state machine](hermes-state-machine.md) — tick lifecycle and state transitions
