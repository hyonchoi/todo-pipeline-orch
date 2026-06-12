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
