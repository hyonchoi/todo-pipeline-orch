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
