## Stable TODO-<n> ID Assignment

### ID sequencing rule

- IDs are assigned sequentially in insertion order, starting from 1.
- Once a TODO-<n> is committed, its ID is immutable.
- The common path reads `NEXT_TODO_ID` from `## Metadata` in `TODOS.md` and assigns that value.
- After a successful add, increment `NEXT_TODO_ID` by 1 in the same locked atomic write as the new entry.
- Archived entries under `## Entries` count during reconciliation. TODO-like examples in `## Entry Schema` never count. Do not fill gaps.

### Tracked state rule

`TODOS.md` must contain this file-level metadata line in `## Metadata`:

```markdown
NEXT_TODO_ID: <n>
```

`<n>` must be a positive base-10 integer and means "the next ID to assign."

### Reconciliation algorithm

1. Read `NEXT_TODO_ID` from `TODOS.md`.
2. If the value is missing, duplicated, non-integer, zero, negative, stale, or already used by an active TODO, scan only entries under `## Entries` in `TODOS.md` and `TODOS-archive.md`.
3. Compute `max(all IDs) + 1`, or `1` when no IDs exist.
4. Write the corrected `NEXT_TODO_ID` in place and report the correction.
5. For `--add`, continue by assigning the corrected ID and incrementing the tracked value.

### Counter cache

`.hermes/todo_id_counter` is compatibility/cache state only. It may be updated after a successful TODO write, but it no longer decides the next ID.
