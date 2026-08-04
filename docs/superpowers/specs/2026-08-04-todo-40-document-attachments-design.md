# TODO-40 Document Attachment Design

## Outcome

`todos-manager --add` and `todos-manager --revise` help users attach repository documents to a TODO as `Plan:`, `Spec:`, or `Reference:` without guessing or writing before confirmation.

`Plan:` is the execution authority and the sole field that makes a TODO pipeline-actionable. `Spec:` is the authoritative outcome contract. `Reference:` is supplementary context. A single document may serve as both Plan and Spec when it genuinely fulfills both roles, but it must not also be duplicated as a Reference.

TODO-40 owns document discovery, classification, validation, confirmation, schema documentation, and attachment mutation. TODO-39 owns selection eligibility, pipeline prompt consumption, worktree creation, and execution behavior.

## Candidate discovery

Discovery is bounded to the current repository and uses this order:

1. Paths explicitly named by the user, invoking skill, or current task context.
2. Changed or untracked documentation files reported by Git.
3. Strongly related files in conventional documentation locations, including `docs/gstack/**` and `docs/superpowers/plans/**`.

Modification time is not evidence that a document belongs to the current session. Archives, generated artifacts, dependencies, `.git`, and other linked worktrees are excluded.

The complete research phase retains the existing cap of 20 file reads and 10 searches. Up to five reads are reserved for attachment candidates, explicitly supplied paths are inspected first, and discovery stops after five qualified candidates. Budget exhaustion is reported and falls back to manual entry or no attachment.

## Qualification and relevance

Finalized gstack documents produced by office-hours or plan-eng-review and Superpowers implementation plans are first-class Plan candidates. Other documents qualify as Plans only when they contain:

- ordered or dependency-aware implementation work;
- concrete files, modules, interfaces, or commands to change; and
- verification or acceptance steps.

A candidate must also have a strong relationship to the TODO through an explicit user reference, the TODO ID, a close subject-and-scope match, or substantial overlap with implementation targets found during auto-research. Recency, directory, author, and filename affect ranking only.

Documents are classified by their strongest role: executable work as Plan, requirements or acceptance contracts as Spec, and background or supporting material as Reference. If a document strongly fulfills both Plan and Spec, the user is offered an explicit combined-role choice. Generic keyword overlap is insufficient.

## Path contract

Every detected or manually entered attachment path must:

- name an existing regular file;
- resolve inside the current repository without symlink escape;
- be stored as a normalized repository-relative POSIX path; and
- reject absolute paths, directories, missing files, and traversal outside the repository.

Tracked and untracked files are allowed. Spaces and punctuation are allowed. `Plan:` and `Spec:` contain one path each; `Reference:` contains a comma-separated ordered list of paths, so a Reference path cannot contain a literal comma.

Validation failures identify the path and exact defect, then return to attachment selection without rerunning research.

## Interaction contract

Both commands use their existing consolidated synthesis and preview gates. No attachment is written silently.

For `--add`:

- zero Plan candidates shows `Plan: none detected` without another prompt;
- one candidate is shown as a suggestion, not silently accepted;
- multiple candidates are listed with short relevance reasons and remain unresolved;
- the user selects a candidate, enters another valid path, or explicitly chooses `none`; and
- `confirm` is rejected while an ambiguity remains unresolved.

For `--revise TODO-N`:

- explicit session paths rank first, followed by Git-changed documents and bounded fallback search;
- candidates may be proposed for Plan, Spec, and Reference;
- existing valid values remain selected by default;
- replacing Plan or Spec, removing any attachment, or replacing an invalid existing value requires an explicit edit;
- References append in existing order and deduplicate normalized paths; and
- invalid existing paths produce warnings but do not block unrelated revisions.

The user may confirm a combined Plan-and-Spec attachment. The same path is never duplicated under Reference.

## Compatibility and completion

Existing TODO entries without `Plan:` remain schema-valid. A Spec alone does not make a TODO actionable. `--audit` recognizes and path-validates attachments when present but does not require them or repair them automatically.

Completion requires executable coverage for zero/one/multiple candidates, manual and omitted attachments, add and revise flows, combined roles, preservation/replacement/removal, Reference deduplication, discovery precedence and limits, path containment and symlink escape, recognized and fallback document formats, no-write-before-confirmation, ambiguity handling, legacy entries, and packaged/installed skill parity.

