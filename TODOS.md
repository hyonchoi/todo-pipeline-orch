# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

## Harness

- [ ] **TODO-4: build a massive integration test project for Hermes, Kanban, and Claude Code** — End-to-end phase progression harness
  - **What:** Build an automated, step-by-step integration harness on a dedicated test project with mock TODOs, driving real phase progression across Hermes, Kanban, and Claude Code.
  - **Why:** Current behavior is hard to debug once Kanban and Claude Code interact, especially around blocking decisions and late-phase review transitions.
  - **Pros:** Provides deterministic reproduction for cross-system bugs, exposes status drift clearly, and creates a concrete debug surface for decision-gated phases.
  - **Cons:** Expensive to build/maintain and may require fixtures, logging hooks, and orchestration around blocking prompts.
  - **Context:** The harness should seed representative TODOs, progress each phase, and record status transitions plus stalls/mismatches across all three systems.
  - **Depends on:** `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P1`, Effort `L`, Phase `4 (Development)`, Branch `feature/massive-integration-test-project`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-5: selection-agent model lifecycle policy** — Pinned model + documented fallback ladder
  - **What:** Add a model-lifecycle policy in `.hermes/config.toml`: pinned `selection.model` (already shipping with TODO-2/3) plus `selection.model_fallback` ladder + alert behavior on Anthropic API deprecation (e.g., 404 on the pinned model id).
  - **Why:** TODO-2/3 hardcode `claude-opus-4-7` with no plan for the day Anthropic retires that model id. Without a documented fallback path, the first deprecation produces silent shadow-mode failures one morning.
  - **Pros:** Cheap insurance once the fallback mechanic is understood; aligns model handling with the prompt SHA pinning pattern from TODO-2/3; one-time decision.
  - **Cons:** Adds two config knobs; the fallback ladder needs revisiting as Anthropic's model lineup shifts. Designing cold is partial guesswork — better with one deprecation event of empirical data.
  - **Context:** Builds on TODO-2/3 once `config.py` and `decision/agent.py` exist. Today's design fails loudly on 404 (acceptable for v1). Revisit when Anthropic announces opus-4-7 EOL.
  - **Depends on:** `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P3`, Effort `S`, Phase `2 (Design)`, Branch `feature/selection-model-fallback`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-23: Harden kanban-as-scheduler edge cases in harness.py** — Fix timeout/hang gaps found in TODO-20 adversarial review
  - **What:** Address remaining edge cases in `_poll_kanban_phases`/`_run_with_timeout` surfaced by Codex and Claude adversarial review of TODO-20: (1) daemon polling thread stays alive after `_run_with_timeout` times out in `--kanban hermes` mode, with no stop/archive/cancel of registered kanban tasks before temp project cleanup; (2) `--phase <gate> --kanban hermes` creates a single blocked gate task with no predecessor entry, hanging until overall timeout; (3) unrecognized/unknown kanban status value causes silent infinite poll loop; (4) `ConvergenceHaltError` may be masked by a simultaneous worker-join timeout race; (5) `_auto_complete_gate_tasks` idempotency under status flapping is unverified, now more relevant since a fix in TODO-20 broadened which transitions call it.
  - **Why:** These are known correctness/robustness gaps in the kanban-as-scheduler harness path, identified but out of scope for TODO-20's core `--kanban` flag delivery. Left unaddressed, they can cause silent hangs or leaked background threads in `--kanban hermes` test runs.
  - **Depends on:** `TODO-20`
  - **Decisions:** Priority `P2`, Effort `M`, Phase `4 (Development)`, Branch `feature/harden-kanban-scheduler-edge-cases`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-24: Refine gstack's phases.yaml — review phase composition and instructions** — Audit `hermes_pipeline/data/profiles/gstack/phases.yaml` for phase composition and per-phase instruction quality
  - **What:** Review `hermes_pipeline/data/profiles/gstack/phases.yaml` (9 phases: autoplan → plan_gate → writing_plan → development → review → cso → document_release → finish_branch → ship_gate) for: (1) phase composition — are these the right phases, in the right order, with correct gates; (2) each phase's `prompt` field — is the instruction clear, correctly scoped, and consistent with the underlying skill's current behavior; (3) `tools`/`turns`/`timeout` budgets — are they still appropriate. Deliverable is a revised phases.yaml (or a design doc proposing changes) — not a full pipeline rewrite.
  - **Why:** The 9-phase pipeline was assembled incrementally via prior TODOs (TODO-6/7/8), each adding/replacing a phase in isolation. No holistic review has been done of phase ordering, tool grants, turn/timeout budgets, or prompt wording as a set.
  - **Pros:** Catches stale/inconsistent phase prompts before they cause pipeline failures; opportunity to right-size turn/timeout budgets based on real run history.
  - **Cons:** Risk of scope creep into a full pipeline redesign; changes to phase prompts affect live orchestrator runs.
  - **Context:** Related prior work — `TODO-7` (added phase_5_review) and `TODO-8` (replaced phase_8 ship gate), both in TODOS-archive.md.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Branch `feature/refine-phases-yaml`, Test Coverage `not-required`, Security Review `not-required`

- [ ] **TODO-25: TODOS.md: add optional Spec/Reference field, threaded into autoplan phase prompt** — Add a `**Spec:**` field to the TODOS.md schema; when present, phase_2_autoplan reads and passes that file to the autoplan skill.
  - **What:** Add an optional `**Spec:**` (or `**Reference:**`) field to the TODOS.md entry schema pointing to a spec/reference md file path. When a TODO entry has this field, `phases.py`'s `_render_phase_prompt` for `phase_2_autoplan` must read the field, and inject the file path/content into the autoplan prompt so the skill runs off that doc rather than only the inline TODO text.
  - **Why:** Currently phase_2_autoplan only sees the TODO's inline What/Why/Context — there's no way to hand it a fuller spec doc when one exists, forcing either inline bloat in TODOS.md or the spec being invisible to the pipeline.
  - **Context:** Surfaced during TODO-24 phase_2_autoplan review — schema change lives in `todos-manager` skill (preamble field list) plus `hermes_pipeline/phases.py` (`_render_phase_prompt`, `load_phases`).
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P2`, Effort `S`, Phase `2 (Design)`, Branch `feature/todos-spec-field`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-26: Remove dead plan-gate code after phase_2b_plan_gate removal** — Delete approve_plan.py, its CLI subcommand, and gates.py plan-gate logic once phases.yaml drops phase_2b.
  - **What:** Once `phase_2b_plan_gate` is removed from `phases.yaml` (TODO-24), delete the now-dead plan-gate machinery: `hermes_pipeline/approve_plan.py` (entire module), the `approve-plan` CLI subcommand in `cli.py` (parser + `_cmd_approve_plan`), and the plan-gate-specific logic in `gates.py` (`PLAN_GATE_PHASE_KEY`, `is_high_risk` status-map handling) and `gate_state.py` (confirm `gate_status()`'s default arg/callers still make sense with only `phase_9_ship` using `gate: true`), and `kanban_tasks.py`'s `all_phases_complete` partial-registration guard (~line 443: the `if key == "phase_2b_plan_gate":` branch that treats a rejected/archived gate task as a completion signal) — this branch stops matching anything once phase_2b is removed from phases.yaml and must be deleted alongside the rest.
  - **Why:** `approve-plan` was exclusively wired to `phase_2b_plan_gate` (`PLAN_GATE_PHASE_KEY = "phase_2b_plan_gate"`); once that gate is removed the CLI, its handler, and gates.py's plan-gate branch become unreachable dead code.
  - **Context:** Surfaced during TODO-24 phase_2b review — verified via grep that `approve_plan.py`/`PLAN_GATE_PHASE_KEY` have no other callers; `phase_9_ship` uses separate gate logic in `ship.py`.
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Branch `feature/remove-plan-gate-dead-code`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-27: Fix test harness to drive real kanban-task pipeline; remove dead phases.run/watcher.py path** — harness.py bypasses register_todo_phases and calls phases.run directly — violates the harness-must-match-production invariant; watcher.py is unreferenced dead code.
  - **What:** `harness.py:418` calls `phases.run` directly (via `from .phases import run as phases_run`), completely bypassing `register_todo_phases`/kanban-task creation — the actual production path (`cli.py`'s `_cmd_tick` → `register_todo_phases` → kanban `--goal` tasks with the rendered prompt baked in at creation time). This means the harness exercises a different code path (`phases.run` → `_invoke_hermes`/`_invoke_review_phase` → `review_phase.py`'s PRE/subagent/POST split for phase_5) than production ever runs. Also, `watcher.py`'s `run_phase()` is imported nowhere in the codebase — confirmed dead code. Fix: rewire the harness to register real kanban tasks via `register_todo_phases` and drive them through the same dispatch production uses; then delete `watcher.py`, `phases.run`, `_invoke_hermes`, `_invoke_review_phase`, and `review_phase.py`'s PRE/POST machinery (`capture_pre_review_state`, `finalize_review`, `run_pytest`, `commit_all`) if nothing in the new harness needs them.
  - **Why:** Violates the harness-must-mirror-production invariant (established in a prior completed TODO). Confirmed via grep that only `harness.py` and self-referencing `watcher.py` import `phases.run`; production's real flow (`cli.py`, `contract.py`, `kanban_tasks.py`) only ever imports `load_phases`/`_render_phase_prompt` — pure data, never the executing function. Surfaced while reviewing TODO-24's phase_5_review prompt: discovered the prompt's "pipeline runs tests/commits after you" claim describes a code path (`finalize_review`) that never executes for real kanban-dispatched tasks — the prompt text is the only real lever in production, so it needs redesigning to own test-run-and-commit itself, but the current dead PRE/POST code is misleading anyone reading `review_phase.py` into thinking it's live.
  - **Context:** Discovered via grep during TODO-24 phase_5_review review — `register_todo_phases` (`kanban_tasks.py:66`) renders the full prompt into the kanban task body at creation time; nothing dispatches phases dynamically afterward in production. Also folds in the `phase_9_ship` → `phase_9_human_review` phase_key rename (TODO-24 renamed the display `name` only): update `hermes_pipeline/ship.py:29` (`GATE_PHASE_KEY = "phase_9_ship"`), the matching comment in `cli.py:1122`, and `phases.py:326`'s `f"todo-{todo_num}-{phase_key}"` (part of the dead harness path already being deleted here) together with the phases.yaml key change.
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P1`, Effort `L`, Phase `2 (Design)`, Branch `feature/fix-harness-production-parity`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-28: Conditional kanban-task registration for optional pipeline phases** — register_todo_phases should skip creating a kanban task entirely when a phase's applicability signal (e.g. Security Review: not-required) says it doesn't apply, instead of dispatching a no-op task.
  - **What:** Add conditional registration to `register_todo_phases` (`kanban_tasks.py`): before creating a kanban task for an optional phase (e.g. `phase_6_1_cso`, and any future QA phase from TODO-24/29), check the TODO's applicability signal (TODOS.md `Decisions:` field, e.g. `Security Review: required/not-required`) and skip task creation entirely when not applicable — rather than the current TODO-24 interim fix (approach A) where the task is always created and the subagent self-checks and exits 0 as a no-op. Requires handling the `--parent` chain gap: the next real phase's `--parent` must point to the last *actually created* task, not literally `task_ids[phase_idx - 1]`.
  - **Why:** Approach A (in-prompt self-check) still creates and dispatches a kanban task every time, wasting a full phase-execution cycle (subprocess spawn, turn budget) even when the phase is a guaranteed no-op. Skipping registration entirely is more correct and avoids the wasted dispatch, but is a real code change to `kanban_tasks.py`'s task-creation loop and `--parent` chaining logic — out of scope for TODO-24 ("revised phases.yaml... not a full pipeline rewrite").
  - **Context:** Surfaced during TODO-24 phase_6_1_cso review — TODO-24 ships approach A (in-prompt guard) as the interim fix; this TODO is the proper follow-up.
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P3`, Effort `M`, Phase `2 (Design)`, Branch `feature/conditional-phase-registration`, Test Coverage `required`, Security Review `not-required`

## Completed
