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
[Compile pinned Plan source] --> worker --> controller gate --> next worker
    |
    v
[Independent review] --> review-fix --> validation --> re-review (bounded)
    |
    v
[Finish + TODO closeout] --> human merge gate
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
dependency chains; human gates stay in the chain but have no assignee or goal
and receive a sticky `needs_input` block. Registration creates the phase chain
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

The final minute is cleanup-only: after the client deadline, the dispatcher
terminates the external process tree and confirms it is no longer running.
Only a zero external-agent exit may complete the phase. After a timeout or
non-zero exit, the dispatcher comments the known external-agent failure
metadata, then applies the supported `needs_input` block transition with the
exact reason. Hermes cannot finish, inspect, or commit partial work.

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
    `-- create_prepared_todo_phases(...)
            +-- create unassigned registration barrier
            +-- create every prepared phase behind the barrier
            |       +-- every phase follows the previous phase with --parent
            |       `-- gate tasks receive no goal and a sticky needs_input block
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

The `native-sdd` profile uses that gate. A manifest Plan compiles to ordered
worker cards separated by unassigned controller gates. A manifest-free embedded
Plan is not selectable under a plan-gated profile: eligibility blocks the issue
as `plan_invalid:manifest_required`. A manifest-free `Plan:` path stays
selectable and compiles to a single development card. Independent review uses
a distinct session; findings compile into at most five `review-fix` /
fix-validation / re-review rounds. A clean result enables verified PR creation,
deterministic TODO closeout, and an unassigned terminal human-review gate.
Only the Hermes `ai-coding-agents` dispatcher skill is required; client-side
gstack, superpowers, and agent-skills workflows are not part of this profile.

Kanban is authoritative for live state and `metadata.tpo_result`. Local files
under `.hermes/runs/<tick-id>/` contain immutable registration and crash-recovery
evidence only. TPO validates identity, commit topology, changed files, TDD
records, acceptance statuses, and review findings before opening a controller
gate.

## Data Flow

### State Files
All pipeline state lives under `<project>/.hermes/`:

```
<project>/.hermes/
├── decisions/                 # Immutable selection decisions (write-once)
├── outcomes/                  # Phase completion/failure sidecars
├── runs/<tick-id>/registration.json # Schema v3: pinned issue, tagged Plan source, branch/worktree
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

## Circuit Breaker

- Tracks consecutive no-progress ticks (selection returns `picked=None`)
- After threshold (default: 3), fires Slack alert
- Alert dedup: one alert per `alert_dedup_hours` (default: 24)
- Gateway service manages tick scheduling and cron backoff

## Key Design Decisions

1. **Kanban as scheduler** — Executable phases are kanban tasks with `--parent`
   chains. A non-spawnable registration barrier prevents partial chains from
   running; it is completed only after the complete chain is durable. Profiles
   may define manual gates with sticky `needs_input` blocks; the deprecated
   `gstack` profile ends at Phase 8 PR handoff. `native-sdd`, the default
   profile ([ADR-0004](adr/0004-native-sdd-is-the-default-phase-profile.md)),
   keeps the same merge-aware Phase 8 handoff key and follows it with a terminal
   human gate.
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
- **Snapshot authority** — schema-v3 registration pins the issue identity,
  hashed snapshot, `plan_source_kind`, `plan_hash`, and either a legacy
  `plan_path` or verified embedded `plan_artifact`. Readers accept active
  schema-v2 and v3 runs. Schema v1 remains unsupported, and versions without v3
  support must not be installed while a v3 run is active.
- **Single-writer creation** — one host-local `<state-dir>/todo-create.lock`
  serializes issue creation. Durable approved requests and transaction markers
  resume partial GitHub mutations without deleting or closing issues.
- **Drift is a human boundary** — live issue drift after registration
  (`issue_drift`, `issue_closed`, `issue_on_hold`, `issue_identity_mismatch`)
  blocks the run as `needs_input` and is never auto-repaired;
  `issue_unavailable:<code>` only warns. Tracker outages during selection are
  persisted as `tracker_error: <gh code>` decisions.
- **Claim and closeout** — the tick that creates a run's cards adds
  `tpo:in-progress` under every profile; it is the re-selection guard between
  PR-open and merge. Under a plan-gated profile (`requires_plan`) closeout
  closes the issue via `gh`, removes the label, and writes the `issue-closed`
  run marker after the PR merges. Non-plan profiles keep the claim until you run
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
