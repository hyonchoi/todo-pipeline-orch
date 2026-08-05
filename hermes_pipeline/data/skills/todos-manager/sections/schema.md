# TODOS.md Schema

## File location and format

TODOS.md is stored at the repo root. Each entry occupies a single markdown list item (`- [ ] ...`), with fields as sub-bullets using bold labels.

## Entry header line

```markdown
- [ ] **TODO-<n>: <Title>** — <One-line summary>
```

## Status markers

| Marker | Meaning |
|--------|---------|
| `[ ]` | Pending |
| `[→]` | In progress |
| `[x]` | Done |
| `[~]` | On hold |

## Required fields

| Field | Description | Format |
|-------|-------------|--------|
| **What:** | What needs to be done | Free text |
| **Why:** | Why this task matters | Free text |
| **Decisions:** | Key decisions | Backtick-delimited: `Priority \`P1\`, Effort \`M\`, Phase \`4 (Development)\`, Branch \`feature/...\`, Test Coverage \`required/not-required\`, Security Review \`required/not-required\`, UI Review \`required/not-required\`` |

## Optional fields

| Field | Description |
|-------|-------------|
| **Pros:** | Benefits |
| **Cons:** | Risks/drawbacks |
| **Context:** | References, design doc pointers, file locations |
| **Depends on:** | Other TODO-<n> references |
| **Assumptions:** | Preconditions |
| **Completed:** | Version + date (set when done) |
| **Resolved design:** | Design decisions (zero or more) |
| **Plan:** | Single repository-relative path to executable implementation instructions. The execution authority and sole actionability gate. |
| **Spec:** | Single repository-relative path to the authoritative outcome contract. |
| **Reference:** | Comma-separated repository-relative paths to supplementary context; literal commas are not allowed in a path. |

## Example: complete entry

```markdown
- [ ] TODO-42: refactor pipeline-watcher.py into uv modules
  - **What:** Split `pipeline-watcher.py` into modular Python packages under `hermes_pipeline/`.
  - **Why:** Single-file monolith is hard to test and extend. Modularization unblocks CI integration.
  - **Pros:** Testable modules, shared utilities, clear boundaries
  - **Cons:** Migration effort, import path updates across test suite
  - **Context:** Design lives in [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md)
  - **Depends on:** `TODO-40` (design review finalized)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/modularize-watcher`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
```

## Canonical Document Layout

When creating or converting TODOS.md, use these sections in this order:
`## Metadata`, `## Entry Schema`, and `## Entries`. `NEXT_TODO_ID` is
file-level state in `## Metadata`, not an entry field. `## Entry Schema` is
documentation only; TODO-like examples in it are never active entries and must
not be parsed, validated, dependency-resolved, listed, revised, archived, or
counted.
Any `NEXT_TODO_ID:` line under `## Entry Schema`, under `## Entries`, or
outside the canonical sections is invalid tracked state.

```markdown
# TODOS

## Metadata

NEXT_TODO_ID: <n>

## Entry Schema

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Plan:**, **Spec:**, **Reference:**
> - Attachments may be proposed by `--add` or `--revise`, but require explicit user confirmation
> - ID: sequential, immutable TODO-<n>
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

## Entries

- [ ] **TODO-<n>: <Title>** — <Summary>
  - **What:** ...
  - **Why:** ...
  - **Decisions:** ...
```
