# TODOS

## Metadata

NEXT_TODO_ID: 39

## Entry Schema

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Spec:**, **Reference:**
> - **Spec:**/**Reference:** are `--revise`-only (never suggested by `--add` or auto-research); always typed verbatim
> - ID: sequential, immutable TODO-<n>
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

## Entries

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

- [ ] **TODO-28: Conditional kanban-task registration for optional pipeline phases** — register_todo_phases should skip creating a kanban task entirely when a phase's applicability signal (e.g. Security Review: not-required) says it doesn't apply, instead of dispatching a no-op task.
  - **What:** Add conditional registration to `register_todo_phases` (`kanban_tasks.py`): before creating a kanban task for an optional phase (e.g. `phase_6_1_cso`, and any future QA phase from TODO-24/29), check the TODO's applicability signal (TODOS.md `Decisions:` field, e.g. `Security Review: required/not-required`) and skip task creation entirely when not applicable — rather than the current TODO-24 interim fix (approach A) where the task is always created and the subagent self-checks and exits 0 as a no-op. Requires handling the `--parent` chain gap: the next real phase's `--parent` must point to the last *actually created* task, not literally `task_ids[phase_idx - 1]`.
  - **Why:** Approach A (in-prompt self-check) still creates and dispatches a kanban task every time, wasting a full phase-execution cycle (subprocess spawn, turn budget) even when the phase is a guaranteed no-op. Skipping registration entirely is more correct and avoids the wasted dispatch, but is a real code change to `kanban_tasks.py`'s task-creation loop and `--parent` chaining logic — out of scope for TODO-24 ("revised phases.yaml... not a full pipeline rewrite").
  - **Context:** Surfaced during TODO-24 phase_6_1_cso review — TODO-24 ships approach A (in-prompt guard) as the interim fix; this TODO is the proper follow-up.
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P3`, Effort `M`, Phase `2 (Design)`, Branch `feature/conditional-phase-registration`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-36: Reorganize and refresh README.md's docs table** — Fix broken link, remove stale CLI-name references, group the 30-entry doc table by subsystem, and rewrite Getting Started for real install paths
  - **What:** Restructure the flat 30-row "Documentation" table in README.md (README.md:37-75) into subsystem-grouped sections (pipeline core, multi-project setup, pipeline contract, todos-manager, skill test harness) instead of one undifferentiated table. Fix the broken link `[Install TODOS Manager](tpo-skills-install)` (README.md:63) to point at a real doc/section. Update `docs/pipeline-modularization-plan.md`, which still references the pre-rename `pipeline-watch`/`hermes-pipeline` CLI names instead of `tpo`. Rewrite the Getting Started / Installation section (README.md:103-138) to reflect the actual install and onboarding paths, currently undocumented or misleading:
    1. Document `uv tool install` as the primary install method — the CLI is invoked directly as `tpo ...`, not `uv run tpo ...` (README's current "Run"/"CLI Commands" section (README.md:113-155) exclusively shows `uv run tpo <cmd>`, which only applies to running from a source checkout, not the packaged/installed CLI).
    2. Add an explicit "starting a project from scratch" path (`tpo init <project>` on a project with no TODOS.md yet, tying into `todos-manager --init`).
    3. Add an explicit "adopting tpo on an existing project" path — call out that `todos-manager --convert` (or `--revise` for individual entries) is required to bring a pre-existing/hand-written TODOS.md into the enforced schema before `tpo tick` can select from it.
  - **Why:** The docs table has grown organically to 30 links in one flat list with no grouping beyond a `Quadrant` column, making it hard to find the right doc. One link is broken (points at a CLI command name, not a path). `pipeline-modularization-plan.md` was missed during the `tpo` CLI rename (commits `5fbc837`..`e6aab3a`), leaving stale command names in a doc new contributors are pointed to first. Separately, the Getting Started flow only demonstrates `uv run tpo ...` from a source checkout and never mentions `uv tool install` (the actual distribution model per TODO-34's context) or the two distinct onboarding paths — new project vs. existing project with a pre-existing TODOS.md needing `--convert`/`--revise` — leaving new users without a clear starting point for either case.
  - **Pros:** Easier onboarding via a scannable, grouped doc index; no dead links; consistent CLI naming across all docs; new users get a correct install command and a clear fork for "new project" vs. "existing project" onboarding.
  - **Cons:** Touches a widely-linked file (README.md); requires re-verifying every link after restructuring to avoid introducing new breaks; Getting Started rewrite needs to stay in sync with `docs/tutorial-getting-started.md`, which may itself need the same `uv tool install` correction.
  - **Context:** README.md Documentation table (lines 37-75) and Getting Started / Run sections (lines 103-155); stale references in docs/pipeline-modularization-plan.md; CLI rename history in TODO-33 (already-committed `tpo` rename) and TODO-34/35 (skills install follow-ons); `todos-manager --convert`/`--revise` subcommands (docs/howto-todos-manager.md) as the existing-project onboarding mechanism.
  - **Spec:** docs/superpowers/plans/2026-07-28-todo-36-readme-refresh.md
  - **Reference:** docs/superpowers/specs/2026-07-28-todo-36-readme-refresh-design.md
  - **Decisions:** Priority `P2`, Effort `M`, Phase `4 (Development)`, Branch `docs/reorganize-readme-docs-table`, Test Coverage `not-required`, Security Review `not-required`, UI Review `not-required`
