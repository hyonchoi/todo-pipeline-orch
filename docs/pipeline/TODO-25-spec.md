<!-- /autoplan restore point: /Users/hyonchoi/.gstack/projects/todo-pipeline-orchestrator/main-autoplan-restore-20260721-171816.md -->
# TODO-25: Optional Spec/Reference field, threaded into first-phase prompt

Status: Design finalized (grilling session, resolved decisions below). Not yet implemented.

## Summary

Add two optional TODOS.md fields — `**Spec:**` and `**Reference:**` — that,
when present on a TODO entry, get resolved and injected as file-path
references into the prompt of the pipeline's first phase (currently
`phase_2_autoplan`, determined dynamically rather than hardcoded).

## Problem

`phase_2_autoplan`'s prompt (and any phase's prompt, via
`_render_phase_prompt`) only ever sees the pipeline context header
(`todo_id`/`tick_id`/`project_slug`) plus its own static template text. There
is no way to hand the phase a fuller spec document — e.g. the output of an
office-hours / grill-with-docs / spec skill run — without either bloating the
TODO's inline `What:`/`Why:` text or leaving that spec doc invisible to the
pipeline entirely.

## Schema changes (`todos-manager` skill)

Two new **optional** fields, added to `schema.md`'s optional-fields table and
the TODOS.md preamble blockquote:

| Field | Cardinality | Meaning |
|-------|-------------|---------|
| `**Spec:**` | single path | The authoritative deliverable from office-hours / grill-with-docs / spec skills. Not a synonym for `Reference:` — this is the doc that should drive the phase. |
| `**Reference:**` | comma-separated list of paths | Supplementary/background material. Not prescriptive. |

Both fields may be present on the same entry simultaneously. Neither
overlaps with the existing `**Context:**` field, which remains free-text
prose (may still mention doc pointers informally, but is not machine-parsed
for this feature).

### `--add` / `--revise` behavior

- `--add` does **not** prompt for `Spec:`/`Reference:` and they are **not**
  part of auto-research pre-fill. New entries are created without them —
  at creation time there is usually no spec doc yet.
- They are appended **afterward**, once a spec/reference doc exists, via
  `todos-manager --revise` on the existing entry.
- Even through `--revise`, the value is always **typed by the user**
  verbatim — no AI-suggested or auto-detected path (e.g. no scanning
  `docs/pipeline/TODO-<n>-*.md` and offering it as a suggestion). This is a
  deliberate deviation from `--revise`'s usual AI-pre-fill behavior for other
  fields, because a wrong guessed path is worse than an empty field.

## Pipeline changes (`hermes_pipeline`)

### New module: `hermes_pipeline/todos_md.py`

A small, standalone parser — not a full markdown parser, and not a reuse of
the `todos-manager` skill's prose-based logic (which isn't Python-importable
anyway, being an LLM-facing skill).

```python
def find_todo_fields(todos_md_path: Path, todo_id: str) -> dict:
    """Locate the TODO-<n> entry in todos_md_path and extract Spec:/Reference:.

    Returns {"spec": str | None, "references": list[str]}.
    Never raises for parsing problems — see Failure handling below.
    """
```

Implementation notes:
- Locates the `- [ ] **TODO-<n>: ...**` (or `[x]`/`[→]`/`[~]`) entry header
  matching `todo_id`, then scans its sub-bullets for `**Spec:**` and
  `**Reference:**` lines.
- `Reference:` values are split on `,` and stripped.
- Testable in isolation via fixture TODOS.md content (no pipeline
  dependencies).

### Dynamic "first phase" detection (not hardcoded to `phase_2_autoplan`)

In `_invoke_hermes` (`hermes_pipeline/phases.py`), alongside the existing
`phases_cfg = {p.phase_key: p for p in load_phases(...)}` dict build, also
keep the ordered list:

```python
phases_list = load_phases(resolve_profile_phases_path(profile))
phases_cfg = {p.phase_key: p for p in phases_list}
...
is_first_phase = phase.phase_key == phases_list[0].phase_key
```

Spec/Reference injection only happens when `is_first_phase` is true. This
means the mechanism follows whatever phase is first in a given profile's
`phases.yaml`, rather than being pinned to the literal string
`"phase_2_autoplan"`. (Today, for the `gstack` profile, that resolves to
`phase_2_autoplan` since it's index 0 — there is no `phase_1_...` phase in
the current file.)

### Lookup + injection flow

In `_invoke_hermes`, when `is_first_phase` is true:

1. Resolve `todos_md_path = Path(project_dir) / "TODOS.md"`.
2. Call `todos_md.find_todo_fields(todos_md_path, todo_id)`.
3. For the returned `spec` path (if any) and each `references` path,
   resolve via `(Path(project_dir) / path).resolve()` and verify the
   resolved path falls under `Path(project_dir).resolve()` (containment
   check — rejects `../` traversal and symlink escapes, since `Spec:`/
   `Reference:` are free-typed paths in TODOS.md). Then existence-check.
   Resolve **per item, independently** — drop only the invalid ones
   (missing OR outside project_dir), keep the rest.
4. Pass the surviving `spec_path: str | None` and
   `reference_paths: list[str]` into `_render_phase_prompt` as new optional
   keyword arguments.

`_render_phase_prompt` stays a pure string-formatting function. It appends
conditional lines to its existing header block:

```
Pipeline context:
- todo_id: TODO-25
- tick_id: ...
- project_slug: ...
Work on TODO-25 ONLY. Do not pick a different TODO.

Spec (authoritative): docs/pipeline/TODO-25-spec.md
Reference material: docs/notes/a.md, docs/notes/b.md
```

- `Spec (authoritative):` line omitted if absent or invalid.
- `Reference material:` line omitted if the list ends up empty.
- The entire two-line block omitted if neither field survives — so prompt
  output for TODOs without these fields is byte-identical to today.

Only the **path** is injected, never file content. The phase already runs
with file-system tool access (per `phases.yaml`'s `tools` field for that
phase), so the agent reads the file itself. This avoids prompt bloat,
encoding concerns, and truncation logic in `phases.py`.

### Failure handling — fail-soft throughout

None of the following raise or halt the pipeline run; each logs a warning
and degrades gracefully:

| Condition | Behavior |
|-----------|----------|
| `TODOS.md` missing at `project_dir` root | No injection; phase runs as it does today. |
| `todo_id` not found in `TODOS.md` | No injection. |
| Malformed/unparseable entry | No injection. |
| `Spec:` path present but file doesn't exist on disk | Drop `Spec:` only; `Reference:` items still evaluated independently. |
| One or more `Reference:` paths don't exist | Drop only the missing ones; keep the rest. |
| `Spec:`/`Reference:` path resolves outside `project_dir` (`../` traversal or symlink escape) | Drop that item only; treated identically to "doesn't exist" — no distinct error surfaced to avoid signaling to a TODOS.md editor which check tripped. |

This mirrors the existing pipeline philosophy that the phase's core function
(running autoplan) must not be blocked by an optional augmentation failing.

## Files touched

- `skills/todos-manager/SKILL.md` — note new optional fields are `--revise`-only, not `--add`.
- `skills/todos-manager/sections/schema.md` — add `Spec:`/`Reference:` rows to optional-fields table; update preamble template text.
- `TODOS.md` — preamble blockquote optional-fields list gets the two new field names (existing entries unaffected).
- `hermes_pipeline/todos_md.py` — new module, `find_todo_fields()`.
- `hermes_pipeline/phases.py` — `_invoke_hermes` gains ordered-list lookup + `is_first_phase` check + Spec/Reference resolution; `_render_phase_prompt` gains two new optional kwargs and conditional header lines.

## Test coverage (required)

- `todos_md.find_todo_fields()`:
  - Entry with both fields → both parsed correctly.
  - Entry with only `Spec:` / only `Reference:`.
  - Entry with neither field → `{"spec": None, "references": []}`.
  - `Reference:` with multiple comma-separated paths, including whitespace variance.
  - **Malformed TODOS.md** (e.g. entry header present but fields section truncated/garbled, or file isn't valid per schema) → must not raise; returns the empty/partial result.
  - **Multiple TODO entries in the file** → the entry-header match must anchor to `todo_id` specifically and scan only that entry's sub-bullet block (up to the next `- [ ]`/`[x]`/`[→]`/`[~]` entry marker or EOF), not the whole file — otherwise a naive regex can bleed into a neighboring entry's fields.
  - **`Spec:`/`Reference:` path resolves outside `project_dir`** (`../../etc/passwd`, or a symlink pointing outside the repo) → dropped, same as a missing file; never raises, never surfaces the traversed path.
  - `todo_id` not present in file → returns empty result, no raise.
  - `TODOS.md` file missing entirely → returns empty result, no raise.
- `_render_phase_prompt`:
  - No spec/reference kwargs → output unchanged from current behavior (regression guard).
  - Both provided → both lines present, correctly formatted.
  - Only one provided → only that line present.
- `_invoke_hermes` / first-phase detection:
  - Confirms injection happens only when `phase.phase_key == phases_list[0].phase_key`, not for any other phase key in the profile.
  - Existence-check drops a nonexistent `Spec:` path but keeps valid `Reference:` entries (and vice versa).
  - A `Spec:`/`Reference:` path that traverses outside `project_dir` is dropped by the containment check, independently of the existence check.
  - `phases_list` must never be indexed unguarded — if `load_phases()` ever returns an empty list (malformed/empty `phases.yaml`), `is_first_phase` resolution must not raise `IndexError`; treat as "no first phase" and skip injection.

## Depends on

`TODO-24` (phases.yaml revision — already completed; this design assumes the
current 9-phase `gstack` profile shape).

## Out of scope

- No changes to `Context:` field semantics.
- No auto-suggestion of `Spec:`/`Reference:` values at any point (neither
  `--add` nor `--revise`).
- No content-inlining of spec/reference files into prompts.
- No enforcement that `phase_key` numbering (`phase_<n>_...`) be contiguous
  or start at 1 — "first phase" is purely positional (`phases_list[0]`).

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale |
|---|-------|----------|-----------------|-----------|-----------|
| 1 | CEO | Accept premises as stated; defer "TODO-as-schema" reframe to TODOS.md | Taste | P3 Pragmatic | Narrow, already-designed P2/S feature; the bigger schema idea is a multi-week architecture project with no scope boundary — logged as future TODO, not blocking this ticket |
| 2 | CEO | Review mode: HOLD SCOPE | Mechanical | P3 Pragmatic | Design doc is fully written (schema, failure handling, test plan); diff is 2 files, well under 8-file complexity threshold |
| 3 | Eng | Add containment check (canonicalize + verify under project_dir) for Spec:/Reference: paths | User-surfaced (accepted) | P1 Completeness | Prevents path-traversal/symlink-escape from a TODOS.md-authored path reaching agent file-read tools; same fail-soft drop behavior as existing missing-file case, so cost is ~5 lines |
| 4 | Eng | Anchor entry-header regex to todo_id and scan only that entry's sub-bullet block | Mechanical | P5 Explicit | Naive regex could bleed into a neighboring TODO entry's fields with multiple entries in file; added as explicit test requirement |
| 5 | Eng | Guard `phases_list[0]` against empty phases_list (no raise, treat as no-first-phase) | Mechanical | P1 Completeness | `load_phases()` returning empty is possible under malformed phases.yaml; must not crash first-phase detection |

## GSTACK REVIEW REPORT

### Runs / Status / Findings

| Phase | Voices | Status | Findings |
|-------|--------|--------|----------|
| CEO | Claude subagent only (codex unavailable: model `gpt-5.6-sol` not supported for this ChatGPT account — `[subagent-only]`) | issues_open → resolved | 1 critical (scope reframe, deferred not blocking), premises accepted by user, mode=HOLD SCOPE |
| Design | Skipped — no UI scope detected (grep matches were false positives: "form"→"format", "pip"→"pipeline") | n/a | n/a |
| Eng | Claude subagent only (`[subagent-only]`, same codex unavailability) | issues_open → resolved | 1 high (path traversal — fixed in spec), 2 medium (regex anchoring, phases_list[0] guard — added as test requirements), 2 low (comma-parsing edge cases, no-injection-for-later-phases rationale — accepted as documented) |
| DX | Skipped — no developer-facing scope detected beyond existing pipeline internals | n/a | n/a |

VERDICT: Spec updated with 3 concrete additions (containment check, regex anchoring requirement, phases_list guard requirement) based on eng subagent findings. Codex unavailable both phases (`gpt-5.6-sol` model rejected by this ChatGPT account) — review ran in `[subagent-only]` degradation mode throughout. Design ready for implementation.

NO UNRESOLVED DECISIONS
