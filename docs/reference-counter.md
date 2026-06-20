# Counter Recovery (counter.py)

The counter recovery module initializes and updates `.hermes/todo_id_counter` by scanning TODOS.md for the highest TODO-N ID. It is the safety net when the counter file is missing — for example, when bootstrapping a project that already has hand-written TODOs but no counter yet.

## API

### `recover_counter(project_dir: Path) -> int`

Scan TODOS.md for TODO-N entries and initialize/update the counter file.

**Parameters:**

- `project_dir` — Path to the project root (containing TODOS.md)

**Returns:**

- The counter value after recovery (the maximum of the existing counter and the scanned maximum from TODOS.md)

**Raises:**

- `FileNotFoundError` — If TODOS.md doesn't exist in the project directory

**Behavior:**

1. Reads `project_dir / "TODOS.md"` and finds all TODO-N patterns using the regex `\bTODO-(\d+)\b`
2. Determines `scanned_max` — the maximum N found (0 if no TODO-N entries)
3. Reads the existing counter from `project_dir / ".hermes/todo_id_counter"` (0 if missing or corrupt)
4. Writes `max(existing_value, scanned_max)` back to the counter file

### `COUNTER_FILE`

Module-level constant set from `Config().counter_file_subpath`. Default value: `.hermes/todo_id_counter`.

### `TODO_ID_RE`

Module-level compiled regex: `\bTODO-(\d+)\b`. Matches TODO-N patterns anywhere in text (not just as list entries).

## How it's used

The `recover_counter()` function is exposed via the `pipeline-watch recover-counter` CLI subcommand:

```bash
uv run pipeline-watch recover-counter my-project
```

The CLI handler (`_cmd_recover_counter` in cli.py) resolves the project directory from `PIPELINE_PROJECTS_DIR / project`, validates the slug, calls `recover_counter()`, and prints the result.

## Design decisions

### Max-over-write semantics (never decrease)

The counter is set to `max(existing_value, scanned_max)`, not `scanned_max`. If you had TODO-8 and then removed it from TODOS.md (completed it), the counter stays at 8 instead of dropping to whatever is the new max. This prevents ID collisions — a future TODO could be assigned 8 again if the counter dropped.

### Regex matches TODO-N anywhere

The regex `\bTODO-(\d+)\b` matches TODO-N patterns anywhere in TODOS.md, not just as list entries. If TODO-6 appears in a "Depends on" note within a TODO-1 entry, the counter is set to 6. This is intentional — it ensures the counter never collides with referenced IDs, even if they aren't active entries.

### Direct file writes

The counter is written via `counter_path.write_text()`, not an atomic temp+rename. If the process crashes mid-write, the counter file may be corrupt. The reader treats a corrupt file as 0 (see `ValueError`/`OSError` handling at counter.py:51), so a crash doesn't corrupt the pipeline.

### Creates `.hermes/` directory if needed

Unlike the counter reader (which assumes `.hermes/` exists), `recover_counter()` creates the directory if it doesn't exist. This is the initialization path — the directory shouldn't exist yet if the counter is missing.

## Related

- [How to debug pipeline ticks and recover TODO counters](howto-debugging-and-recovery.md) — CLI usage for `--verbose`, `--debug`, and `recover-counter`
- [Run a manual tick](howto-pipeline-tick.md) — Running `pipeline-watch tick`
- [Pipeline state machine](hermes-state-machine.md) — State transitions and file layout under `.hermes/`
