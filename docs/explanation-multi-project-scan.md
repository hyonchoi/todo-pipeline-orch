# How the multi-project scan loop works

The multi-project scan loop replaces per-project cron entries with one
discovery pass, per-project locks, and one selection per project. This doc
explains why it was built this way and what was traded off.

## The problem

Before multi-project scanning, each project needed its own cron entry:

```bash
0 * * * * tpo tick project-a
0 * * * * tpo tick project-b
0 * * * * tpo tick project-c
```

Three problems:
1. **Hard to manage** — adding or removing projects means editing the crontab.
2. **Race conditions** — overlapping cron entries make it hard to tell which
   project is already running and which projects remain eligible.
3. **State drift** — the old global `~/.hermes/` directory held state for one
   project. When a second project started, its state overwrote the first
   project's state.

The scan loop solves all three by making the tick itself discover the projects
and iterate over them with a lock scoped to each project.

## The approach

The scan loop lives in `_cmd_tick()` in `hermes_pipeline/cli.py`. It follows
these phases:

```
Tick:
  1. Discover active projects
  2. For each project:
     a. Acquire the project's TickLock (atomic mkdir)
     b. Run the project tick, or skip if already locked
     c. Release the project's lock
```

### Phase 1: Per-project locks

Each tick lock lives in its project's state directory
(`<project>/.hermes/tick.lock/`). Two overlapping cron invocations cannot
advance the same project simultaneously, while an invocation that encounters a
locked project can skip it and continue scanning other projects.

If a project's lock is held, that project is skipped with "tick already in
flight, skipping". The stale-lock sweep checks the holder's PID and releases
the project lock after `max_tick_duration_min` (default: 10 minutes).

**Trade-off:** Projects are visited sequentially within one scan, so one slow
selection can delay later projects in that invocation. A concurrent invocation
can still skip the busy project and progress other unlocked projects.

### Phase 2: Project discovery

`_discover_projects()` in `hermes_pipeline/project_config.py` scans the
`projects_dir` (default: `~/projects`) and returns a sorted list of project
directories that pass three filters:

1. **Has a pipeline contract** — `<project>/.hermes/pipeline.toml` (written by
   `tpo init <project>`) is the canonical signal that a directory is a
   pipeline-watched project. A directory that has `.hermes/` or a legacy
   `TODOS.md` but no contract is skipped with a WARNING suggesting
   `tpo init <slug>`. The backlog itself lives in GitHub Issues (`tpo:todo`
   label; canonical ID `TODO-<issue-number>`) — see
   [issue tracker](agents/issue-tracker.md#tpo-backlog-items) and
   [ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md).
2. **Valid slug** — the directory name must pass `_validate_project_slug()`.
   Rejects `..`, `.` and single-character names to prevent path traversal.
3. **Not archived** — `enabled = true` (default if `project.toml` is missing).
   Setting `enabled = false` in `<project>/.hermes/project.toml` pauses
   selection without deleting the contract.

```
~/projects/
  demo-app/           ← discovered (pipeline.toml, enabled=true)
    .hermes/
      pipeline.toml
  second-app/         ← discovered (pipeline.toml, enabled=true)
    .hermes/
      pipeline.toml
      project.toml    ← slack_channel = "project__second-app"
  archived-project/   ← skipped (enabled=false)
    .hermes/
      pipeline.toml
      project.toml    ← enabled = false
  legacy-project/     ← skipped with WARNING (no pipeline.toml; run `tpo init`)
    TODOS.md
    .hermes/
  not-a-project/      ← skipped silently (no .hermes/)
```

**Trade-off:** The project slug is the directory name. You cannot have two
projects with the same name under different parents — the kanban board uses
the slug as the tenant identifier.

### Per-project state

Every project owns its state under `<project>/.hermes/`
(`current_tick_id.txt`, `circuit.json`, `decisions/`, `outcomes/`, `runs/`).
The legacy automatic copy of global `~/.hermes/` state into the first
discovered project was removed as a hard cutover; `~/.hermes/` now holds only
global configuration.

### Phase 3: Per-project tick

For each project, `_tick_project()` runs the same flow as the single-project
tick:

1. **Check prior tick** — is `current_tick_id.txt` present? If yes, are all
   phases complete? If not, skip the project.
2. **Observe outcomes** — if the prior tick completed, read the outcomes and
   update the circuit breaker.
3. **Run selection** — the orchestrator compiles the eligible `tpo:todo`
   issues from the project's GitHub origin into a candidate list, and the
   Hermes agent picks one (or returns `picked=None`).
4. **Register kanban phases** — create kanban tasks with `--parent` dependency
   chains for the selected TODO.

**Error isolation:** If project-a's selection fails (e.g., `gh` unavailable,
recorded as a `tracker_error` decision),
the error is logged and the scan continues to project-b. One project's failure
does not block the others.

**Trade-off:** A failed project doesn't count toward the circuit breaker in
other projects. The circuit breaker is per-project — it lives in
`<project>/.hermes/circuit.json`.

## Alternatives considered

**Single global lock.** One lock for the entire scan would make overlapping
invocations easy to serialize. However, one busy project would prevent every
unrelated project from being considered. Per-project locks preserve
same-project exclusion without putting all projects in one failure and latency
domain.

**Global selection, per-project state.** One Hermes agent call evaluates all
projects' candidate issues together and picks one TODO across all projects. Pro: single
LLM call. Con: the agent needs context from all projects at once, which
increases token cost and makes the prompt harder to maintain. The current
design calls the agent once per project — more calls, but each call is
self-contained.

## Related

- [How to set up multiple projects](howto-multi-project-setup.md) — configuring project.toml
- [Issue tracker conventions](agents/issue-tracker.md) — the GitHub Issues backlog the scan selects from
- [Pipeline state machine](hermes-state-machine.md) — tick lifecycle and state transitions
