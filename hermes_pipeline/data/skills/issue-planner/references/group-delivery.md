# Group and delivery contracts

Read for every split goal and integration strategy. Build the whole group
before review; size and review the final validator like any other child.

## Parent and coverage

Create one ordinary parent recording the original goal, scope, non-goals,
acceptance criteria, source-plan provenance, requirement-to-child/test mapping,
delivery strategy, and completion conditions. Exclude `tpo:todo` and
`ready-for-agent`. Give it neither an executable branch nor a Plan manifest.
Record delivery branches as descriptive strategy information only.

Attach children using native GitHub sub-issues. Execution order uses separate
native dependency edges between executable children; never make the parent
an executable blocker. Every required criterion must map to implementation
and verification evidence. Each child must stand alone for an agent without
the original conversation, including prerequisite interfaces and promised
behavior. Avoid overlapping ownership and contradictory task/test contracts.

Require exactly one terminal `final-validation` child that directly depends
on every required implementation or scoped-validation child. It verifies
every parent acceptance criterion against the combined result, including
relevant cross-child behavior, compatibility, migrations, documentation, and
release readiness. If this exceeds Medium, add bounded scoped-validation
children first; the terminal assessment remains Small/Medium. Its delivery PR
must record tested code SHA, exact checks and outcomes, criterion coverage,
and meaningful integration evidence. Distinguish tested code from later
report-only commits; closed child counts or a report file alone prove nothing.

## Strategy confirmed before review

| Strategy | Required delivery contract |
| --- | --- |
| Incremental (default) | Each child safely merges to the literal default branch. Independent children may run in parallel. Dependent children start from an updated base only after prerequisite delivery verification. The final validator delivers to the default branch. |
| Integration | Every child PR targets a literal group branch; all executable issues remain held and manual-only. The final validator uses its own branch for validation changes into the group branch, then owns preparation of one group-to-default integration PR. Subsequent merges need separate authorization. |

Record repositories, exact branch names and PR bases, prerequisites, merge
order, verification requirements, and final-delivery owner in every Plan and
parent. Do not use placeholders in the reviewed packet. Do not change TPO
base validation or `origin/HEAD`, force completion, or release integration
children after a user accepts manual execution.

Before dependent implementation, verify prerequisite PRs actually merged to
the intended base, their merge commits are reachable from that base, and their
promised behavior is still present. Closed-but-unmerged, reverted, or
unverifiable prerequisites stop implementation. Issue closure alone is not
evidence. Require applicable checks and reviews after PR head or base changes;
resolve conflicts with behavior verification. Preserve repository protections
and separate human merge authorization.

All child PRs reference the parent **without closing keywords**. Closing
references may target only the corresponding child where appropriate. Neither
child PRs nor the final integration PR automatically close the parent.

## Completion and later changes

Parent remains open until every required delivery is verified, final
validation is delivered to the intended target branch, every original
criterion has evidence, and an authorized coordinator explicitly closes it.
Closed, canceled, reverted, or merely PR-open children do not establish goal
completion. Failed integration means the original goal remains incomplete;
missing evidence is UNVERIFIED. This skill hands off these conditions; it
does not add a PR-management or merge-automation mode.

Post-publication rescoping requires manual reconciliation with the existing
parent, its dependencies, and original requirements. Do not create another
parent for the same goal. Preserve provenance and held resources on rollback.
