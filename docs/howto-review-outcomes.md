# How to Interpret Review Outcomes

Native SDD review is a distinct, read-only Hermes Kanban worker session. It
closes with a bounded `metadata.tpo_result.review` value:

- `clean` opens the persistent review-acceptance controller gate.
- `findings` contains at most 50 structured P0-P3 findings, each with a changed
  code location, concrete failure scenario, and bounded recommendation.

TPO validates the review identity, pinned head, and clean linked worktree. It
does not trust a prose summary as machine evidence and never stores raw provider
output. Invalid or mismatched metadata leaves the controller gate
`needs_input`.

## Remediation rounds

One findings result creates one stable round barrier followed by `review-fix`,
fix-validation, and re-review cards. Cron retries reconcile those same cards;
they do not create duplicate rounds. Only a validated clean re-review opens the
acceptance gate.

After five unsuccessful fix rounds TPO creates no more cards. Existing cards
must be terminal and the review-acceptance gate remains `needs_input` with a
sanitized findings summary. Manual repair and resume are intentionally outside
the automated lifecycle.

Inspect the board with `hermes kanban show <task-id> --json`. Treat reported TDD
commands as worker-reported evidence; TPO independently checks Git topology,
not the truth of external test execution.

See [Kanban as scheduler](reference-kanban-as-scheduler.md) and
[debugging and recovery](howto-debugging-and-recovery.md).
