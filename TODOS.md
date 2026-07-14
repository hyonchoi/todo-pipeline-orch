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
  - **Decisions:** Priority `P1`, Effort `L`, Phase `4 (Development)`, Branch `feature/massive-integration-test-project`, Test Coverage `필요`, Security Review `불필요`

- [ ] **TODO-5: selection-agent model lifecycle policy** — Pinned model + documented fallback ladder
  - **What:** Add a model-lifecycle policy in `.hermes/config.toml`: pinned `selection.model` (already shipping with TODO-2/3) plus `selection.model_fallback` ladder + alert behavior on Anthropic API deprecation (e.g., 404 on the pinned model id).
  - **Why:** TODO-2/3 hardcode `claude-opus-4-7` with no plan for the day Anthropic retires that model id. Without a documented fallback path, the first deprecation produces silent shadow-mode failures one morning.
  - **Pros:** Cheap insurance once the fallback mechanic is understood; aligns model handling with the prompt SHA pinning pattern from TODO-2/3; one-time decision.
  - **Cons:** Adds two config knobs; the fallback ladder needs revisiting as Anthropic's model lineup shifts. Designing cold is partial guesswork — better with one deprecation event of empirical data.
  - **Context:** Builds on TODO-2/3 once `config.py` and `decision/agent.py` exist. Today's design fails loudly on 404 (acceptable for v1). Revisit when Anthropic announces opus-4-7 EOL.
  - **Depends on:** `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P3`, Effort `S`, Phase `2 (Design)`, Branch `feature/selection-model-fallback`, Test Coverage `필요`, Security Review `불필요`

