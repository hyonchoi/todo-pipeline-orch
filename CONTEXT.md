# Todo Pipeline Orchestrator

Language for describing work items as they move from backlog definition to pipeline execution.

## Language

**TODO**:
An open GitHub issue on the project's github.com origin carrying the `tpo:todo` label. Its canonical ID is `TODO-<issue-number>`. A TODO becomes pipeline-actionable only through a Plan (see **Plan** below).

**Legacy ID**:
A pre-migration `TODO-<n>` identifier from the retired `TODOS.md`, preserved as a `legacy-id:TODO-<n>` label, a `### Legacy ID` body section, and in `docs/migration/todos-to-issues.md`. It is never a live issue number.

**Issue Snapshot**:
The identity-bound canonical text of a TODO (`tpo-issue-snapshot/1`: repository, number, title, normalized body) captured at selection and hashed into the run registration as the selected-TODO authority. Labels, assignees, state, and dependencies are live, not part of the snapshot.

**Plan**:
A repository document containing implementation-ready execution steps for a TODO. It is the execution authority and makes the TODO pipeline-actionable; design explorations, reviews, specifications, references, and test plans do not qualify unless they explicitly define executable implementation steps.

**Plan Candidate**:
A potential Plan discovered from an explicit task-context path or a bounded search of conventional documentation locations in the current repository. Finalized gstack documents from office-hours or plan-eng-review and Superpowers implementation plans are first-class candidates; other documents may qualify when they contain ordered implementation work, concrete change targets, and verification steps. A candidate may be tracked or untracked and excludes archives, generated artifacts, dependencies, and other linked worktrees.

**Relevant Plan Candidate**:
A Plan Candidate connected to a TODO by an explicit user reference, the TODO ID, a close subject-and-scope match, or substantial overlap with the implementation targets found during auto-research. Recency, location, authorship, and filename affect ranking but do not establish relevance.

**Spec**:
A document defining the authoritative outcome contract for a TODO. It does not make the TODO pipeline-actionable, but an implementation executed from a Plan must satisfy it when present.

**Reference**:
Supporting context for a TODO that neither authorizes execution nor defines its authoritative outcome.

**Document Attachment**:
A user-confirmed association between a TODO and an existing repository document in the role of Plan, Spec, or Reference. One document may serve as both Plan and Spec when it genuinely contains both execution instructions and the authoritative outcome contract, but it is not duplicated as a Reference. Attachments may be proposed during TODO creation or revision, but are never made silently and never replace an existing attachment without explicit approval.
