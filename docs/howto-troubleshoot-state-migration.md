# How to troubleshoot state migration

The multi-project scan loop (v0.3.3+) runs a one-time migration that copies
state files (`current_tick_id.txt`, `circuit.json`, `outcomes/`) from the
global `~/.hermes/` into `<project>/.hermes/`. This guide covers what to do
when the migration doesn't work as expected.

By the end, you'll have identified the cause and either let the scan loop
handle it or fixed it manually.

## Prerequisites

- You have at least one project under `PIPELINE_PROJECTS_DIR`.
- You've run `pipeline-watch tick` without a project argument at least once.

## Understanding the migration

The migration runs automatically on the first `pipeline-watch tick` (no project
argument). **Important:** auto-migration only runs when exactly one project is
discovered. With multiple projects, the tick warns and skips — it can't know
which project owned the old global state.

```
One project:  state copied from ~/.hermes/ to <project>/.hermes/
Two+ projects: skipped with warning; migrate manually
```

## Steps

### 1. Check whether migration ran

Run a tick and look at the log output:

```bash
uv run pipeline-watch tick --debug
```

Three outcomes:

- **"Copying current_tick_id.txt"** — migration is in progress. Let it finish.
- **"no active projects found"** — nothing to migrate. Check `PIPELINE_PROJECTS_DIR`.
- **"global state exists at ... but N projects were discovered"** — migration
  was skipped because there are multiple projects.

### 2. Verify the destination files exist

Check if the state files landed in the right place:

```bash
ls ~/projects/<project>/.hermes/current_tick_id.txt
ls ~/projects/<project>/.hermes/circuit.json
ls ~/projects/<project>/.hermes/outcomes/
```

If they are missing, the migration didn't run yet or failed silently.

### 3. Fix "migration skipped — multiple projects"

If you see the "N projects were discovered" warning, you need to decide which
project owned the old global state. The global `~/.hermes/current_tick_id.txt`
and `~/.hermes/circuit.json` belonged to whichever project was running before
multi-project was enabled.

**Option A: Move the files manually (recommended)**

```bash
# If the old state belonged to project-a:
mkdir -p ~/projects/project-a/.hermes

mv ~/.hermes/current_tick_id.txt ~/projects/project-a/.hermes/
mv ~/.hermes/circuit.json ~/projects/project-a/.hermes/
mv ~/.hermes/outcomes ~/projects/project-a/.hermes/

# Now run the tick — the global state is gone, migration is skipped,
# and project-a has the right state.
uv run pipeline-watch tick
```

**Option B: Delete the global state if it's stale**

If the old state is no longer relevant (e.g., the project was restarted from
scratch), simply delete the files:

```bash
rm ~/.hermes/current_tick_id.txt
rm ~/.hermes/circuit.json
rm -rf ~/.hermes/outcomes
```

The next tick starts fresh.

### 4. Fix "project stalled on stale tick_id"

If a project is not selected for a new TODO and its decision shows
`picked=None` with the rationale that a prior tick is in-flight, it may be
holding a stale `current_tick_id.txt`.

Check the tick_id:

```bash
cat ~/projects/<project>/.hermes/current_tick_id.txt
```

If the tick_id is old (from before the migration), clear it:

```bash
rm ~/projects/<project>/.hermes/current_tick_id.txt
uv run pipeline-watch tick
```

The next tick treats the project as having no prior tick and runs selection
from scratch.

### 5. Fix "circuit breaker inherited from another project"

If auto-migration ran with only one project at the time, that project now has
its own copy of `circuit.json`. If the circuit breaker trips (shows
`backed_off: true`) right after migration, it inherited the counter.

Check:

```bash
cat ~/projects/<project>/.hermes/circuit.json | jq .
```

If `consecutive_no_progress` is greater than 0 and you want to reset it:

```bash
echo '{"consecutive_no_progress": 0, "backed_off": false}' \
  > ~/projects/<project>/.hermes/circuit.json
```

The next tick resumes with a clean circuit breaker.

## Verification

After fixing the issue, run a tick and check the logs:

```bash
uv run pipeline-watch tick --debug 2>&1 | grep -i "migrat\|project\|selection"
```

You should see:
- `"discovered N active projects"` — project discovery is working
- `"selected TODO-X"` — the Hermes agent picked a TODO (or `picked=None` if nothing is ready)

## Troubleshooting

**"No such file or directory" for `.hermes/outcomes` in a project.**
The migration hasn't created the per-project directory yet. Create it manually:

```bash
mkdir -p ~/projects/<project>/.hermes/outcomes
```

Then run the tick again.

**"Warning: one-time state migration to project-a: ...".**
The migration hit an error (e.g., permission denied, disk full). Check the
full log output. The error is logged at the warning level, so run with
`--debug` to see the full traceback.

**A project was discovered but its `.hermes/` directory doesn't exist.**
Normal — the tick creates it on the first run. The migration only touches the
first project's directory. Other projects start with fresh state.

## Related

- [How to set up multiple projects](howto-multi-project-setup.md) — configuring project.toml
- [How to run a manual tick](howto-pipeline-tick.md) — the tick flow
- [Pipeline state machine](hermes-state-machine.md) — tick lifecycle and state transitions
