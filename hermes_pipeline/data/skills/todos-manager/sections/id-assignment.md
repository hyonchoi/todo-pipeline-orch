## Stable TODO-<n> ID Assignment

### ID sequencing rule

- IDs are assigned sequentially in **insertion order**, starting from 1.
- Once a TODO-<n> is committed, its ID is **immutable** (even if the entry is moved, deferred, or deleted).
- The next new entry receives `max(all IDs in TODOS.md + TODOS-archive.md) + 1`.
- Archived entries count toward ID computation — do not skip archived IDs.

### Bootstrap algorithm

On each invocation, scan **both** TODOS.md and TODOS-archive.md for existing IDs:

1. **Parse all entries** in TODOS.md using regex `/TODO-(\d+)/g`.
2. **Parse all entries** in TODOS-archive.md (if it exists) using same regex.
3. **Collect used IDs** from both files into a single set.
4. **Compute next ID** as `max(used_ids) + 1`.
5. **If both files are empty:** Start at `TODO-1`.
6. **If IDs are non-contiguous** (e.g., `{1, 2, 5}`), still use `6` for the next entry. Do not attempt to fill gaps.

### Counter cache

`.hermes/todo_id_counter` is a performance cache — not authoritative. On write, update the counter to match the computed value. If the counter exists but diverges from the scan, trust the scan and correct the cache.

