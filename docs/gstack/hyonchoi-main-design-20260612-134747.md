# TODO Pipeline Orchestrator — Hermes-Centric Selection & Spawning

**Mode:** Builder
**Status:** APPROVED
**Verdict:** APPROVED — ready for /plan-eng-review
**Date:** 2026-06-12
**Author:** hyonchoi
**Branch:** main
**Supersedes (in part):** [hyonchoi-main-design-20260610-195349.md](hyonchoi-main-design-20260610-195349.md)
  — replaces `selection.py` (Approach A deterministic sort, T1/T3/T4) and the
  `watcher.py` tick/cron loop. The state machine (`state.py`,
  `ready_for_review`, `merge_status`), Phase 9 typed-confirm merge, and
  `kanban.py` thin-wrapper survive intact.

**Resolves:** TODO-2 (Hermes-agent TODO parsing/selection), TODO-3 (route
non-Hermes process spawning through Hermes). Unblocks TODO-4 (integration
tests).

---

## Problem Statement

The approved 2026-06-10 design specs a deterministic `selection.py` that
assumes a strict TODOS.md schema (priority/effort/phase/deps fields). Two
problems surfaced post-approval:

1. **Schema fragility.** Real TODOS.md files drift — humans add free-form
   notes, half-fill metadata, reorder sections. A strict parser either
   rejects valid intent or silently misranks. Patching the parser for every
   irregularity is a treadmill.
2. **Spawning sprawl.** The watcher shells out to `crontab` for scheduling
   and `claude -p` for phase execution, separate from how every other agent
   workflow on this machine launches (via Hermes). Two scheduling paths, two
   log destinations, two failure modes.

Both are symptoms of the same thing: an orchestrator that has Hermes
*available* but routes around it for the decisions and the spawns.

## What Makes This Cool

The Phase 9 merge gate is a hard human checkpoint that already exists. That
gate is the reason it is safe to put an LLM in the selection seat: even a
catastrophic miscall produces a branch + ready-for-review record that a
human types `TODO-N` to merge or discards. The blast radius of a bad pick
is "one wasted Claude run," not "shipped wrong code." With that backstop in
place, the deterministic sort becomes the conservative choice, not the
obvious one.

## Constraints

- **Personal tool.** Single user, single machine. No multi-tenant concerns.
- **Phase 9 merge gate is non-negotiable.** Never auto-merge.
- **Hermes is the agent substrate.** Already provides `chan message`,
  `kanban create/comment/complete/archive`, and (per TODO-3) `cron`.
- **Python 3.12+, uv-managed.**
- **State on disk under `.hermes/`** (existing convention).
- **Cost: not a primary constraint** — user explicitly accepted per-tick
  LLM cost; circuit breaker handles runaway loops, not unit economics.

## Premises

1. **Phase 9 is the safety net, not the parser.** Selection correctness is
   eventually-consistent; the merge gate is the consistency point.
2. **Schema-tolerance > schema-enforcement.** A Hermes agent reading
   TODOS.md as prose handles drift the deterministic sort cannot.
3. **One spawn path.** Every process the orchestrator launches goes through
   `hermes` — cron, Claude Code phase invocations, notifications. No
   `crontab -e`, no bare `claude -p`.
4. **`hermes_pipeline` shrinks.** It becomes phase-execution helpers + the
   state/merge-gate it owns. It no longer owns selection or scheduling.
5. **Hermes is the top-level orchestrator.** The cron-fired entrypoint is a
   Hermes command that decides "what next" and calls into
   `hermes_pipeline` for the mechanical phase work.
6. **Circuit breaker is orthogonal to selection.** N consecutive
   no-progress ticks back off the interval and emit one Slack alert. This
   guards cost/observability, not pick quality.
7. **Every pick emits a `HermesSelectionDecision` record.** Structured
   audit trail: `{tick_id, candidates_considered, picked, rationale,
   blocked_reasons[], model, timestamp}`. Persisted alongside
   `ready_for_review`. Doubles as the transition-period manual-driver
   artifact — during week 1, the agent picks, the human reads the decision
   record before letting Phase 4+ run.

## Cross-Model Perspective

Codex (read-only, high reasoning) flagged three things worth keeping:

- **The `HermesSelectionDecision` contract** — adopted as Premise 7.
- **Concern: "agent picks the same TODO every tick."** Mitigated by the
  decision record (visible loop) + circuit breaker (Premise 6) +
  `ready_for_review` blocking re-pick of in-flight items (already in
  `state.py`).
- **Concern: "no rollback path if Hermes is down."** Resolved by Premise 4:
  `hermes_pipeline` retains a `--no-hermes` mode that runs the
  deterministic sort against a strict-schema TODOS.md. Not the happy path,
  but a known-good fallback for debugging Hermes itself.

## Approaches Considered

**A. Keep deterministic selection.py, add a tolerant pre-parser.**
Parser normalizes drift into the strict schema before sort. Cheap, no LLM
cost, fully reproducible. Rejected: the pre-parser is itself the schema
fragility problem one layer down, and "what should this TODO's priority
be?" isn't a parsing question, it's a judgment question.

**B. Hybrid — agent parses, deterministic sort picks.** Hermes reads
TODOS.md and emits the strict-schema JSON; selection.py sorts that.
Rejected: keeps two systems of record (the LLM's reading + the sort's
ranking) and the agent does the easy half of the job. If you trust the
agent for parsing, trust it for picking.

**C. Hermes agent fully owns parsing + selection (recommended).** One
agent call per tick, returns a `HermesSelectionDecision`. `selection.py`
deleted. `hermes_pipeline` keeps state/Phase 9. Spawning routed through
`hermes cron` and `hermes run`. This is what we ship.

## Recommended Approach

### Architecture

```
hermes cron (every 5 min)
   └─> hermes run pipeline-tick
         ├─> mints tick_id (ULID)
         ├─> calls hermes_pipeline.decision.run_selection(tick_id, ctx)
         │     └─> builds prompt, calls agent, parses response,
         │         persists HermesSelectionDecision, returns it
         ├─> if config.auto_execute is false: STOP (shadow mode)
         ├─> if decision.picked is None: STOP (circuit breaker tick++)
         └─> hermes run pipeline-phase --todo TODO-N --tick-id <id> --phase autoplan
               └─> hermes_pipeline.phases.run(...) — mechanical execution
                     └─> on completion writes ready_for_review (carrying tick_id)

hermes_pipeline (Python package, shrunk):
  state.py         — unchanged (ready_for_review, merge_status state machine)
  phases.py        — Claude Code phase invocations (was watcher.run_phase)
                     writes ready_for_review; owns post-phase state
  merge.py         — Phase 9 typed-confirm command (unchanged)
  kanban.py        — thin HermesKanbanAdapter; T1 outbox-collapse RETAINED
  decision.py      — schema + run_selection() + persistence + archive rotation
  fallback.py      — --no-hermes deterministic sort, strict-schema only
  config.py        — loads .hermes/config.toml (auto_execute, model, caps)
  cli.py           — `pipeline-watch` CLI: merge, status, --no-hermes,
                     --help. NO tick/cron entrypoint (Hermes owns that).
```

`pipeline-tick` (Hermes command) is a thin shell: mint id, call
`decision.run_selection()`, branch on auto_execute + picked, hand off to
`pipeline-phase`. Prompt construction, parsing, and persistence live in
`decision.py` — the Hermes command does not see the agent directly.

### HermesSelectionDecision schema

```python
@dataclass
class HermesSelectionDecision:
    tick_id: str                      # ULID
    timestamp: str                    # ISO8601 UTC
    model: str                        # e.g. "claude-opus-4-7"
    candidates_considered: list[str]  # ["TODO-1", "TODO-2", ...]
    picked: str | None                # "TODO-2" or None (no-progress tick)
    rationale: str                    # one paragraph, agent's words
    blocked_reasons: dict[str, str]   # {"TODO-3": "depends on TODO-2"}
    in_flight: list[str]              # ready_for_review TODOs skipped
```

Persisted to `.hermes/decisions/<tick_id>.json`. Rotation: when hot dir
exceeds 50 files, oldest are moved to `.hermes/decisions/archive/`
(append-only, never pruned — JSON is tiny).

### Agent input contract

`decision.run_selection(tick_id, ctx)` receives `ctx`:
```python
@dataclass
class SelectionContext:
    todos_md: str                     # raw file contents, no preprocessing
    in_flight: list[str]              # ready_for_review TODO IDs
    recent_decisions: list[dict]      # last 5 decision JSONs (loop visibility)
    kanban_snapshot: dict             # `hermes kanban list` output
    project_slug: str
```

The agent sees the full raw TODOS.md (drift-tolerance is the point).
Recent decisions are passed so the agent can notice "I picked TODO-N
3 ticks ago and it's still in_flight — pick something else."

### Agent prompt + model config

`.hermes/config.toml`:
```toml
[selection]
model = "claude-opus-4-7"
max_tokens = 4000
auto_execute = false          # shadow mode default
prompt_path = ".hermes/prompts/selection.md"

[circuit_breaker]
no_progress_threshold = 3
backoff_interval_min = 30
alert_dedup_hours = 24

[cost]
max_decisions_per_hour = 20   # hard cap, separate from circuit breaker
```

API key: read from `ANTHROPIC_API_KEY` env, which Hermes injects from
its existing secret store. Not stored in `.hermes/` on disk.

Prompt template lives in `.hermes/prompts/selection.md` (versioned in the
Hermes config repo, not this one). Required output: JSON matching
`HermesSelectionDecision` minus `tick_id`/`timestamp`/`model` (filled by
`decision.py`). Parse failure → `picked=None`, rationale captures the
parse error, counted as no-progress for circuit breaker.

### Spawning (TODO-3)

| Old path | New path |
|---|---|
| `crontab -e` registration | `hermes cron add pipeline-tick "*/5 * * * *"` |
| `claude -p ...` for phases | `hermes run pipeline-phase --todo X --phase Y` |
| Slack via `curl` webhook | `hermes chan message <channel> <msg>` |
| Kanban CRUD | `hermes kanban …` (already in approved doc) |

`hermes_pipeline` exposes the phase logic as a library call; the Hermes
command is the spawner. Single log destination (Hermes's), single failure
surface.

### Circuit breaker

State in `.hermes/circuit.json`:
```
{ consecutive_no_progress: int, last_alert_at: iso8601,
  hourly_decision_count: int, hour_bucket: iso8601 }
```

**"No-progress tick" is defined as:** `decision.picked is None`
(including agent parse failures). A picked-but-failed phase is NOT
no-progress — that's a code/run problem, surfaced via `merge_status:
failed`, not selection.

After 3 consecutive no-progress ticks: cron interval backs off to 30 min
and one Slack alert fires (deduped 24h). Resets on first successful pick.

Separately, `cost.max_decisions_per_hour` caps `run_selection()` calls
per rolling hour — guards against the "agent picks something every tick
forever" failure mode that the no-progress breaker misses.

### Migration

- **Week 1 (shadow mode):** `auto_execute = false`. `pipeline-tick` runs
  every 5 min, emits decisions, STOPS. Human reads
  `.hermes/decisions/<latest>.json` and manually runs
  `hermes run pipeline-phase --todo TODO-N --tick-id <id>` if the pick
  looks right.
- **Week 2 (live):** set `auto_execute = true` in `.hermes/config.toml`.
  Config is reloaded per tick (no daemon to restart).
- **Rollback:** `pipeline-watch --no-hermes` uses the strict-schema
  fallback sort. **Caveat:** the fallback assumes a strict-schema
  TODOS.md and will reject the very drift the agent path tolerates. The
  fallback is for debugging Hermes itself (e.g., outage during a known
  clean TODOS.md), not for sustained operation. If TODOS.md has drifted,
  rollback requires either reverting the file to strict schema or
  reverting to the prior approved design.

### Tasks dropped / re-homed from the prior design

- ❌ T1 (selection.py outbox collapse) — selection.py gone. **Kanban
  outbox-collapse logic from T1 RETAINED in `kanban.py`** (the
  create-preserving idempotency was about kanban writes, not selection).
- ⚠️ T3 (cycle-detection dedup) — cycle *detection* moves to agent
  judgment, but the underlying correctness concern (clear-on-resolve to
  prevent suppressed regressions) is re-homed: when a TODO transitions
  out of `ready_for_review` (merged or failed), its decision history is
  marked resolved so a future re-occurrence is treated as a NEW pick,
  not a suppressed cycle. Lives in `state.py` transitions, not
  `decision.py`.
- ⚠️ T4 (per-project parse isolation) — strict parser gone, but the
  *isolation* requirement survives: `run_selection()` is called
  per-project in a try/except; one project's agent failure (API error,
  parse fail, timeout) does NOT block other projects in the same tick.
  Persisted as `picked=None` with rationale="error: ..." for that
  project only.

### Tasks retained

- ✅ T2 (`merge_status: failed`), T5/T14 (-attemptN scan), T6 (CLI
  subcommands), T7 (`clear_active_task(outcome)`), T8 (error messages),
  T9 (Phase 9 typed-confirm), T10 (todos-manager preview gate), T11
  (tick_id — now lives on the decision record), T12 (log routing — now
  through Hermes), T13 (--help).

### New tasks

- **N1** — `decision.py`: schema, `run_selection(tick_id, ctx)`, prompt
  builder, response parser, persistence, archive rotation, per-project
  try/except isolation (re-homed T4).
- **N2** — `phases.py`: extract phase invocation from `watcher.py` into a
  library callable. Owns `ready_for_review` write with `tick_id`.
- **N3** — `fallback.py`: strict-schema sort behind
  `pipeline-watch --no-hermes`.
- **N4** — Hermes command definitions: `pipeline-tick`, `pipeline-phase`.
  Lives in the Hermes config repo. Contract with this repo: the JSON
  shape of `SelectionContext` and `HermesSelectionDecision` — both
  exported from `hermes_pipeline.decision` and documented in
  `docs/hermes-contract.md`.
- **N5** — Circuit breaker state (`.hermes/circuit.json`), no-progress
  counter, hourly decision cap, Slack alert via `hermes chan message`.
- **N6** — `config.py` + `.hermes/config.toml` loader (auto_execute,
  model, cost caps, prompt_path). Per-tick reload, no daemon state.
- **N7** — `state.py` decision-history resolution on transition
  out of `ready_for_review` (re-homed T3).
- **N8** — `docs/hermes-contract.md`: schemas, prompt I/O, error codes.
  Cross-repo contract document.

## Open Questions

1. **Hermes command repo path** — assumed Hermes config repo. Confirm
   exact location before N4.
2. **Prompt versioning** — `selection.md` lives in Hermes config repo;
   should it pin a version that `decision.py` validates? Lean yes,
   defer to /plan-eng-review.
3. **T12 log routing** — does `hermes_pipeline` still write structured
   logs to a local file, or only to stdout (which Hermes captures)?
   Lean: stdout only, Hermes is the log sink.

## Success Criteria

- TODOS.md with irregular formatting (missing fields, mixed sections,
  freeform notes) is handled without parser edits.
- One spawn path: `ps auxf | grep -E 'claude|pipeline'` shows all
  processes parented by a `hermes` process.
- Every executed phase has a matching `HermesSelectionDecision` on disk.
- Circuit breaker fires once and only once on a forced 3-tick stall.
- Phase 9 merge gate behavior unchanged from approved doc.
- `--no-hermes` fallback runs end-to-end on a strict-schema TODOS.md.

## Distribution Plan

Personal tool — no external distribution. Internal "release" is:
1. Merge to `main`.
2. Update local Hermes config with new commands.
3. Replace existing crontab entry with `hermes cron add`.
4. Run one week in shadow mode before flipping `auto_execute`.

## Next Steps

1. `/plan-eng-review` this design (specifically: decision.py schema,
   phases.py extraction boundary, fallback.py scope).
2. Spec out the Hermes command JSON for `pipeline-tick` /
   `pipeline-phase`.
3. Implement N1–N6 in dependency order (N1 → N2 → N3 → N5 → N6 → N4).
4. Shadow-mode bake week.
5. After 2 successful weeks live: revisit TODO-4 (integration tests) with
   the new architecture as fixture.

## What I noticed about how you think

You kept choosing the more radical option against my safer
recommendations — agent over deterministic, no guardrails over guardrails,
Hermes as top-level over Hermes as adapter. Each push-back from me was met
with the same answer. That's a founder-conviction signal: you've decided
the Phase 9 merge gate is sufficient backstop and you'd rather pay LLM
cost than maintain a parser. The design follows your conviction, not my
hedge.

---

*Approved 2026-06-12 after 2-iteration spec review (converged clean).*

## Eng Review Updates (2026-06-13)

This section locks the design changes from `/plan-eng-review`. Where these
conflict with sections above, this section wins.

### Scope reductions (Step 0)

- **N3 fallback.py — DROPPED.** The `--no-hermes` deterministic-sort fallback
  is removed. Rationale: per the original design, the fallback only runs
  against a strict-schema TODOS.md, and drift-tolerance is the whole reason
  for the agent path. Hermes-outage debugging uses `hermes_pipeline.decision`
  imported directly with `ANTHROPIC_API_KEY` in env.
- **`cost.max_decisions_per_hour` — DROPPED.** The no-progress breaker plus
  outcome-aware recent_decisions plus Phase 9 cover the failure modes. Re-add
  if a cost spike is ever observed in practice (~half-day change).
- **N8 cross-repo doc — COLLAPSED.** `docs/hermes-contract.md` becomes rich
  docstrings on `HermesSelectionDecision` + `SelectionContext` + a 20-line
  `hermes_pipeline/decision/README.md` that re-exports the schemas. Single
  source of truth; no drift.
- **Single-project assumption — LOCKED.** N1's per-project try/except wrapper
  (re-homed T4) is dropped. `pipeline-tick` runs against this repo only.
  `SelectionContext.project_slug` is a constant. Multi-project is a thin
  wrapper to be added if/when a second project exists.

### Architecture locks

- **A1 — mid-flight in_flight tracking.** `phases.run()` writes
  `.hermes/phase_started/<todo_id>.json` synchronously at the top of
  execution, before any Claude Code invocation. Marker is deleted only when
  the phase reaches a terminal state (success writes ready_for_review then
  deletes; failure writes merge_status:failed then deletes). Stale markers
  older than `max_phase_timeout` are swept by `decision/context.py` on read.
  `in_flight` is the union of `ready_for_review` and `phase_started/*`.
- **A2 — prompt SHA pinning.** `decision/agent.py` computes SHA-256 of the
  prompt file body, embeds it as `prompt_sha` on every
  `HermesSelectionDecision`. `.hermes/config.toml` adds optional
  `expected_prompt_sha`. Mismatch → `picked=None`, rationale captures both
  SHAs, fires Slack alert via `hermes chan message` (loud failure, not
  silent — folds Codex #7), and does NOT count toward the no-progress
  circuit breaker (the failure is a deployment fault, not a stall).
- **A3 — outcome-aware recent_decisions.** Decision records gain an
  `outcome` field surfaced through `recent_decisions` in the next tick's
  `SelectionContext`. Outcome values: `"in_flight" | "merged" |
  "failed_at_phase_N" | "discarded" | "killed_by_operator" |
  "failed_to_spawn"`. See XM2 for the N7 reconciliation and XM3 for the
  storage shape.
- **XM1 — tick-level lock.** `pipeline-tick` acquires `.hermes/tick.lock`
  (flock or atomic mkdir) before `run_selection()` and releases it after
  spawn confirmation (or after writing `outcome="failed_to_spawn"` on
  spawn failure, or after persisting the decision in shadow mode). A new
  tick that finds the lock held exits immediately with rationale="tick
  already in flight, skipping" — NOT counted as no-progress. Stale-lock
  sweep when the holder file is older than `max_tick_duration`.
- **XM2 — N7 dropped.** The re-homed T3 "clear decision history on
  transition out of ready_for_review" is removed entirely. The original
  T3 concern (suppressed-regression dedup) lives in `kanban.py`'s
  outbox-collapse, not in `state.py` touching decision records. Decision
  records are immutable; the agent's failure memory survives.
- **XM3 — immutable decisions + sidecar outcomes.** `.hermes/decisions/
  <tick_id>.json` is write-once. `.hermes/outcomes/<tick_id>.json` is
  appended by `state.py` transitions. `store.load_recent(n)` joins the
  two directories by tick_id. Rotation moves both files in lockstep when
  the hot dir exceeds 50.
- **XM4 — prompt-injection guard.** `.hermes/prompts/selection.md` wraps
  TODOS.md content in `<todos_md_content>` tags and recent decisions in
  `<recent_decisions>` tags. System prompt explicitly states that fenced
  content is untrusted data, not instructions. Eval suite gains an
  `injection_attempt.md` fixture.
- **XM6 — rollback kill switch.** New CLI subcommand
  `pipeline-watch kill [--all | --todo TODO-N]` reads
  `phase_started/*` markers, sends `hermes run kill <job-id>` for each,
  writes `outcome="killed_by_operator"` outcome sidecars, deletes the
  markers. Migration section's rollback procedure: (1) set
  `auto_execute=false`, (2) `pipeline-watch kill --all`, (3) inspect
  `.hermes/decisions/` for orphans, (4) optionally revert
  `expected_prompt_sha` to a known-good.

### Code quality lock

- **C1 — decision.py becomes a sub-package.** `hermes_pipeline/decision/`
  layout:
  - `__init__.py` — re-exports public API (`HermesSelectionDecision`,
    `SelectionContext`, `run_selection`); orchestrates build-ctx → call-agent
    → persist → return.
  - `schema.py` — dataclasses (`HermesSelectionDecision`,
    `SelectionContext`). Rich docstrings carry the cross-repo contract
    (replaces N8).
  - `agent.py` — prompt build, SHA compute, Anthropic API call, response
    parse.
  - `store.py` — `persist()`, `append_outcome()`, `load_recent()` (joins
    decisions + outcomes by tick_id), rotation.
  - `context.py` — `build_in_flight()` (union of `phase_started/*` and
    `ready_for_review`, with stale sweep), `build_context(tick_id)`.

### Test plan lock

- **T1 — selection-prompt eval suite.** 8-12 fixtures under
  `tests/eval/selection/` covering: 3 drift levels, in_flight respect,
  outcome-aware reasoning (A3), priority-with-deps, SHA-mismatch refusal,
  prompt-injection attempt, empty TODOS. CI runs the suite on every commit
  touching `decision/agent.py` or `.hermes/prompts/`; **non-blocking** until
  the shadow-mode bake week ends, then flip to blocking. See the test plan
  artifact for the full fixture inventory:
  `~/.gstack/projects/todo-pipeline-orchestrator/hyonchoi-main-eng-review-test-plan-20260613-000000.md`.
- **REGRESSION RULE** — Phase 9 typed-confirm merge: a regression test is
  written and verified green against the current `watcher.py`
  implementation BEFORE `phases.py` extraction lands. Then extract.

## NOT in scope

Explicitly deferred or excluded from this PR.

- **`--no-hermes` deterministic-sort fallback (N3).** Dropped. Re-add only
  if a Hermes outage event makes the case empirically. Hermes-outage
  debugging today: `python -c "from hermes_pipeline.decision import
  run_selection; ..."` with API key in env.
- **`max_decisions_per_hour` cost cap.** Dropped per D1 and XM5. Re-evaluate
  if real-cost data justifies it.
- **Standalone `docs/hermes-contract.md`.** Replaced by docstrings +
  `decision/README.md` (T15).
- **Multi-project tick iteration.** Single-project locked. Multi-project
  wrapper is a half-day change when a second project exists.
- **Selection-model fallback ladder.** Captured as TODO-5 in `TODOS.md`.
  This PR fails loudly on a model 404.
- **Open Q1 / Open Q3 resolution as separate PR.** Folded into T16 (resolve
  before N4 implementation kicks off). Q3 lean: stdout-only, Hermes is the
  log sink.

## What already exists

Existing code/flows this plan reuses rather than rebuilds.

- **`state.py` state machine** — `ready_for_review`, `merge_status`. Reused
  unchanged in shape; extended via T8 (sidecar outcome writes) and the
  Phase 9 transition path.
- **`kanban.py` thin HermesKanbanAdapter + T1 outbox-collapse logic.**
  Retained. The original T1 (selection.py outbox collapse) is gone because
  selection.py is gone, but the kanban write-side outbox-collapse stays
  here and absorbs any "suppressed regression" concern from XM2.
- **Phase 9 typed-confirm merge (`merge.py`).** Behavior unchanged. T1
  regression test pins this before phases.py extraction.
- **`hermes cron`, `hermes run`, `hermes chan message`, `hermes kanban`.**
  Already shipping in the Hermes substrate; this plan consumes them.
- **Existing `watcher.py` phase invocation.** Logic is reused as the body
  of the new `phases.py`; this is an extraction + marker write, not a
  rewrite.
- **Hermes config repo.** Hosts `.hermes/prompts/selection.md`,
  `pipeline-tick`, `pipeline-phase` command defs. Cross-repo contract is
  the Python schema imports (T15), not a separate markdown.

## Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific
finding above. Run with Claude Code or Codex; checkbox as you ship.

- [ ] **T1 (P1, human: ~3h / CC: ~25min)** — phases.py — Pin Phase 9 typed-confirm regression test BEFORE extracting from watcher.py
  - Surfaced by: Test review — REGRESSION RULE; phases.py extraction must not silently change Phase 9 behavior
  - Files: `tests/regression/test_phase9_merge.py`, `hermes_pipeline/phases.py`, `hermes_pipeline/watcher.py`
  - Verify: `uv run pytest tests/regression/test_phase9_merge.py` passes against `watcher.py` BEFORE the extraction PR; same test still passes after `phases.py` lands.
- [ ] **T2 (P1, human: ~4h / CC: ~30min)** — decision/ — Implement decision/ sub-package
  - Surfaced by: C1 — split decision.py into schema/agent/store/context with `__init__.py` re-exporting public API
  - Files: `hermes_pipeline/decision/{__init__,schema,agent,store,context}.py`
  - Verify: `from hermes_pipeline.decision import HermesSelectionDecision, SelectionContext, run_selection` works; `uv run pytest tests/test_decision_*.py` green.
- [ ] **T3 (P1, human: ~2h / CC: ~15min)** — phases.py — phase_started marker write + stale sweep
  - Surfaced by: A1 — mid-flight in_flight tracking; cron-overlap protection at phase level
  - Files: `hermes_pipeline/phases.py`, `hermes_pipeline/decision/context.py`
  - Verify: marker present during phase execution, absent after terminal; sweep test removes stale markers older than `max_phase_timeout`.
- [ ] **T4 (P1, human: ~3h / CC: ~20min)** — pipeline-tick — Tick-level `.hermes/tick.lock`
  - Surfaced by: XM1 — closes overlapping-cron race and spawn-failure orphan (Codex #2/#3)
  - Files: `hermes_pipeline/decision/__init__.py`, Hermes command definition for `pipeline-tick`
  - Verify: two concurrent `pipeline-tick` invocations result in one call to `run_selection` and one `tick already in flight` exit; spawn failure writes `outcome="failed_to_spawn"`.
- [ ] **T5 (P1, human: ~2h / CC: ~15min)** — decision/agent.py — Prompt SHA pin + loud mismatch alert
  - Surfaced by: A2 + folded Codex #7 — prompt provenance and loud failure on pin mismatch
  - Files: `hermes_pipeline/decision/agent.py`, `hermes_pipeline/circuit.py`
  - Verify: decision records carry `prompt_sha`; mismatch with pinned SHA produces Slack alert and `picked=None` without calling the API; not counted as no-progress.
- [ ] **T6 (P1, human: ~1h / CC: ~10min)** — decision/agent.py — Prompt-injection fences
  - Surfaced by: XM4 — Codex #8
  - Files: `.hermes/prompts/selection.md`, `hermes_pipeline/decision/agent.py`
  - Verify: eval suite `injection_attempt.md` fixture passes; rendered prompt contains `<todos_md_content>...</todos_md_content>` and anti-injection clause.
- [ ] **T7 (P1, human: ~3h / CC: ~20min)** — decision/store.py — Immutable decision + sidecar outcome pattern
  - Surfaced by: XM3 — Codex #5 immutable audit trail
  - Files: `hermes_pipeline/decision/store.py`, `hermes_pipeline/state.py`
  - Verify: writing an outcome never modifies the decision file; `load_recent` returns joined records; rotation moves matching pairs.
- [ ] **T8 (P1, human: ~1h / CC: ~10min)** — state.py — Outcome sidecar writes; remove N7
  - Surfaced by: A3 + XM2 — outcome feedback retained, N7 dropped to remove the self-contradiction
  - Files: `hermes_pipeline/state.py`, `tests/test_state_outcomes.py`
  - Verify: each terminal transition writes a sidecar with the correct outcome value; no decision JSON mutation; legacy clear-on-resolve code paths absent.
- [ ] **T9 (P1, human: ~2h / CC: ~15min)** — cli.py — `pipeline-watch kill [--all|--todo X]`
  - Surfaced by: XM6 — rollback kill switch (Codex #9)
  - Files: `hermes_pipeline/cli.py`, `tests/test_cli_kill.py`
  - Verify: invoking with `--all` issues `hermes run kill` for each `phase_started/*`; writes `killed_by_operator` outcome sidecars; deletes the markers; releases `tick.lock` if held.
- [ ] **T10 (P1, human: ~3h / CC: ~20min)** — circuit.py — No-progress counter + 24h Slack dedup + cron backoff
  - Surfaced by: N5 from approved design (retained shape)
  - Files: `hermes_pipeline/circuit.py`, `tests/test_circuit.py`
  - Verify: 3 consecutive `picked=None` ticks trip backoff and fire exactly one Slack message; second alert within 24h is deduped; first successful pick resets state and restores 5-min interval.
- [ ] **T11 (P1, human: ~1h / CC: ~10min)** — config.py — `.hermes/config.toml` loader, per-tick reload
  - Surfaced by: N6 + A2 — config schema + SHA pin support
  - Files: `hermes_pipeline/config.py`, `tests/test_config.py`
  - Verify: edits to `config.toml` mid-run reflect in the next tick; missing optional fields default cleanly; malformed TOML raises with line.
- [ ] **T12 (P1, human: ~6h / CC: ~45min)** — tests/ — Unit suite for all 49 code paths + 8 user flows
  - Surfaced by: Test review — 0/57 coverage today (green-field)
  - Files: `tests/test_decision_*.py`, `tests/test_phases.py`, `tests/test_state_outcomes.py`, `tests/test_circuit.py`, `tests/test_config.py`
  - Verify: `uv run pytest` reports >= 49 unit tests green; coverage report shows every diagrammed branch hit at least once.
- [ ] **T13 (P1, human: ~2h / CC: ~30min)** — tests/eval/ — Selection-prompt eval suite
  - Surfaced by: T1 — highest-leverage testable surface
  - Files: `tests/eval/selection/`, `tests/eval/runner.py`, `.github/workflows/eval.yml`
  - Verify: 8+ fixtures pass against the current prompt SHA; CI workflow runs and posts result as PR comment; flip to blocking after bake week.
- [ ] **T14 (P2, human: ~2h / CC: ~15min)** — docs/ — Explicit state-machine transition table
  - Surfaced by: Codex #18
  - Files: `docs/pipeline-modularization-plan.md`, `docs/hermes-state-machine.md`
  - Verify: table enumerates every transition (markers → decisions → outcomes → ready_for_review → terminal); links from `decision/README.md`.
- [ ] **T15 (P2, human: ~1h / CC: ~10min)** — docs/ — N8 contract → docstrings + decision/README.md
  - Surfaced by: D1 — collapse standalone N8 doc
  - Files: `hermes_pipeline/decision/__init__.py`, `hermes_pipeline/decision/schema.py`, `hermes_pipeline/decision/README.md`
  - Verify: external Hermes-config repo imports compile against the docstrings; README is < 30 lines and just points at the schemas.
- [ ] **T16 (P2, human: ~30min / CC: ~5min)** — ops — Resolve Open Q1 + Open Q3
  - Surfaced by: Codex #14 / #15
  - Files: `docs/pipeline-modularization-plan.md`
  - Verify: Hermes command repo path is documented; log-routing decision (lean: stdout-only) is documented as the final resolution; both removed from "Open Questions".

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 11 decisions folded, 16 implementation tasks queued |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** Outside voice ran (Codex, read-only, high effort). 18 findings; 6 substantive cross-model tensions surfaced as AskUserQuestion (XM1-XM6). 5 accepted as Codex-recommended, 1 kept current (XM5 cost cap relitigation). Remaining Codex findings (#1 cli wording, #4 stale sweep design-acceptable, #11 eval bake gate documented, #13 architectural alternative declined per office-hours conviction, #16 single-project ≠ single-process — addressed by XM1, #17 model lifecycle — TODO-5, #18 state machine table — T14) were resolved or routed to tasks.
- **CROSS-MODEL:** Eng review and Codex converged on tick lock (XM1), N7 reconciliation (XM2), immutable decisions (XM3), prompt injection guard (XM4), and kill switch (XM6). One genuine disagreement (XM5 hourly cost cap) resolved in favor of the existing design with explicit rationale.
- **VERDICT:** ENG CLEARED — ready to implement after Open Q1 and Open Q3 resolution (T16, blocker for N4 only).

**UNRESOLVED DECISIONS:**
- Open Q1 (Hermes command repo path) — must be resolved before T4/N4 lands; tracked as T16. Does not block T1-T3 or T5-T15 from starting.
- Open Q3 (log routing) — leaning stdout-only with Hermes as log sink; T16 confirms. Does not block implementation start.
