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
