# How to Use the Native SDD Profile

The `native-sdd` profile is the Plan-to-Kanban compiler. The production flow is
`Hermes cron -> TPO tick -> eligible TODO -> pinned worktree -> Kanban ->
worker`. Hermes >= 0.19.0 dispatches workers; TPO never invokes Claude or Codex
directly.

It is the default profile for every new contract `tpo init` writes
([ADR-0004](adr/0004-native-sdd-is-the-default-phase-profile.md)); `gstack` is
deprecated but still bundled and supported. If you are moving an existing
project off `gstack`, read [Migrating from gstack](#migrating-from-gstack).

## Prepare the TODO

Create the TODO with an embedded, implementation-ready Plan:

```bash
tpo todos create <project> --request-file <project>/.hermes/todo-create-input/<uuid>.json
```

The command previews the entire issue and mutates GitHub only after `create`
confirmation. The embedded block is pinned from the issue snapshot and
materialized as a verified mode-`0600` `runs/<tick-id>/plan.md` artifact before
worktree or Kanban side effects.

For visible per-task execution, embed exactly one manifest (maximum 50 tasks):

```json tpo-plan
{
  "schema_version": 1,
  "todo_id": "TODO-42",
  "tasks": [{
    "id": "task-1",
    "title": "Implement behavior",
    "instructions": "Make the bounded change.",
    "acceptance_criteria": ["The behavior is observable."],
    "verification": ["uv run pytest tests/test_example.py"],
    "commit_message": "feat(scope): implement behavior"
  }]
}
```

The manifest is mandatory for an **embedded** Plan, which is how the issue form
authors one. An embedded Plan carrying no `json tpo-plan` block is rejected as
`manifest_required`: because the profile is plan-gated (`requires_plan`),
eligibility blocks the issue with `plan_invalid:manifest_required`, so the tick
picks nothing from it. `tpo doctor` counts it in
`Plan readiness: ... blocked=N (plan_invalid=N)` and prints the migration `Hint:`.

A pre-existing `### Plan` repository path remains supported as `legacy_path`,
and there the manifest stays optional: a valid legacy Markdown Plan without the
block still runs as exactly one development card. `tpo plan validate` and
`tpo doctor` warn because its internal steps cannot be exposed as separate
Kanban cards; pass `--require-manifest` to turn that warning into a failure. On
retries, TPO validates its pinned base authority and then leaves the existing
static development, review, and finish chain to the legacy
lifecycle; manifest-only result, dynamic review, and closeout reconciliation do
not intercept that chain.

## Initialize and verify

```bash
tpo init <project> --profile native-sdd
tpo doctor <project>
```

The only skill prerequisite is Hermes `ai-coding-agents`. The selected worker
client must still be installed and callable as `claude -p` or `codex exec`, but
no gstack, superpowers, or client-side workflow skill is used.

`tpo init` needs no `--profile` for a new project — `native-sdd` is the
default; the flag above is explicit for clarity and is required only when
regenerating a contract that names another profile.

## Migrating from gstack

`gstack` is deprecated
([ADR-0004](adr/0004-native-sdd-is-the-default-phase-profile.md)). It stays
bundled and fully supported until a later major release removes it, and nothing
migrates automatically: `tpo doctor` and `tpo tick` only emit an informational
deprecation notice (`DEPRECATED:` from `doctor`, a warning line from `tick`)
for a gstack contract, and a contract with no `profile` key keeps resolving to
`gstack`, the legacy implicit default. Existing `.hermes/` state and in-flight
ticks are unaffected until you migrate.

Migrate one project explicitly:

1. **Finish or abandon in-flight gstack runs first.** Exactly one run is active
   per project. Migrate while the board is quiescent so a gstack phase chain is
   never reconciled by the native-sdd reconcilers.
2. **Rewrite the contract.**

   ```bash
   tpo init <project> --force --profile native-sdd
   tpo doctor <project>
   ```

   `--force` rewrites the whole contract from the profile defaults: it
   recomputes `capabilities` from `native-sdd`'s `phases.yaml` and resets a
   customized `assignee`, `review_assignee`, and `capabilities` to
   `"default"` / `"default"` / the computed set. Adding `--assignee <name>`
   re-renders `review_assignee` as a *copy* of `assignee`, not as your previous
   value, so re-apply all three by editing `.hermes/pipeline.toml` afterwards.
3. **Give every eligible TODO a Plan, and a manifest if you want per-task
   cards.** `native-sdd` is plan-gated, so each `tpo:todo` issue needs exactly
   one Plan authority: either one repo-relative `Plan:` path or one embedded
   Plan block. Start from the [Plan template](templates/tpo-plan.md). An
   embedded Plan must carry a `json tpo-plan` block; without one it is blocked
   as `plan_invalid:manifest_required`, which is what `tpo doctor`'s `Hint:`
   line points at. A `Plan:` path stays eligible without a manifest, but it
   compiles to a single development card instead of one worker card per
   Plan task.
4. **Validate before the next tick.**

   ```bash
   tpo plan validate <project> --todo <n> --require-manifest
   ```

Client-side gstack work has no equivalent here: the Phase 8 `/ship` and
`$ship` prompts, `tpo approve`, and the `/review`, `/cso`, `/qa` skills are not
part of this profile. PR creation, review, and closeout are reconciled from
Kanban results instead; the run's terminal boundary is the open, unmerged pull
request and its human merge decision, which no card represents.

## Compiled sequence

1. TPO records schema-v3 `.hermes/runs/<tick-id>/registration.json`, including
   the tagged Plan source, pinned base SHA, TODO and Plan hashes, branch,
   linked worktree, roles, and step keys. Schema-v2 active runs remain readable;
   do not downgrade while a schema-v3 run is active. The same applies to the
   per-task controller gate: a run registered after that gate was dropped lists
   only `plan:<task-id>` step keys, which an older TPO rejects as
   `registration_invalid` on every tick, so it wedges with the in-progress
   label and a live worktree. Do not downgrade past that release while a
   manifest run is active; drain the run first.
2. Each Plan task becomes one worker card chained onto the previous task's
   worker. The worker reports bounded `metadata.tpo_result`; TPO validates that
   metadata and the Git topology on the next tick, and the run stops advancing
   until it does. No card waits for a human between Plan tasks.
3. A fresh review session runs the profile's own `phase_5_review` prompt: it
   applies every valid finding and commits the fixes as one review-fix commit.
   The card reaching `done` IS the pass; the card reaching `blocked` is the
   profile's own nonzero exit and automation stops. There are no remediation
   rounds and no cards fanned out from findings.
4. An accepted review enables finish, deterministic issue closeout (the `tpo:todo`
   issue is closed via `gh` after the merge), remote-head/check verification,
   and the open, unmerged pull request and its human merge decision. That
   boundary is not a card: `phase_9_human_review` is a gate phase, and
   registration creates no card for a gate phase.

Exactly one run is active per project. Retries reconcile the same keys. Drifted
authority, branch, worktree, PR, or remote head is preserved and blocked for
human input: TPO never resets, cleans, deletes, force-pushes, merges, or repairs
it automatically.

## Run evidence

Everything a run records lives in `.hermes/runs/<tick-id>/`. These files are
evidence, never a second workflow database:

| File | Written when |
|---|---|
| `registration.json` | the run is registered: immutable pinned authority |
| `plan.md` | the Plan is embedded: the hash-verified Plan artifact |
| `result-validation-blocked` | a Plan result fails validation, or the card chain is not wired; names the stalled `step_key` and `code`, blocks nothing, and is removed once every result validates |
| `pending-review-create.json` | a dynamic card create (`review:0` or `finish`) is about to run; removed once that create reports an id. Diagnostic residue only — `_persist_pending_create` writes it and `_clear_pending_create` deletes it, and nothing reads it back, so a copy left on disk means a create was in flight and never confirmed. It is not what recovers the card: `_create_task` re-derives the id with `_find_task_id_in_snapshot` before every attempt (the create itself is keyed `--idempotency-key <tick>:<step>`), and an outcome it still cannot resolve raises `RetryableReviewRegistration`, which both reconcilers turn into a plain "retry next tick". Do not confuse it with `pending-task-create.json`, the registration marker `reconcile_pending_task_create` really does read |
| `accepted-review-head` | a review is accepted, pinning the head the review left behind (its own fix commit included) |
| `finish-verified` | the PR handoff is verified: the proof of delivery |
| `issue-close-started` / `issue-commented` / `issue-closed` | issue closeout progresses |

Because a manifest run has no per-task human gate, `result-validation-blocked`
is the first thing to read when the board shows every worker `done` but the run
stops advancing. `tpo doctor` prints its step key and code for the active tick,
and the circuit-breaker alert names them once the no-progress threshold trips.
