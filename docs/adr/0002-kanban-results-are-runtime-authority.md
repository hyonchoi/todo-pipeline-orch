# Kanban results are runtime authority

Hermes Kanban owns live card state, summaries, and structured
`metadata.tpo_result`. TPO owns deterministic compilation and reconciliation:
it validates bounded metadata and Git facts before completing unassigned
controller gates.

Local `.hermes/runs/<tick-id>/registration.json` state is immutable registration
and crash-recovery evidence, not a competing workflow database. Stable
idempotency keys make cron retries converge. Drift, the five-round review
breaker, and the final merge are human `needs_input` boundaries; TPO does not
perform destructive Git recovery or automated merge.

## Amendment: the review breaker is superseded

The "five-round review breaker" clause above no longer describes the runtime and
is retained only as the record of what this ADR decided.

Bounded review remediation was removed when the phase profile became the
specification for the review card. `MAX_REVIEW_ROUNDS`, `_ensure_round`,
`_ensure_rereview` and the `review-fix:<n>` / `re-review:<n>` /
`fix-validation:<n>` cards are gone: the profile's own `phase_5_review` reviewer
applies its findings as one review-fix commit, so review is binary — a `done`
review card is the pass and a `blocked` one is the reviewer's own nonzero exit.
There is no round counter left to break.

The other two boundaries in that clause stand. Drift is still a human
`needs_input` boundary, and the final merge is still human: TPO performs no
destructive Git recovery and no automated merge. What replaced the breaker is
not a gate but a terminal card status; see
[howto-review-outcomes.md](../howto-review-outcomes.md) for what a `blocked`
review card actually causes in each context.
