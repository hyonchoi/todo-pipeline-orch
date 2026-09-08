# Pipeline State Machine

Kanban closing-run state is authoritative. Local state records immutable
registration and crash-recovery evidence, never a second mutable workflow.

| Trigger | Guard | Transition |
|---|---|---|
| Hermes cron or manual `tpo tick` | no active project run | compile eligible TODOs |
| TODO selected | issue snapshot and embedded Plan pinned, or legacy path tracked at base SHA | write schema-v3 `.hermes/runs/<tick-id>/registration.json` and verified embedded `plan.md` artifact; create/reuse exact linked worktree |
| manifest validated | <=50 ordered tasks | register ONE `phase_4_development` card carrying that phase's own prompt, tools, turns and timeout; the task count changes no card |
| legacy Plan validated | valid Markdown, no manifest | register the same card, publishing no result template: its result is never parsed |
| implementation card closes | valid sanitized result metadata, every Plan task's acceptance criteria, and exactly `len(tasks)` first-parent commits from the pinned base SHA whose real diff matches `changed_files` | chain may advance; while it is still the chain tip it is also verified against HEAD and a clean worktree |
| card evidence invalid | immutable mismatch, wrong commit count, or unsafe Git state | tick reports no progress and logs the bounded diagnostic; no card is blocked |
| review card closes `done` | reported head is the implementation head or one commit past it, and descends from it | record `accepted-review-head` and allow finish |
| review card closes `blocked` | the profile's own nonzero exit | Hermes keeps it sticky and both readers treat it as terminal, but they disagree on what that means: the harness poller fails the run, while the production tick counts `blocked` as complete, releases the project and selects the next TODO -- `observe_outcomes` does record a `failed_at_phase_<key>` outcome naming the blocked phase, so the abandonment is no longer silent (see [howto-review-outcomes.md](howto-review-outcomes.md)) |
| review evidence invalid | reported parent, diff, or reachability mismatch | tick reports no progress and logs the bounded diagnostic; no head is accepted |
| finish and closeout validate | PR branch/head/checks match | the open, unmerged pull request and its human merge decision are the run's terminal boundary; `phase_9_human_review` is a gate phase, so no card represents it |
| GitHub reports merge | PR identity still matches | the run's terminal boundary is complete; later selection allowed |

Exactly one run may be active per project; a multi-project cron scan reconciles
projects independently. Stable idempotency keys make retries converge on the
same cards.

## Recovery boundary

Authority-hash drift, a mismatched or dirty worktree, unexpected PR closure,
branch deletion, force-push, or remote-head drift is never repaired
automatically. TPO never resets, cleans, deletes, force-pushes, merges, or
abandons those resources. It reports expected and observed state and preserves
the run behind `needs_input` for an operator.
