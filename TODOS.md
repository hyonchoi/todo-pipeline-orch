# TODOS

gstack-format work queue for `todo-pipeline-orchestrator`. Each entry: What/Why/Pros/
Cons/Context/Depends on/Decisions. Status markers: `[ ]` pending, `[→]` in progress,
`[x]` done, `[~]` on hold. See `docs/gstack/hyonchoi-main-design-20260610-195349.md`
("TODOS Manager Skill") for the full schema and `TODO-<n>` ID assignment rules.

## TODO-1: todos-manager counter recovery mode

**What:** Add `todos-manager --recover-counter` — scans TODOS.md for the max existing
`TODO-<n>` heading and initializes `.hermes/todo_id_counter` to N.

**Why:** Prevents ID collisions when bootstrapping a project that already has
hand-written `TODO-<n>` entries but no counter file yet.

**Pros:**
- Closes the only gap left by the todos-manager spec.
- Cheap — small, isolated addition to the skill.

**Cons:**
- Not needed until a project actually has pre-existing `TODO-<n>` entries without a
  counter file — doesn't block current work.

**Context:** See "NOT in scope" / Test Plan note in
`docs/gstack/hyonchoi-main-design-20260610-195349.md`, section "TODOS Manager Skill
(`todos-manager`)".

**Depends on:** none

**Decisions:**
- Priority: P3
- Effort: S
- Phase: 4 (Development)
- Branch: feature/todos-manager-counter-recovery
- Test Coverage: 필요
- Security Review: 불필요

Status: `[ ]`

## TODO-2: use Hermes agent for TODO parsing and selection

**What:** Make TODO file parsing and task selection rely on the Hermes agent with
an explicit instruction layer, instead of assuming the TODO file always follows a
fully well-defined format.

**Why:** This project still needs to understand useful task information from
irregular TODO formats and select the correct item even when the file does not
strictly match the expected schema.

**Pros:**
- Handles real-world TODO files instead of only idealized gstack-formatted input.
- Improves selection accuracy when structure is partial, inconsistent, or noisy.
- Aligns parsing behavior with the stated project requirement.

**Cons:**
- Adds agent-prompt design and evaluation work beyond simple regex parsing.
- May require tighter validation to keep agent-driven selection deterministic.

**Context:** Applies to TODO ingestion and selection behavior across the Hermes
pipeline, especially where TODO documents contain mixed or irregular structure.

**Depends on:** none

**Decisions:**
- Priority: P1
- Effort: M
- Phase: 2 (Design)
- Branch: feature/hermes-todo-selection
- Test Coverage: 필요
- Security Review: 불필요

Status: `[ ]`

## TODO-3: route non-Hermes process spawning through Hermes commands

**What:** Require every process-spawning path in this project, except direct
execution of the `hermes ...` command itself, to go through Hermes instead of
invoking underlying tools directly. Examples: cron registration must use
`hermes cron ...` instead of `crontab`; Claude Code invocation must go through
the Hermes `/claude-code` skill or an explicit verbal indication such as
"via claude-code client".

**Why:** This project is tightly coupled to the Hermes agent, so direct process
execution outside Hermes creates behavior drift, bypasses the intended control
surface, and weakens the agent-centered execution model.

**Pros:**
- Keeps orchestration behavior aligned with the Hermes agent contract.
- Centralizes execution policy, routing, and future instrumentation in Hermes.
- Reduces hidden direct shell integrations that can diverge from agent intent.

**Cons:**
- Increases coupling to Hermes command coverage and skill interfaces.
- May require refactors where the code currently shells out to system tools.

**Context:** Applies to all process spawning in this repository and related
automation surfaces. The only allowed direct process execution is the `hermes`
command itself; all downstream operations should be expressed as Hermes
subcommands or Hermes-mediated skill calls.

**Depends on:** none

**Decisions:**
- Priority: P1
- Effort: M
- Phase: 2 (Design)
- Branch: feature/hermes-process-routing
- Test Coverage: 필요
- Security Review: 불필요

Status: `[ ]`

## TODO-4: build a massive integration test project for Hermes, Kanban, and Claude Code

**What:** Create an automated step-by-step integration test around a dedicated test project with mock TODOs, then drive real phase progression through Hermes, Kanban, and Claude Code while tracing status changes and interaction boundaries end to end.

**Why:** The current workflow is hard to debug once Kanban and Claude Code are involved, especially when Claude Code blocks on a required decision while a subagent is running. We need a reproducible test harness that exposes exactly what happens at each phase transition, with extra attention on review behavior and the user decision immediately before the last phase.

**Pros:**
- Gives a deterministic repro path for cross-system bugs that are currently hard to observe.
- Makes status drift visible across Hermes, Kanban, and Claude Code instead of inferring it after the fact.
- Creates a concrete debugging surface for review-phase behavior and decision-gated execution.

**Cons:**
- Expensive to build and maintain because it depends on real multi-system interaction.
- May require dedicated fixtures, logging hooks, and non-trivial orchestration around blocking prompts.

**Context:** The test should spin up a realistic mock project, seed representative TODOs, progress them through each pipeline phase, and record status transitions plus any unexpected stops, stalls, or mismatches across Hermes, Kanban, and Claude Code. The highest-risk area is the review phase and the user-decision checkpoint right before the final phase, where current behavior is still unclear.

**Depends on:** TODO-2, TODO-3

**Decisions:**
- Priority: P1
- Effort: L
- Phase: 4 (Development)
- Branch: feature/massive-integration-test-project
- Test Coverage: 필요
- Security Review: 불필요

Status: `[ ]`
