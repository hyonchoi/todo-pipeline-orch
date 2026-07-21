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
| **Decisions:** | Key decisions | Backtick-delimited: `Priority \`P1\`, Effort \`M\`, Phase \`4 (Development)\`, Branch \`feature/...\`, Test Coverage \`required/not-required\`, Security Review \`required/not-required\`` |

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
| **Spec:** | Single path to the authoritative deliverable (e.g. from office-hours / grill-with-docs / spec skills) — drives the pipeline's first phase. `--revise`-only: never AI-suggested, never part of `--add` auto-research; always user-typed verbatim. |
| **Reference:** | Comma-separated list of supplementary/background paths, threaded into the pipeline's first phase prompt. Same `--revise`-only, never-auto-suggested rule as `Spec:`. Not a synonym for `Context:`, which stays free-text prose. |

## Example: complete entry

```markdown
- [ ] TODO-42: refactor pipeline-watcher.py into uv modules
  - **What:** Split `pipeline-watcher.py` into modular Python packages under `hermes_pipeline/`.
  - **Why:** Single-file monolith is hard to test and extend. Modularization unblocks CI integration.
  - **Pros:** Testable modules, shared utilities, clear boundaries
  - **Cons:** Migration effort, import path updates across test suite
  - **Context:** Design lives in [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md)
  - **Depends on:** `TODO-40` (design review finalized)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/modularize-watcher`, Test Coverage `required`, Security Review `not-required`
```

## Preamble Template

When creating or converting TODOS.md, insert this blockquote as the file header:

```markdown
# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Spec:**, **Reference:**
> - **Spec:**/**Reference:** are `--revise`-only (never suggested by `--add` or auto-research); always typed verbatim
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`
```
