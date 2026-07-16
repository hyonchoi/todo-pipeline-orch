# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

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

- [x] **TODO-19: partial impl of TODO-4, integration test data that is repeatable, verifiable mock data for whole pipeline from start to the end** — Repeatable mock integration test harness for pipeline end-to-end verification
  - **What:** Five deliverables: (1) setup script + mock project fixtures (git, preset, TODOS.md) in temp dir, (2) pipeline execution through mock project, (3) monitoring/verification of pipeline steps and kanban status, (4) findings report generation, (5) loopable 1-4 for iterative fix cycles. Assumes local running Hermes configuration.
  - **Why:** TODO-4 (end-to-end integration harness) is P1 with no implementation progress. This partial impl creates repeatable, verifiable mock data — a prerequisite for debugging cross-system bugs (Hermes + Kanban + Claude Code) without manual setup each time. Prior test infra (TODO-16) is structural-only; no integration-level fixtures exist.
  - **Pros:** Deterministic reproduction for cross-system debugging, reusable fixtures for future integration tests, validates pipeline end-to-end without prod data
  - **Cons:** Temp dir setup may not capture all edge cases of a real project. Hermes-local assumption limits portability. Report generator is a new artifact to maintain.
  - **Context:** TODO-4 (parent), TODO-16 (skill-test-environment Phase 1, tests/skill-test-environment/). Pipeline modules: hermes_pipeline/decision/, hermes_pipeline/kanban.py, hermes_pipeline/runner.py, hermes_pipeline/phases.py
  - **Depends on:** `TODO-4`, `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P1`, Effort `L`, Phase `4 (Development)`, Branch `feature/integration-test-harness`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.4.11 (2026-07-15)

- [x] **TODO-20: Add `--kanban {null,hermes}` option to `hermes-pipeline test`** — Let the mock integration harness exercise the real HermesKanbanAdapter, not just NullKanbanAdapter
  - **What:** Add a `--kanban {null,hermes}` CLI flag to the `test` subcommand (`cli.py:566`), thread it through `run_harness()` (`harness.py:352`), and when `hermes` is selected, construct `HermesKanbanAdapter(outbox, active_tasks)` wired to `KanbanOutbox`/`ActiveTasksStore` paths under the fixture's temp `state_dir`, instead of hardcoding `NullKanbanAdapter()` (`harness.py:410`).
  - **Why:** The harness currently can't validate real kanban sync behavior (task creation, phase comments, complete/archive) end-to-end against a mock project — it silently no-ops. TODO-19's harness already runs real `hermes`/`claude` subprocesses for phases; kanban is the one system left mocked.
  - **Pros:** Closes the last gap in true end-to-end pipeline verification; reuses the existing `HermesKanbanAdapter`/outbox machinery with no new abstractions.
  - **Cons:** Real kanban calls against a mock tenant require a reachable `hermes kanban` backend/tenant — may need a dedicated test tenant or additional mocking at the `hermes kanban` CLI boundary to stay hermetic.
  - **Context:** `docs/howto-mock-integration-test-harness.md` also got a new "Run with real kanban adapter" step documenting the flag.
  - **Depends on:** `TODO-19`
  - **Assumptions:** A test/mock kanban tenant is available or acceptable for CI use; if not, this TODO may need to scope down to "outbox/dry-run verification only."
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Branch `feature/harness-real-kanban-adapter`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.5.0 (2026-07-16)

- [ ] **TODO-22: Fine-grained checklist for harness test coverage of production code paths** — Based on the production (pipeline-watch) code and docs, a checklist to verify the harness test meets all requirements by exercising production code paths.
  - **What:** Create a fine-grained checklist mapping each harness capability to the corresponding production pipeline-watch code path, covering: phase execution (phases.run), kanban-as-scheduler (kanban_tasks.register_todo_phases, get_todo_kanban_status, all_phases_complete, observe_outcomes), contract resolution (contract.load_contract), convergence/circuit breaker (circuit.CircuitBreaker), state management (state.State, tick.TickLock), error handling (hermes_adapter error types), timeout/kill (killpg), gate handling (gates.check_gate_status), preflight, and config isolation. Each item verifies the harness uses (not re-implements) the production function.
  - **Why:** There's no structured way to verify the harness actually tests pipeline-watch behavior vs a parallel implementation. The harness re-implements convergence detection, gate handling, phase dispatch, error classification, and timeout handling — each needs a checklist item mapped to the production code path it should exercise. The checklist serves as acceptance criteria for TODO-21.
  - **Pros:** Provides measurable acceptance criteria for TODO-21 refactor, ensures no production path is left untested by the harness, serves as a regression checklist for future harness changes
  - **Cons:** Checklist may grow stale as production code evolves — needs periodic refresh. Some production paths (multi-project scan, slack alerts) may not be harness-applicable.
  - **Context:** docs/reference-kanban-as-scheduler.md, docs/howto-mock-integration-test-harness.md, docs/hermes-state-machine.md, hermes_pipeline/ (contract.py, circuit.py, runner.py, phases.py, tick.py, kanban_tasks.py, state.py)
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `worktree-todo21-harness-prod-reuse`, Test Coverage `not-required`, Security Review `not-required`

- [ ] **TODO-21: Revise pipeline harness to maximum use of production module/code** — The harness is to test/verify/validate the production (pipeline-watch) code. Currently, harness is written in its own logic code. It must use production code/function as much as possible to test the production.
  - **What:** Refactor harness.py to import and delegate to production modules instead of custom implementations — e.g., use production runner, config loading, state management, tick generation, error classification, and convergence detection. Keep only fixture/seed logic in the harness.
  - **Why:** The harness re-inplements logic that already exists in production modules (runner, circuit, state, config, tick, hermes_adapter, phases). This means bugs fixed in production don't benefit the harness, and harness fixes never reach production — defeating the purpose of a test/verification tool. Reusing production code paths ensures the harness actually validates pipeline-watch behavior.
  - **Pros:** Single source of truth for pipeline logic, harness tests become integration tests (not unit tests of a parallel implementation), production bug fixes automatically improve harness coverage
  - **Cons:** Tight coupling to production internals means API changes in production modules break the harness. Requires careful dependency graph analysis to avoid circular imports. Some production functions have side effects (markers, subprocess spawns) that need fixture isolation.
  - **Context:** harness.py (28.7KB, 20 top-level symbols), production modules: cli.py, runner.py, phases.py, contract.py, kanban_tasks.py, state.py, circuit.py, tick.py, config.py, hermes_adapter.py
  - **Depends on:** `TODO-19`, `TODO-20`, `TODO-22`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `worktree-todo21-harness-prod-reuse`, Test Coverage `required`, Security Review `not-required`

