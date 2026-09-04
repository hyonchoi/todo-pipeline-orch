# Pipeline State Machine

Kanban closing-run state is authoritative. Local state records immutable
registration and crash-recovery evidence, never a second mutable workflow.

| Trigger | Guard | Transition |
|---|---|---|
| Hermes cron or manual `tpo tick` | no active project run | compile eligible TODOs |
| TODO selected | issue snapshot and embedded Plan pinned, or legacy path tracked at base SHA | write schema-v3 `.hermes/runs/<tick-id>/registration.json` and verified embedded `plan.md` artifact; create/reuse exact linked worktree |
| manifest compiled | <=50 ordered tasks | register one `worker` card per task, chained, with stable keys |
| legacy Plan compiled | valid Markdown, no manifest | register one development worker and warn |
| worker closes | valid sanitized result metadata and Git facts | chain may advance; the chain tip is also verified against HEAD and a clean worktree |
| worker evidence invalid | immutable mismatch or unsafe Git state | tick reports no progress and logs the bounded diagnostic; no card is blocked |
| initial review finds issues | unchanged head and clean worktree | create one `review-fix -> fix-validation -> re-review` round |
| fifth re-review still finds issues | every round card terminal | review gate remains human `needs_input`; create no cards |
| review is clean | review evidence validates | allow finish and closeout |
| finish and closeout validate | PR branch/head/checks match | human merge gate remains `needs_input` |
| GitHub reports merge | PR identity still matches | complete terminal gate; later selection allowed |

Exactly one run may be active per project; a multi-project cron scan reconciles
projects independently. Stable idempotency keys make retries converge on the
same cards.

## Recovery boundary

Authority-hash drift, a mismatched or dirty worktree, unexpected PR closure,
branch deletion, force-push, or remote-head drift is never repaired
automatically. TPO never resets, cleans, deletes, force-pushes, merges, or
abandons those resources. It reports expected and observed state and preserves
the run behind `needs_input` for an operator.
