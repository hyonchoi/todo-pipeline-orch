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

- [x] **TODO-17: Add `--list` subcommand to todos-manager** — List existing active todos, `--all` flag includes archived
  - **What:** Add a `--list` subcommand to the todos-manager skill that displays all active TODO entries from TODOS.md in a formatted, readable summary showing ID, status, title, and summary for each entry. Support an optional `--all` flag that also includes entries from TODOS-archive.md.
  - **Why:** Users frequently need to see what TODOs exist without running a full audit. A lightweight listing command provides quick visibility into project state and reduces context-switching overhead.
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `debug/todos-manager`, Test Coverage `불필요`, Security Review `불필요`
  - **Completed:** v0.4.8 (2026-07-13)

- [x] **TODO-18: add `--revise` subcommand for fixing existing TODOs** — Fill missing fields or refine decisions after audit/convert
  - **What:** Add a `--revise` subcommand to the todos-manager skill: select an existing TODO-<n> from TODOS.md, scan for missing or weak fields (What, Why, Decisions, Branch, etc.), auto-research the codebase to pre-fill gaps, present a confirm/edit gate, and write the updated entry back to disk. Reuses the auto-research phase from `--add`.
  - **Why:** `--audit` and `--convert` are report-only with no path to fixing discovered issues. After an audit surfaces missing What/Why/Decisions or incomplete fields, users have no skill-driven workflow to fill the gaps — they must manually edit TODOS.md.
  - **Pros:** Closes the audit→fix loop without manual markdown editing. Reuses auto-research from `--add` for gap filling. Keeps entries schema-compliant after `--convert` migration.
  - **Cons:** Adds a subcommand to an already feature-rich skill. Edge case: revising archived entries may surprise users who expect archives to be append-only.
  - **Context:** Skill source: `skills/todos-manager/SKILL.md` (section-skeleton pattern). Related entries in this repo's TODOS-archive.md were flagged by the prior audit (TODO-9, TODO-11, TODO-12, TODO-13, TODO-14, TODO-15 missing What/Why/Branch).
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Branch `feat/todos-manager-revise`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.4.10 (2026-07-14)
