# Document Attachment Discovery and Validation

## Purpose

The fenced JSON block below is the deterministic policy consumed by the
provider-free contract harness. Keep it authoritative and synchronized with
the human-readable rules that follow.

```json todos-manager-attachment-policy
{
  "version": 1,
  "read_limit": 20,
  "search_limit": 10,
  "candidate_limit": 5,
  "sources": ["explicit", "git changed or untracked", "bounded search"],
  "confirmation": {"zero": "none", "one": "explicit-selection", "multiple": "explicit-selection"},
  "reference_separator": ",",
  "fields": ["Plan", "Spec", "Reference"],
  "excluded_parts": [".git", ".worktrees", "archive", "archives", "dist", "build", "generated", "node_modules", "vendor"],
  "relevance": ["explicit", "todo-id", "close-scope", "concrete-target-overlap"],
  "close_scope": {
    "minimum_specific_term_overlap": 2,
    "generic_terms": ["acceptance", "change", "changes", "document", "documents", "implementation", "plan", "planning", "review", "scope", "spec", "task", "tasks", "test", "tests", "todo", "update", "updates", "verification", "verify", "work", "with", "from", "that", "this", "into", "using"]
  },
  "plan_authoring": {
    "task_limit": 50,
    "task_id_format": "task-%02d",
    "evidence_locator_kinds": ["plan_lines", "repository_lines", "git_commit"],
    "evidence_digest": "sha256",
    "candidate_mode": "0600",
    "existing_target_mode": "preserve",
    "new_target_mode": "0644",
    "validator": ["tpo", "plan", "validate", "<project>", "--todo", "TODO-N", "--plan", "<candidate>", "--require-manifest"]
  },
  "errors": {
    "absolute": "is absolute, not repository-relative",
    "outside": "resolves outside the repository",
    "symlink_outside": "is a symlink that resolves outside the repository",
    "missing": "does not exist",
    "directory": "is a directory, not a regular file",
    "not_regular": "is not a regular file",
    "empty_reference": "contains an empty path between separators"
  }
}
```

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
Count searches by invocation, not by returned path. If one search invocation
returns several paths, it consumes one of the ten searches. If the read or search cap is reached first, disclose incomplete discovery and
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
change targets. A generic subject substring alone is not strong relevance.
For close-scope matching, lowercase the title and summary terms of at least
four characters, discard every term listed in `close_scope.generic_terms`, and
require at least `close_scope.minimum_specific_term_overlap` distinct remaining
terms to occur in the candidate path or content. Generic planning vocabulary
never contributes to the threshold, even when several generic terms overlap.
Record the strongest applicable signal in `relevance_reason`.

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
empty item, and continue validating each non-empty item as a separate path. Audit must
never infer that two stored items were one literal-comma path. `Plan` and
`Spec` each contain one normalized path only.

### New Plan target reservation

Existing attachment candidates remain existing regular files only under the
algorithm above. A user-approved new Plan target is the sole exception, and is
validated as a new Plan target reservation rather than as an attachment
candidate. Require its lexical path to be repository-relative and contained
inside the resolved repository root. Reject an existing target. Require the
nearest existing parent to be an existing, contained, non-symlink directory;
if intermediate directories are intended, create them one at a time while
rechecking that each parent remains contained and is not a symlink. Record that
the target is absent as its preimage, reserve only that one normalized path,
and recheck that absence immediately before atomic creation. A concurrently
created target fails closed without replacement. These rules do not make other
missing Plan, Spec, or Reference candidates eligible.

Use these exact validation errors and remediation text:

```text
Error: Attachment path must be repository-relative.
Remediation: Enter a path relative to the repository root.

Error: Attachment path resolves outside the repository.
Remediation: Choose a file inside the repository root.

Error: Attachment path does not exist.
Remediation: Choose an existing document file.

Error: Attachment path is a directory, not a regular file.
Remediation: Choose an existing document file.

Error: Attachment path is not a regular file.
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

Never silently select a suggested candidate, including during a plain
`confirm`; require an explicit candidate selection or `none`. Never resolve ambiguity or rewrite an
existing value. Put Plan, Spec, and Reference paths and states into the
existing synthesis confirmation alongside the ordinary TODO fields. The
`--add` and `--revise` confirmation gate must present all roles together,
including `Plan: none detected` when applicable, and require explicit user
confirmation. Carry the confirmed attachment rows into the subsequent full-entry preview,
where the user can edit or cancel them before writing.

## Plan execution readiness and manifest authoring

Validate the normalized Plan through the packaged deterministic CLI contract
after AI research and Plan selection:

For an unchanged Plan on an existing TODO, invoke
`tpo plan validate <project> --todo TODO-N`. If it reports `manifest`, preserve
and attach the Plan byte-for-byte unchanged. Newly selected, created, or
replacement Plans must finish this section in `manifest` state. A `legacy`
Plan may be selected only through the authoring sequence below; `invalid`
requires correction or another selection. An explicit `none` remains
non-actionable.

The selected-path readiness form is
`tpo plan validate <project> --todo TODO-N --plan <normalized-path>`; authoring
replaces the normalized path with the staged candidate and adds the strict flag:
candidate mode validates before the candidate is persisted.

```text
tpo plan validate <project> --todo TODO-N --plan <candidate> --require-manifest
```

Use the TODO ID assigned to `--add` or selected by `--revise`, and run the
command from the target project. Do not parse or reproduce the `tpo-plan`
schema in skill prose or helper logic. The packaged validator is authoritative.
Classify its result for display as exactly one of:

- `manifest`: validation succeeds and reports a valid `tpo-plan` manifest;
- `legacy`: validation succeeds with the no-manifest warning;
- `invalid`: validation fails for the selected Plan or its manifest.

Show `Plan readiness: <state>` with the validator's bounded diagnostic in the
combined synthesis and again in the full-entry preview. `invalid` blocks preview
and requests only a corrected Plan selection or value; it does not rerun
attachment discovery or AI research. If Plan is explicitly resolved as `none`,
omit the readiness row and retain the existing non-actionable TODO behavior.
Readiness validation never selects a candidate, derives ordinary TODO fields,
or changes the user's confirmation authority.

### Evidence and proposal contract

Author exactly one selected TODO and one selected Plan; never bulk-mutate Plans
or TODOs. First obtain explicit approval of the human Plan snapshot that will
be used as the source. Existing valid manifests are a byte-for-byte no-op and
must not be regenerated.

For a new or legacy Plan, draft a strict `json tpo-plan` block containing one
to 50 tasks with deterministic ordered IDs `task-01` through `task-50`. Keep an
out-of-band provenance map; provenance is never embedded in the manifest.
Every title, instructions, every acceptance criterion, every verification
command, and commit message must be supported by at least one typed locator to
approved Plan lines, repository-file lines, or exact Git commits. Each locator
records its source and SHA-256 digest; line locators also record inclusive line
bounds, and commit locators record the full commit object ID. Re-read every
locator and verify its digest before proposal. If any field or list item lacks
support, or its locator is missing, stale, outside the repository, or not the
exact commit requested, return `insufficient_evidence` without staging or
changing either target.

### Ordered authoring state machine

Perform these steps in order:

1. Obtain explicit approval of the human Plan snapshot and record both Plan and
   TODO preimages.
2. Draft only evidence-supported manifest fields and the out-of-band provenance
   map. Do not infer unsupported boundaries, criteria, commands, or messages.
3. Stage the candidate in the Plan's directory as a same-directory staged candidate
   with mode `0600`; do not expose it as the selected Plan. Invoke
   exactly `tpo plan validate <project> --todo TODO-N --plan <candidate>
   --require-manifest` from the target project through the packaged command
   boundary.
4. Show the exact unified Plan diff and obtain explicit diff confirmation.
5. Show the final TODO preview with the selected Plan path.
6. Recheck the Plan and TODO preimages, the validated candidate's identity and
   digest, and that no concurrently created target now occupies a new path.
7. Obtain final TODO approval. Then atomically replace or create the Plan,
   preserving its path and content outside the confirmed diff; preserve the
   existing Plan mode, or use mode `0644` for a new repository document. Only
   after the Plan succeeds, write the TODO through its existing atomic write
   contract.

Cancellation, insufficient evidence, validator failure, rejected diff, Plan
or TODO drift, candidate drift, or a concurrently created target must delete
the staged candidate and leave both paths byte-for-byte unchanged. Preserve
the original Plan path and mode. If the separately confirmed Plan installs but
the TODO write then fails, report that partial outcome explicitly; do not hide
it by rolling the Plan back.

This intake readiness is not runtime qualification. Before execution, run
`tpo doctor <project>` to verify Hermes >= 0.19.0, installed project-skill parity,
and the current manifest/legacy/invalid readiness counts. TPO later requires
the selected TODO and Plan bytes to be tracked at the pinned base commit; the
skill must not claim that a successful candidate check proves that
Git authority or live Kanban state.

## Existing-value preservation

Existing `Plan` and `Spec` values are retained unless the user explicitly
replaces or removes them. Existing `Reference` values are retained; append
validated new Reference paths, deduplicate the normalized values, and preserve
their existing order. Replacing or removing any attachment value requires an
explicit user instruction. A candidate may be offered for any role, but it
never overwrites a value solely because it was discovered.
