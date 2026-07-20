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

## Completed
