# Counter Recovery (counter.py)

The counter recovery module initializes and updates the compatibility cache at `.hermes/todo_id_counter`. When `TODOS.md` has valid sectioned tracked metadata and the value equals the scan-derived next ID, `recover-counter` writes `NEXT_TODO_ID - 1` to `.hermes/todo_id_counter`. Legacy or invalid section placement falls back to scanning active plus archived IDs without decreasing a higher existing cache.

## API

### `recover_counter(project_dir: Path) -> int`

Initialize or update the counter cache from tracked TODO metadata, with a legacy scan fallback.

**Parameters:**

- `project_dir` — Path to the project root (containing TODOS.md)

**Returns:**

- The counter value after recovery. With consistent tracked metadata this is `NEXT_TODO_ID - 1`; fallback recovery returns the higher of the existing counter and scanned maximum.

**Raises:**

- `FileNotFoundError` — If TODOS.md doesn't exist in the project directory

**Behavior:**

1. Reads `project_dir / "TODOS.md"` and its tracked `NEXT_TODO_ID` value under `## Metadata`.
2. Scans active IDs under `## Entries` in `TODOS.md` plus archived IDs in `TODOS-archive.md` and determines `scanned_max` (0 if no TODO-N entries).
3. When exactly one valid tracked value exists and equals `scanned_max + 1`, writes `NEXT_TODO_ID - 1` to the counter cache.
4. Otherwise, reads the existing counter (0 if missing or corrupt) and writes `max(existing_value, scanned_max)`.

### `COUNTER_FILE`

Module-level constant for the per-project counter path: `.hermes/todo_id_counter`.

### `TODO_ID_RE`

Module-level compiled regex: `\bTODO-(\d+)\b`. Matches TODO-N patterns anywhere in text (not just as list entries).

## How it's used

The `recover_counter()` function is exposed via the `tpo recover-counter` CLI subcommand:

```bash
uv run tpo recover-counter my-project
```

The CLI handler (`_cmd_recover_counter` in cli.py) resolves the project directory from the configured `projects_dir`, validates the slug, calls `recover_counter()`, and prints the result.

## Design decisions

### Tracked state is authoritative

`NEXT_TODO_ID` under `## Metadata` is the source of truth when valid sectioned metadata is consistent with active and archived IDs. `recover_counter()` writes `NEXT_TODO_ID - 1` only in that case, keeping the legacy counter cache compatible without letting stale metadata resurrect IDs.

### Legacy max-over-write semantics (never decrease)

For legacy TODO files without consistent tracked state, the counter is set to `max(existing_value, scanned_max)`, not `scanned_max`. If you had TODO-8 and then removed it from TODOS.md, the counter stays at 8 instead of dropping to the new scanned maximum. This prevents ID resurrection during fallback recovery.

### Section-aware entry scanning

Recovery scans active entries only under `## Entries` in `TODOS.md`; TODO-like schema examples and misplaced metadata do not affect the scan-derived next ID. Archived IDs remain part of the scan. Legacy files without a valid sectioned layout retain the compatibility fallback scan.

### Atomic file writes

The counter is written through a same-directory temporary file and `os.replace()`. A crash before replacement leaves the prior counter intact; temporary files are cleaned up when the write raises.

### Creates `.hermes/` directory if needed

Unlike the counter reader (which assumes `.hermes/` exists), `recover_counter()` creates the directory if it doesn't exist. This is the initialization path — the directory shouldn't exist yet if the counter is missing.

## Related

- [How to debug pipeline ticks and recover TODO counters](howto-debugging-and-recovery.md) — CLI usage for `--verbose`, `--debug`, and `recover-counter`
- [Run a manual tick](howto-pipeline-tick.md) — Running `tpo tick`
- [Pipeline state machine](hermes-state-machine.md) — State transitions and file layout under `.hermes/`
