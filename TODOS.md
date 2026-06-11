# TODOS

gstack-format work queue for `todo-pipeline-orchestrator`. Each entry keeps the required fields: What/Why/Pros/Cons/Context/Depends on/Decisions. Status markers: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold. See `docs/gstack/hyonchoi-main-design-20260610-195349.md` ("TODOS Manager Skill") for the full schema and `TODO-<n>` ID assignment rules.

- [ ] **TODO-1: todos-manager counter recovery mode** — Add `todos-manager --recover-counter`
  - **What:** Add `todos-manager --recover-counter` that scans `TODOS.md` for the max existing `TODO-<n>` ID and initializes `.hermes/todo_id_counter` to that value.
  - **Why:** Prevent ID collisions when bootstrapping a project that already has hand-written `TODO-<n>` entries but no counter file yet.
  - **Pros:** Closes the only remaining gap in the todos-manager spec. Small and isolated implementation.
  - **Cons:** Not needed until a project has pre-existing `TODO-<n>` entries without a counter file, so it does not block current work.
  - **Context:** See `docs/gstack/hyonchoi-main-design-20260610-195349.md` section "TODOS Manager Skill (`todos-manager`)" and the "NOT in scope" / Test Plan note.
  - **Depends on:** none
  - **Decisions:** Priority `P3`, Effort `S`, Phase `4 (Development)`, Branch `feature/todos-manager-counter-recovery`, Test Coverage `필요`, Security Review `불필요`

- [ ] **TODO-2: use Hermes agent for TODO parsing and selection** — Agent-first parsing for irregular TODO files
  - **What:** Make TODO parsing and task selection rely on the Hermes agent with an explicit instruction layer instead of assuming a fully strict file schema.
  - **Why:** The project must extract useful task data from irregular TODO formats and still select the correct task even when structure is partial.
  - **Pros:** Handles real-world TODO files, improves selection accuracy for noisy structure, and aligns behavior with project requirements.
  - **Cons:** Adds prompt-design and evaluation work beyond regex parsing. May require stricter validation for deterministic selection.
  - **Context:** Applies to TODO ingestion and selection behavior across the Hermes pipeline where TODO structure can be mixed or inconsistent.
  - **Depends on:** none
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/hermes-todo-selection`, Test Coverage `필요`, Security Review `불필요`

- [ ] **TODO-3: route non-Hermes process spawning through Hermes commands** — Hermes as the only process control surface
  - **What:** Require all process-spawning paths, except direct execution of `hermes ...` itself, to route through Hermes commands instead of invoking tools directly.
  - **Why:** Direct non-Hermes process execution creates behavior drift, bypasses intended control surfaces, and weakens the Hermes-centered execution model.
  - **Pros:** Keeps orchestration aligned with the Hermes contract, centralizes execution policy, and reduces hidden shell integrations.
  - **Cons:** Increases coupling to Hermes command/skill coverage and may require refactors where code shells out to system tools.
  - **Context:** Examples include using `hermes cron ...` instead of `crontab`, and routing Claude Code invocation through Hermes-managed skill paths.
  - **Depends on:** none
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/hermes-process-routing`, Test Coverage `필요`, Security Review `불필요`

- [ ] **TODO-4: build a massive integration test project for Hermes, Kanban, and Claude Code** — End-to-end phase progression harness
  - **What:** Build an automated, step-by-step integration harness on a dedicated test project with mock TODOs, driving real phase progression across Hermes, Kanban, and Claude Code.
  - **Why:** Current behavior is hard to debug once Kanban and Claude Code interact, especially around blocking decisions and late-phase review transitions.
  - **Pros:** Provides deterministic reproduction for cross-system bugs, exposes status drift clearly, and creates a concrete debug surface for decision-gated phases.
  - **Cons:** Expensive to build/maintain and may require fixtures, logging hooks, and orchestration around blocking prompts.
  - **Context:** The harness should seed representative TODOs, progress each phase, and record status transitions plus stalls/mismatches across all three systems.
  - **Depends on:** `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P1`, Effort `L`, Phase `4 (Development)`, Branch `feature/massive-integration-test-project`, Test Coverage `필요`, Security Review `불필요`

