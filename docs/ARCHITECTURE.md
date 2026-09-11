# Architecture

todo-pipeline-orchestrator is a uv-managed Python package that automates the lifecycle of TODO backlog items — GitHub issues labelled `tpo:todo` — through a multi-phase pipeline driven by Hermes agents, kanban task dependencies, and circuit breaker protections.

## Overview

```
Tick Loop (Hermes cron or manual)
    |
    v
[Selection] -- Hermes agent picks one of the eligible `tpo:todo` issues
    |
    v
[Kanban Registration] -- Build a complete chain behind a registration barrier
    |
    v
[Compile pinned Plan source] --> worker --> next worker --> next worker
    |
    v
[Independent review: applies its own findings in one review-fix commit]
    |
    v
[Finish + TODO closeout] --> open, unmerged PR + human merge decision
```

## Lane Structure

The package is organized into lanes — loosely coupled subsystems with well-defined interfaces.

```
hermes_pipeline/
├── cli.py                    # CLI entry point (argparse subcommands)
├── config.py                 # Configuration loading (global config, env overrides, TOML overlay)
├── circuit.py                # Circuit breaker (no-progress tracking, Slack alerts)
├── counter.py                # TODO ID counter management
├── contract.py               # Pipeline execution contract (schema, load, validate, capabilities)
├── hermes_adapter.py         # Hermes CLI wrapper (replaces direct Anthropic SDK)
├── kanban.py                 # Kanban adapter (hermes kanban commands)
├── kanban_tasks.py           # Durable task registration, dependency chains, manual gates
├── ship.py                   # Legacy ship-gate helper for existing sidecars
├── outcomes.py               # Outcome sidecar writing/reading
├── phases.py                 # Phase definitions + hermes subprocess invocation
├── project_config.py         # Multi-project discovery & per-project config
```

### Lane A: Hermes-Agent Selection
`decision/` — LLM-driven TODO pick via `hermes chat -q`. SHA-pinned prompt, immutable decision records, outcome sidecars. The deterministic `selection.py` was retired in v0.2.

### Lane B: State Management
`state.py` — Locks, checkpoints, ready-for-review records, atomic tmp+rename writes. All state files are written atomically to prevent partial reads.

### Lane C: Kanban Integration
`kanban.py`, `kanban_tasks.py` — Phases as kanban tasks with `--parent`
dependency chains. A gate phase dispatches no worker, so registration skips it entirely: no card is created for it and no block is ever applied. Registration creates the phase chain
behind a non-spawnable barrier
and releases it only after every task and the expected-phase sentinel are
durable. Kanban status queries drive the tick loop.

### Lane D: Runner & Phases
`phases.py`, `tick.py` — profile loading and one-active-run locking. TPO never
invokes Claude or Codex directly: Hermes dispatches every assigned worker card.
Review is reconciled from structured Kanban result metadata and Git facts.

### Lane E: Finish Branch
Phase 8 runs `/ship` in Claude Code or `$ship` in Codex, opens or updates a PR, pushes all intended branch changes, and completes normally without merging. The legacy `ship.py` helper remains for old ship-gate sidecars but is no longer part of the (now deprecated) `gstack` phase profile.

Phase 8 records the work branch in `.hermes/pipeline_branch.txt`. After the
terminal kanban task completes, the next `tpo tick` checks that branch's PR and
keeps the project in handoff until GitHub reports the PR as merged. This prevents
cron ticks from stacking the next TODO on top of an open PR branch.

### Lane F: CLI, Multi-Project, Backlog
`cli.py`, `project_config.py`, `github_issues.py`, `run_registration.py` — User-facing commands, multi-project scanning (discovery keys on `.hermes/pipeline.toml`), and the GitHub Issues backlog adapter. The legacy global-to-per-project state migration was removed as a hard cutover. (`watcher.py` and `status.py` were removed in v0.5.6 — the `__main__.py` event loop and `cli.py` subcommands cover their roles.)

### Lane G: Hermes Adapter
`hermes_adapter.py` — Wraps `hermes chat -q` for all LLM calls. Replaces direct Anthropic SDK usage.

## Phase Execution Flow

Phase execution is fully kanban-dispatched — there is no in-process Python loop that invokes
Hermes per phase. Production registration is deliberately split around tick
persistence: `prepare_todo_phases` loads the contract-selected profile and renders
every body for the global `prompt_client`; only after all rendering succeeds does
`_tick_project` persist `current_tick_id.txt` and its `tick_started` outcome, then
`create_prepared_todo_phases` creates the Hermes tasks. A malformed later prompt
therefore creates no tasks and records no active tick.

For every executable phase, its configured deadline follows this exact path:

```
Phase.timeout
  -> external Codex/Claude deadline
  -> PreparedPhaseTask.timeout
  -> hermes kanban create --max-runtime <timeout + 60> --max-retries 1
```

The final minute is cleanup-only. The installed `tpo-agent-supervisor` owns
client launch, strict deadlines, durable exit collection, and owned process
cleanup independently of the Hermes worker. Automatic worker re-entry
attaches to the same attempt. Zero exit requires existing result-contract and
current Git validation before completion. Unobservable exits and uncertain
cleanup block another attempt. Hermes reports the structured outcome through
its supported worker operations; it does not inspect or commit partial work.
See [supervision and recovery](howto-agent-supervisor.md) for process ownership,
checkpoint evidence, explicit retry admission, and portability limitations.
New Linux Codex and Claude launches, checkpoint checks, and reviewers share a
cgroup v2 backend using an existing systemd user manager and
`systemd-run --user --scope` with `Delegate=yes`. Missing cgroup v2,
`cgroup.kill`, or delegation support fails closed without a portable fallback or
automatic installation. An inert helper waits for durable cgroup and process
receipts before executing the exact argv and stdin. A bounded private anonymous
pipe carries the exact client environment separately from the manager
environment without persisting it. Setup consumes the existing deadline;
pre-exec failures may report a bootstrap exit status, never client success.
Graceful cleanup uses pidfds with a membership recheck, then
`cgroup.kill` forces termination and `populated=0` confirms emptiness. Native
cleanup uses retained group identity rather than host inventory scans, including
after supervisor loss. Cgroup receipt version 2 pins root device and inode as
well as scope identity; a changed mount view cannot establish cleanup from a
missing scope. Legacy version-1 receipts can clean matching existing scopes but
cannot newly confirm a missing scope. Existing terminal records remain intact
without invented migration evidence. This trusts same-user clients not to
migrate processes out of their groups; cgroups do not provide a filesystem
sandbox.

Execution schema 2 retains append-only `owned_cgroups` receipts. Schema-1 records
are read-upgraded without invented group evidence and persisted on the next
write; collector marker version 2 binds pending launches to their receipt
inventory baseline. Recovery retains legacy Linux PID ownership. macOS retains
its portable `libproc` unique-ID and audit-token backend, detected by capability
rather than OS version; live testing of the unchanged macOS backend is deferred.
New worker cards pin the selected absolute launcher beside the current
interpreter. Isolated `uv tool install` pairs the launcher and helper interpreter
and package version; the `PATH` fallback for nonstandard layouts cannot guarantee
that pairing. Existing cards remain unchanged.
Clients and checkpoint verification commands run as the invoking OS user,
without supervisor sandbox restrictions or storage isolation. Codex uses
`--dangerously-bypass-approvals-and-sandbox`; Claude uses
`--dangerously-skip-permissions` with the configured tools. Collectors execute
pinned command argv directly in exact-commit snapshots, inheriting the
environment and using the existing worktree virtual environment when available.
Process ownership and client availability checks precede admission; a refusal
remains visible in status without consuming an attempt.

```
cli._tick_project(config, contract)
    |
    +-- resolve_profile_phases_path(contract.profile)
    |
    +-- prepare_todo_phases(..., phases_path, prompt_client)
    |       +-- load_phases()
    |       +-- render every body into PreparedPhaseTask[]
    |       `-- any render error: failed_to_spawn, no tick persistence or Hermes calls
    |
    +-- _persist_tick_id() -- current_tick_id.txt + tick_started outcome
    |
    +-- bind prepared phases to pinned durable execution registrations
    |
    `-- create_prepared_todo_phases(...)
            +-- create unassigned registration barrier
            +-- create every prepared phase behind the barrier
            |       +-- every phase follows the previous phase with --parent
            |       `-- a gate phase is skipped: no card is created for it
            +-- persist expected-phases sentinel
            `-- complete barrier, making the first executable runnable
```

There is no combined prepare-and-create wrapper: every caller uses the split
API so tick persistence stays immediately before the first external mutation.

`tpo init` writes `profile = "native-sdd"` unless `--profile` says otherwise,
and `native-sdd` is plan-gated; `gstack` is deprecated but still bundled, and a
contract with no `profile` key keeps resolving to `gstack`, the legacy implicit
default ([ADR-0004](adr/0004-native-sdd-is-the-default-phase-profile.md)).

Profiles may set top-level `requires_plan: true`. After normal TODO selection
and before phase rendering or tick persistence, `_tick_project` resolves the
selected entry's single embedded or legacy-path Plan source. Embedded bytes
come from the pinned issue snapshot; legacy paths are resolved at the pinned
base commit. Failure records `failed_to_spawn` with `plan_validation_failed`
and creates no kanban tasks.

The `native-sdd` profile uses that gate. A manifest Plan compiles to no per-task
cards: the profile's `phase_4_development` phase registers one execution and one
thin Hermes card regardless of task count. Before publishing the card, TPO pins
the rendered phase prompt together with deterministic result, checkpoint, and
recovery instructions. The supervisor passes those exact pinned bytes to the
configured external client through stdin. The phase orders the Plan's tasks,
uses a fresh native implementer subagent for each, and makes one atomic commit
per task. The supervisor collects verification and independent review evidence
before accepting checkpoints and validates the final result before completion.

A manifest-free embedded Plan is not selectable under a plan-gated profile:
eligibility blocks it as `plan_invalid:manifest_required`. A manifest-free
`Plan:` path stays selectable with execution-attempt recovery and a bounded
phase result, without a subtask-checkpoint guarantee. Independent review uses
a distinct execution and may commit its valid findings as one review-fix
commit. An accepted review enables verified PR creation and deterministic TODO
closeout. The open, unmerged pull request and its human merge decision remain
the terminal boundary; the `phase_9_human_review` gate creates no card.

`native-sdd` requires no Hermes coding-agent skill or client-side gstack,
superpowers, or agent-skills workflow. The installed supervisor and configured
client capabilities are checked before external launch. Other profiles retain
the client skills named by their own phase prompts.

Kanban remains authoritative for card state and `metadata.tpo_result`. Thin
cards instruct Hermes to invoke or reconnect to a registered execution and
report its structured outcome through supported worker tools. Supervisor records
outside the worktree pin registration and process evidence; promoted result and
progress records preserve validated evidence separately from writable staging.
TPO checks identity, acceptance, commit topology, changed files, and current Git
state before accepting completion. Supervised implementation, review, and finish
results must match the latest attempt's promoted result, collected zero exit,
confirmed cleanup, and trusted checkpoint evidence. A shared worktree guard spans
prerequisite reads and review acceptance or finish delivery; execution locks
protect each result. Finish revalidates current implementation and review
authority, so an accepted marker cannot conceal a later failed retry. Verified
lock contention makes polling wait; unsupported locking blocks acceptance.
Historical topology validation allows a later review commit to advance HEAD
without invalidating accepted implementation evidence. A zero exit or process
disappearance alone cannot complete a card. See
[supervision and recovery](howto-agent-supervisor.md).

Before attempt admission, the launcher reports `waiting_for_admission` for
verified worktree contention or a pending launch and returns zero for continued
polling. Plain `run` polls for five seconds; newly generated workers use
`run --wait`, await its command or background tool session, and reconnect if
bounded waiting returns a nonterminal state. Waiting is capped by phase timeout
plus cleanup and, on the same host and boot, the original attempt deadline plus
cleanup. It never refreshes the execution budget. Worker instructions preserve
card state while waiting; existing card bodies are not rewritten.

New schema-v6 run registrations pin the complete ordered profile phase
snapshot: exact keys, prompts, tools, timeouts, roles, and gates. Existing v6
runs reconcile from that snapshot even when current profile definitions change
or become invalid. The scheduler advances in that declared worker order only after the predecessor validates.
The full required worker list prevents completion when a deferred card has not
yet been created. Gates retain their no-worker semantics, including the terminal
human boundary. Planless profiles retain their static declared card chain.

`Phase.role` defaults to `worker`. Unique optional `implementation`, `review`,
and `delivery` roles select special validation; an ordinary worker has no
single-commit restriction. Manifest-bearing runs require exactly one reachable
implementation worker; review and delivery remain optional. Missing roles do
not synthesize phases. Card headers,
execution records, result identity, and status retain the exact `phase_key`;
for native-sdd these include `phase_5_review` and `phase_8_finish_branch`.
The supervisor's existing result contract also pins `phase_role`. Its shared
launcher uses `--execution` to select the registered phase and prompt, runs the
client, and collects evidence; TPO owns the next-phase decision. Attempt
generations remain separate from phase identity. Supported registrations through
v5 retain their legacy identities and read protocol without rewriting. Internal
marker names such as `finish-verified` do not define phase keys. Delivery may
precede later workers, but issue closeout requires authorized evidence from every
required worker and a final HEAD matching the delivered PR head.

## Data Flow

### State Files
Project-local pipeline state lives under `<project>/.hermes/`. Non-manifest
execution authority uses the trusted account state root described in the
[supervisor guide](howto-agent-supervisor.md).

```
<project>/.hermes/
├── decisions/                 # Immutable selection decisions (write-once)
├── outcomes/                  # Phase completion/failure sidecars
├── runs/<tick-id>/registration.json # Schema v6: pinned Plan and phase schedule, required supervision
├── runs/<tick-id>/plan.md           # Verified mode-0600 artifact for embedded Plans
├── runs/<tick-id>/issue-closed    # Marker: run delivered, issue closed at closeout
├── runs/<tick-id>/abandoned       # Marker: operator abandoned the run (`touch`)
├── ready_for_review/          # Legacy ship-gate sidecars
├── pipeline_branch.txt        # Branch currently waiting at PR handoff
├── phase_started/             # In-flight phase markers
├── tick.lock/                 # Per-project tick lock (atomic mkdir)
├── config.toml                # Per-project config overlay
├── project.toml               # Project marker (enabled/slack_channel)
├── pipeline.toml              # Pipeline execution contract (assignee, capabilities)
└── circuit.json               # Circuit breaker state
```

### Decision Immutability
`.hermes/decisions/<tick_id>.json` is written exactly once. Outcomes attach via sidecars; the decision file is never edited. Rejection sidecars (`.hermes/decisions/<tick_id>-rejected.json`) are written only on rejection.

### Outcome Types
| Status | Outcome Written |
|--------|----------------|
| `done` | `phase_complete` |
| `failed` | `failed_at_phase_<key>` |
| `archived` | `failed_at_phase_<key>` with `kanban_status: "archived"` |
| `blocked` | `failed_at_phase_<key>` with `kanban_status: "blocked"` |

A `blocked` card is sticky and holds new project selection until resolved or
explicitly abandoned. `all_phases_complete` accepts only `done` and `failed`;
blocked phases still produce failure outcomes and no-progress diagnostics, but
never an `all_phases_complete` sentinel. Repeated observations do not duplicate
the same phase/status failure outcome.

After reconciling the current tick, the scheduler also checks older active
registrations that have not reached verified delivery. Unresolved execution
holds fresh selection without restarting an older run or changing the current
tick pointer; already-running current work continues normally. Runs with an
`issue-closed`, `abandoned`, or `finish-verified` marker do not hold this gate.
A legacy manifest-free run can also pass with a valid pinned registration and all
registered steps in `done` or `failed`; a successful Phase 8 additionally
requires a merged PR for that registration's exact branch. Missing, malformed,
or unavailable evidence holds selection.

## Circuit Breaker

- Tracks consecutive no-progress ticks (selection returns `picked=None`)
- At threshold (default: 3), attempts a Slack alert if a valid project or global
  channel is configured; otherwise no notification subprocess runs
- Alert dedup: one alert per `alert_dedup_hours` (default: 24)
- Gateway service manages tick scheduling and cron backoff

## Key Design Decisions

1. **Kanban as scheduler** — Executable phases are kanban tasks with `--parent`
   chains. A non-spawnable registration barrier prevents partial chains from
   running; it is completed only after the complete chain is durable. A profile
   may declare gate phases, which dispatch no worker and so are registered as no
   card at all; the deprecated
   `gstack` profile ends at Phase 8 PR handoff. `native-sdd`, the default
   profile ([ADR-0004](adr/0004-native-sdd-is-the-default-phase-profile.md)),
   keeps the same merge-aware Phase 8 handoff key and follows it with the open,
   unmerged pull request and its human merge decision as the terminal boundary;
   `phase_9_human_review` is a gate phase, so no card is registered for it.
2. **Atomic state writes** — All state files use tmp+rename to prevent partial reads.
3. **Review reconciliation is metadata-driven** — TPO validates the independent
   review card's bounded result and Git facts; it does not run a local
   snapshot/restore review lifecycle.
4. **Hermes as sole LLM surface** — All LLM traffic routes through `hermes chat -q`, not direct SDK calls.
5. **Multi-project scan** — Each project has its own lock, so a slow or
   overlapping tick skips only that project while the scan continues.

### Backlog: GitHub Issues

The TODO backlog lives in GitHub Issues on the project's github.com `origin`
([ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md)). A TODO is an open
issue carrying `tpo:todo`; its canonical ID is `TODO-<issue-number>`. `TODOS.md`
and `TODOS-archive.md` are retired (see
[migration notes](migration/todos-to-issues.md)).

- **Plan manifests** — new TODOs carry one folded Implementation Plan as the
  final issue-body content, with exactly one strict schema-v1 `json tpo-plan`
  block. The extractor removes it before H3 field parsing. Existing `### Plan`
  repository paths remain `legacy_path` inputs; dual sources are invalid
  ([ADR-0001](adr/0001-plan-is-the-execution-authority.md)). Manifest-free
  Markdown remains a legacy compatibility contract that compiles
  to one development card. A `Plan:` repository path accepts it under any
  profile; an embedded Plan does not, and a plan-gated profile
  (`requires_plan`) blocks such an issue as `plan_invalid:manifest_required`.
  `native-sdd`, the default profile for new contracts
  ([ADR-0004](adr/0004-native-sdd-is-the-default-phase-profile.md)), is
  plan-gated, so a manifest is what makes a Plan's tasks visible as cards.
  Validate a Plan with
  `tpo plan validate <project> --todo <n> --require-manifest`.
- **Label vocabulary and eligibility** — `tpo:todo` + `ready-for-agent` make an
  issue selectable; `tpo:on-hold`, `tpo:in-progress`, and pending-triage labels
  block it. Decisions live in the issue body; labels are mirrors. See
  [issue tracker](agents/issue-tracker.md#tpo-backlog-items) and
  [triage labels](agents/triage-labels.md).
- **Snapshot authority** — new schema-v6 registrations pin the issue identity,
  hashed snapshot, `plan_source_kind`, `plan_hash`, either a legacy `plan_path`
  or verified embedded `plan_artifact`, `agent_policy_mode`, and the complete
  ordered phase snapshot. They require
  durable supervisor authority. Readers also accept supported legacy schema-v2,
  v3, v4, and v5 registrations; schema v1 remains unsupported. Drain active runs and
  confirm process cleanup before installing a version that cannot read their
  registration schema.
- **Single-writer creation** — one host-local `<state-dir>/todo-create.lock`
  serializes issue creation. Durable approved requests and transaction markers
  resume partial GitHub mutations without deleting or closing issues.
- **Drift is a human boundary** — live issue drift after registration
  (`issue_drift`, `issue_closed`, `issue_on_hold`, `issue_identity_mismatch`)
  blocks the run as `needs_input` and is never auto-repaired;
  verified delivery closeout may accept an already-closed issue after checking
  that the PR merged, but identity, snapshot hash, hold, and `not_planned`
  checks still apply.
  `issue_unavailable:<code>` only warns during execution/resume preflight;
  delivery requires a successful live drift read before closeout. Tracker
  outages during selection are persisted as `tracker_error: <gh code>` decisions.
- **Claim and closeout** — the tick that creates a run's cards adds
  `tpo:in-progress` under every profile; it is the re-selection guard between
  PR-open and merge. Under a plan-gated profile (`requires_plan`) closeout
  closes the issue via `gh`, removes the label, and writes the `issue-closed`
  run marker after the PR merges. After the usual tick preflight, including the
  pending-create gate, ticks also retry delivery for active historical runs
  with `finish-verified`, even when `current_tick_id` has advanced or selection
  finds no eligible TODO. Delivered and abandoned runs are skipped. These
  retries only reconcile delivery; they do not merge PRs or require manual
  edits to run state. GitHub auto-closing the issue on merge does not prevent
  closeout. Non-plan profiles keep the claim until you run
  `tpo todos complete <project> --todo N --pr N` after the merge; until then
  `in_progress_stale` is the expected blocked reason for a delivered issue.
  Completion markers count only when TPO wrote them (the current `gh` login, or
  a `tick=` naming a local `runs/<tick>` directory).
- **Live harness** — `tpo test --repo OWNER/NAME` runs one production tick
  against a disposable GitHub sandbox repository with the real `gh` (minimum
  2.44; `TPO_GH_BIN` overrides are rejected). See
  [howto-live-integration-test-harness.md](howto-live-integration-test-harness.md).

## See Also
- [Kanban-as-Scheduler](reference-kanban-as-scheduler.md) — How kanban drives phase state
- [Pipeline State Machine](hermes-state-machine.md) — Full tick lifecycle transitions
- [Modularization Plan](pipeline-modularization-plan.md) — Design history and rationale
- [Multi-Project Scan](explanation-multi-project-scan.md) — Per-project locking and discovery decisions
