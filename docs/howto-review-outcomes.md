# How to Interpret Review Outcomes

Native SDD review is a distinct Hermes Kanban worker session that runs the
phase profile's own `phase_5_review` prompt. Under that prompt the reviewer is
not read-only: it applies every valid finding, runs the relevant focused checks,
and commits the fixes as one review-fix commit. If a material finding cannot be
resolved safely it exits nonzero and leaves no uncommitted changes.

So the outcome is binary and it is the card's own status:

- `done` means the review passed -- fixes applied, or none were needed. TPO
  records `.hermes/runs/<tick-id>/accepted-review-head` as the head the review
  left behind and opens the finish card.
- `blocked` is the profile's nonzero exit. Hermes makes it sticky, so the card
  will never move again. What happens next depends on who is reading the board,
  and the two readers do **not** agree:

  - **The live harness poller stops.** `classify_pinned_run`
    (`hermes_pipeline/harness.py`) returns `"failed"` if *any* card status is
    `blocked`, so `tpo test` ends the run and reports the failure. This is the
    only context in which a blocked card halts anything.
  - **The production cron does not stop; it moves on.**
    `all_phases_complete` (`hermes_pipeline/kanban_tasks.py`) counts `blocked`
    as a completion status, so the prior tick reads as finished, the project is
    released, and the same scan goes on to select the next eligible TODO. The
    reconcilers do not object either: a review card that is not `done` makes
    `reconcile_reviews` return "nothing to do". `observe_outcomes` does now
    record the stop — it writes `failed_at_phase_<key>` with
    `kanban_status: "blocked"`, the same vocabulary a `failed` card gets, so the
    decision store names the phase the run stopped at. What it does not do is
    change the circuit breaker's counter: the classifier reads
    `phase_complete` before it reads a failure, so a run whose earlier phases
    completed still counts as progress — exactly as it does for a `failed`
    card. The blocked run's branch and linked worktree are still left behind,
    with no pull request, no issue comment and no alert naming them.

  So manual repair is outside the lifecycle in the sense that nothing repairs it
  — not in the sense that the pipeline waits. Under cron, a blocked review is
  abandonment that is recorded but not alerted on, and the leftovers are found
  only by looking for them (`hermes kanban list --tenant <project>`,
  `git worktree list`) or by reading the tick's outcome file for a
  `failed_at_phase_*` line.

There is no verdict or findings object in `metadata.tpo_result` and there are no
remediation rounds: the reviewer fixes what it finds, so there is nothing to fan
out into `review-fix` / re-review cards.

TPO does not take the reported head on trust. Before accepting it, the review's
reported parent must be the implementation head TPO recomputed from the Plan
task chain, the reported head must descend from it by at most one commit, that
commit's real diff must match the reported `changed_files`, and the head must be
reachable from the branch HEAD. A mismatch reports no tick progress and accepts
no head.

Inspect the board with `hermes kanban show <task-id> --json`. Treat reported TDD
commands as worker-reported evidence; TPO independently checks Git topology,
not the truth of external test execution.

See [Kanban as scheduler](reference-kanban-as-scheduler.md) and
[debugging and recovery](howto-debugging-and-recovery.md).
