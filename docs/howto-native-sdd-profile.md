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

## User-policy delegated mode

The global setting defaults to `inherit`. Opt in only after confirming that
**each selected external client's user-level policy** recognizes the exact
first-line marker `AGENT-POLICY-MODE: delegated`:

```bash
tpo config set agent_policy_mode delegated
```

This is your compatibility assertion, not automatic policy detection. Only the
exact `native-sdd` profile uses it; other profiles, including custom profiles
with `requires_plan`, keep inherited behavior. There is no project override or
launch flag. TPO retains normal user and project instruction loading, client
arguments, permissions, sandboxing, and tools. It does not use Claude safe mode
or override Codex instruction files. The policy decides which obligations to
waive and keeps obligations it does not waive. An incompatible policy can still
block or time out; TPO cannot guarantee delegated behavior.

For opted-in workers, TPO places that marker first on the external client's
stdin, followed by a blank line and the original task. Dispatcher instructions
and result metadata stay outside that payload. Implementation, unified review
(including its optional fix commit), and finish workers receive it; controller
reconciliation and human gates do not run delegated workers.

Registration pins the effective mode: new opted-in native-SDD runs use schema
v4 with `agent_policy_mode: delegated`; other new runs keep v3. Existing v2/v3
runs mean `inherit`. Changing the global setting cannot change an active run's
initial or later workers.

TPO rejects pre-existing standalone mode declarations before publishing the
corresponding worker card, including duplicates, conflicting or malformed
values, BOM and whitespace prefixes, blockquotes, and inline-code wrappers.
It also inspects lines inside fenced code blocks. This is a line-oriented guard:
the declaration key followed by `:` or `=` and any text, or the bare key with a
single value token, counts as a declaration. Ordinary prose, inline mentions,
and shell metacharacters remain unchanged. This does not detect arbitrary
natural-language instructions or prove policy compatibility. TPO reports a
sanitized preparation failure or `needs_input`; it never strips declarations
or silently switches mode.

For this feature, `tpo doctor` checks client executable availability only; it
does not verify the contents or semantics of user policy. Its other existing
project, Hermes and Plan checks still apply. Run the
[live qualification recipes](release-qualification-agent-clients.md#native-sdd-live-policy-recipes)
to test real policy behavior for both clients and both modes.

### Rollback

Run `tpo config set agent_policy_mode inherit` to disable opt-in for **new**
runs. Enumerate opted-in registrations read-only, for example from a project
root:

```bash
python - <<'PYCODE'
import json
from pathlib import Path
for path in Path(".hermes/runs").glob("*/registration.json"):
    registration = json.loads(path.read_text())
    if registration.get("agent_policy_mode") == "delegated":
        print(path)
PYCODE
```

Inspect their active-run state and drain opted-in runs before reverting code.
Preserve registration files: pre-v4 code rejects v4; restore compatible code to
finish such runs, never reinterpret or edit their pinned mode. If a run must
change mode, abandon it through the recovery workflow and register a new run.

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
3. **Give every eligible TODO a Plan, and a manifest to get its results
   verified.** `native-sdd` is plan-gated, so each `tpo:todo` issue needs
   exactly one Plan authority: either one repo-relative `Plan:` path or one
   embedded Plan block. Start from the [Plan template](templates/tpo-plan.md).
   An embedded Plan must carry a `json tpo-plan` block; without one it is
   blocked as `plan_invalid:manifest_required`, which is what `tpo doctor`'s
   `Hint:` line points at. A `Plan:` path stays eligible without a manifest,
   but a manifest-free run's implementation card publishes no result template
   and its result is never parsed, so nothing anchors the reviewed head: the
   task count, the acceptance criteria, and the commit-count bound all come
   from the manifest.
4. **Validate before the next tick.**

   ```bash
   tpo plan validate <project> --todo <n> --require-manifest
   ```

Client-side gstack work has no equivalent here: the Phase 8 `/ship` and
`$ship` prompts, `tpo approve`, and the `/review`, `/cso`, `/qa` skills are not
part of this profile. PR creation, review, and closeout are reconciled from
Kanban results instead; the run's terminal boundary is the open, unmerged pull
request and its human merge decision, which no card represents.

## Run sequence

1. TPO records schema-v3 (or opted-in schema-v4)
   `.hermes/runs/<tick-id>/registration.json`, including
   the tagged Plan source, pinned base SHA, TODO and Plan hashes, branch,
   linked worktree, roles, and step keys. Schema-v2 active runs remain readable;
   do not downgrade while a schema-v3 run is active. The same applies to the
   step keys themselves: a manifest run registered after the per-Plan-task
   fan-out was deleted lists one `phase_4_development` step key, which an older
   TPO rejects as `registration_invalid` on every tick because it looks for
   `plan:<task-id>` keys instead — and the reverse is equally true, so a run
   registered before that release will not load after it. The break is
   fail-closed in both directions: nothing is verified against a card shape
   that no longer exists, and the branch and its commits are left untouched. Do
   not upgrade or downgrade across that release while a manifest run is active;
   drain the run first.
2. The Plan gets exactly ONE implementation card — the profile's
   `phase_4_development` — whatever the Plan's task count. The card carries that
   phase's prompt verbatim and its declared `tools`, `turns` and `timeout`; the
   prompt is what tells the agent to read the Plan, branch from main, preserve
   unrelated tracked and untracked work, run one native implementer subagent per
   Plan task, and make exactly one atomic commit per Plan task. TPO does not
   restate any of that and does not fan the phase into per-task cards: the
   profile is the specification. The card reports bounded
   `metadata.tpo_result`; on the next tick TPO validates that metadata, every
   Plan task's acceptance criteria, and the Git topology — exactly
   `len(tasks)` commits on the first-parent mainline from the pinned base SHA —
   and the run stops advancing until it does. No card waits for a human.
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
