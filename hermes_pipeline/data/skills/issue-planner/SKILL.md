---
name: issue-planner
description: Research a finalized Codex or Claude implementation plan and publish reviewed Small/Medium GitHub issues, preserving split goals with a parent and final validation child. Use when a finalized plan should become executable backlog work.
---

# Issue Planner

Plan, review, publish, and hand off issues. This skill does not implement the
issues, manage later PRs, merge deliveries, or close their parent. New installs
use `issue-planner`; newly installed/reinstalled `todo-manager` is a deprecation
notice. Existing installed copies keep their old behavior until explicitly
upgraded. Preserve their recovery resources, receipts, journals, and requests.

## Establish the source and delivery

Accept the latest finalized Codex `<proposed_plan>...</proposed_plan>` in the
active conversation, or a Claude Plan-mode artifact explicitly identified in
that conversation and readable now. Record its provenance and content hash.
Strip outer Codex tags from submitted Markdown. Reject drafts, summaries,
quoted examples, inaccessible history, and raw requests. Without a finalized
source, stop and instruct the user to finalize a plan first.

Research current repository code, policy, tests, and related issues. Refine
tasks and split the plan as needed while preserving the original intended
outcome. Resolve any change of intent with the user before review; never
silently drop a requirement. Never retain secrets, credentials, authorization
data, provider bodies, or raw sensitive tool output in requests or evidence.
If the source contains secrets, redact them and stop for user direction.

Resolve literal project slug, canonical `OWNER/REPO`, default branch, and all
delivery branches. **Select and confirm the PR strategy before issue review.**
Read [group and delivery contracts](references/group-delivery.md) when splitting
a goal or choosing integration delivery. Incremental delivery is the default.
Integration delivery requires all executable children to remain held for manual
handoff because current TPO closeout requires the default PR base. Permission
for manual execution never makes that unsupported strategy schedulable by TPO.

## Build a complete packet

Small is a localized outcome; Medium is a bounded coherent deliverable with
independent verification. Split Large work vertically, retaining correctness
tests with their implementation. An inevitably atomic Large executable issue
needs a specific explanation of why splitting is unsafe and explicit user
permission for that exception. A non-executable parent's aggregate scope is
not a Large executable issue.

An unsplit plan becomes one implementation issue with its own goal validation;
it needs no parent. A split plan requires one ordinary non-executable parent,
self-contained implementation children, and exactly one terminal final
validation child. Follow the group reference for coverage, native
relationships, prerequisite checks, and parent completion conditions.

Each child includes context, prerequisite interfaces, implementation tasks,
acceptance criteria, tests, verification commands, and commit messages. Record
literal repository, branches, PR bases, prerequisites, merge order, required
checks/reviews, and final-delivery ownership in the parent and every child Plan.
Use stable group keys and titles in child bodies. Resolve actual parent numbers
from verified native relationships when preparing PRs later; do not mutate an
approved child's body to insert a newly created parent number.

Create each executable request with schema-v1 `schema_version`, canonical
lowercase UUIDv4 `transaction_id`, `title`, `fields`, `plan_markdown`, `tasks`,
and `hold: true`. Each task has exactly `id`, `title`, `instructions`,
`acceptance_criteria`, `verification`, and `commit_message`. Research and fill
`Summary`, `What`, `Why`, `Pros`, `Cons`, `Context`, `Assumptions`, `Spec`,
`Reference`, `Branch`, `Priority`, `Effort`, `Phase`, `Test Coverage`,
`Security Review`, and `UI Review`. Do not add a `Plan` field, `Legacy ID`,
labels, issue numbers, or TODO IDs to requests. The CLI renders the Plan
manifest. `hold` is a strict top-level boolean; omitted/false retains legacy
canonical bytes, true is previewed without changing rendered issue Markdown.

Use bundled `scripts/write_request.py PROJECT_ROOT UUID` with JSON on standard
input to create private immutable requests. Never replace an existing request.
Read [review and publication](references/review-publication.md) before reviewing
or publishing, and [batch record format](references/batch-record.md) before
writing the approved record. Every publication, including a single issue,
requires an immutable approved batch record before any remote creation.

## Review, approval, publication, handoff

Obtain fresh independent Codex and Claude read-only reviews of the complete
packet, with separate verdicts for the parent and every child. Follow the
reference's packet-digest binding, ten-minute deadlines, maximum three
review/fix rounds, and unavailable-engine rules. Author review does not count.

Show complete canonical parent/child previews, hierarchy, dependencies,
reviews, sizing exceptions, confirmed strategy, and release versus manual-only
status. Obtain the exact reply `create` for that packet. Plan approval and
existing files are not publication permission. Changes invalidate approval
and substantive changes invalidate reviews too.

Follow the publication reference in order: approved private batch record,
parent creation/recovery, held CLI children, native relationship/dependency
verification, full remote readback, then incremental hold release only. Keep
all requests and records through partial failures and reconciliation. Never
delete or recreate a partial issue. Integration groups release no children.

Finish with issue URLs/keys, hierarchy, verified edges, release/hold status,
review limitations, retained record paths, and manual next steps. Include the
prerequisite-delivery and parent-closure contracts in the handoff; later merges
and parent closure require their own authorization. Rollback preserves batch
records and held resources for reconciliation; it does not erase published
history or bypass runtime base verification, `origin/HEAD`, or completion gates.
