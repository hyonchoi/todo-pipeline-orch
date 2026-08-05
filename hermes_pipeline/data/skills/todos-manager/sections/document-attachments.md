# Document Attachment Discovery and Validation

## Purpose

This shared policy governs document attachment discovery for both `--add` and
`--revise`. It consumes the TODO title, summary, TODO ID when available,
explicit context paths, Git status, the remaining research budget, and current
attachment values. It produces at most five records shaped as
`{path, roles, relevance_reason, source, validation}` and an explicit
unresolved or none state for each attachment role.

The roles are distinct:

- **Plan:** is the execution authority and the sole actionability gate.
- **Spec:** is the authoritative outcome contract.
- **Reference:** is supplementary, non-authoritative context.

A Plan without a Spec is actionable. A Spec without a Plan is not actionable.
When both are present, execute the Plan and validate the result against the
Spec.

## Shared budget and discovery order

Attachment discovery is part of the same bounded research operation as
`sections/auto-research.md`; it does not start a second counter. The combined
operation may perform no more than **20 file reads** and **10 searches**.
Record every read and search against those shared counters.

Use this order, stopping when five qualified candidates have been collected:

1. Validate user-provided explicit paths and explicit context paths first.
   Explicit paths consume the shared read budget first; reserve up to five
   reads for attachment candidates before spending the remainder on general
   research.
2. Inspect changed or untracked eligible document files reported by Git.
3. Search only task-context paths and conventional documentation locations for
   semantically relevant documents.

Before reading, listing, or searching any discovery root supplied by task
context or selected from a conventional location, require its lexical input to
be repository-relative, resolve it against the resolved repository root, and
require the result to be an existing directory inside the resolved repository root.
Do not traverse a rejected root, including an absolute root, a missing or
non-directory root, traversal outside the repository, or a symlink that
resolves outside the repository. Report the applicable attachment-discovery-root
error from Path normalization and validation before continuing with other roots.

Discovery ends at **five qualified candidates**, not five files considered.
If the read or search cap is reached first, disclose incomplete discovery and
the skipped source; do not extend the 20-read/10-search cap.

Never search or accept paths in `.git`, dependency/vendor trees, generated
artifacts, archive directories or archived documents, or linked worktrees.
Do not follow a symlink that resolves outside the repository. Tracked and
untracked regular files are eligible when they pass all validation rules.

## Qualification and relevance

Treat these as first-class Plan candidates when they are relevant to the TODO:

- a finalized gstack office-hours or plan-eng-review document;
- a Superpowers implementation plan.

A document may also qualify semantically as a Plan when it contains ordered
implementation work, concrete change targets, and verification or acceptance
steps. Do not qualify a document merely because of its filename, recency,
location, or author.

Strong relevance signals are, in descending order: an explicit TODO ID or
task context, close title/summary scope, and substantial overlap with concrete
change targets. Record the strongest applicable signal in `relevance_reason`.

Classify each qualified document by role:

- use **Plan** for executable implementation instructions;
- use **Spec** for the authoritative expected outcome or acceptance contract;
- use **Reference** for supporting context that is not authoritative;
- assign both **Plan** and **Spec** when one document genuinely provides both.

One document may serve as both Plan and Spec. Do not duplicate its candidate
record; list both roles in `roles`.

## Path normalization and validation

Each candidate path must be an existing regular file inside the repository.
Validate every supplied or discovered path with this normative algorithm:

```text
resolve candidate against repository root
reject unless the lexical input is relative
reject unless the resolved target is inside the resolved repository root
reject unless the target exists and is a regular file
store target.relative_to(repository_root).as_posix()
```

The stored value is a repository-relative POSIX path. Reject absolute input,
missing paths, directories, traversal that resolves outside the repository,
and an outside-target symlink. A symlink whose resolved regular-file target is
inside the repository is stored as the resolved repository-relative POSIX path.

`Reference` is a comma-separated list of normalized paths. At candidate
validation, when one detected or explicitly supplied filesystem path is known
to be a single path, reject it if that path contains a literal comma. There is
no escaping syntax. In a stored `Reference:` value every comma is
unconditionally a separator: split on every comma, trim each item, reject an
empty item, and validate each non-empty item as a separate path. Audit must
never infer that two stored items were one literal-comma path. `Plan` and
`Spec` each contain one normalized path only.

Use these exact validation errors and remediation text:

```text
Error: Attachment path must be repository-relative.
Remediation: Enter a path relative to the repository root.

Error: Attachment path resolves outside the repository.
Remediation: Choose a file inside the repository root.

Error: Attachment path does not exist or is not a regular file.
Remediation: Choose an existing document file.

Error: Reference path contains a comma.
Remediation: Use one comma-separated Reference path per value; rename paths containing commas.

Error: Attachment discovery root must be repository-relative.
Remediation: Enter a directory path relative to the repository root.

Error: Attachment discovery root resolves outside the repository.
Remediation: Choose a directory inside the repository root.

Error: Attachment discovery root does not exist or is not a directory.
Remediation: Choose an existing directory inside the repository root.
```

## Candidate records and ambiguity

For every qualified candidate, retain:

```text
{path, roles, relevance_reason, source, validation}
```

`source` is `explicit`, `git changed or untracked`, or `bounded search`.
`validation` records the normalized-path result. Present the records in the
combined attachment confirmation, with their role classification and
relevance reason.

For each role, report one of these states:

- `none detected` when no qualified candidate exists;
- `suggested` when exactly one qualified candidate exists;
- `unresolved` when more than one candidate could fill the role;
- `preserved` when an existing value remains unchanged.

Never silently select a suggested candidate, resolve ambiguity, or rewrite an
existing value. Put Plan, Spec, and Reference paths and states into the
existing synthesis confirmation alongside the ordinary TODO fields. The
`--add` and `--revise` confirmation gate must present all roles together,
including `Plan: none detected` when applicable, and require explicit user
confirmation. Carry the confirmed attachment rows into the subsequent full-entry preview,
where the user can edit or cancel them before writing.

## Existing-value preservation

Existing `Plan` and `Spec` values are retained unless the user explicitly
replaces or removes them. Existing `Reference` values are retained; append
validated new Reference paths, deduplicate the normalized values, and preserve
their existing order. Replacing or removing any attachment value requires an
explicit user instruction. A candidate may be offered for any role, but it
never overwrites a value solely because it was discovered.
