# TODOS.md → GitHub Issues migration

- Date: 2026-08-29 08:11 UTC
- Repository: hyonchoi/todo-pipeline-orch
- Labels: missing vocabulary/phase/legacy-id labels were created; existing labels are left untouched, including color/description.

## Mapping

| Legacy ID | Issue | Title | Plan | Branch |
|---|---|---|---|---|
| TODO-4 | #62 | build a massive integration test project for Hermes, Kanban, and Claude Code | (none) | feature/massive-integration-test-project |
| TODO-5 | #63 | selection-agent model lifecycle policy | (none) | feature/selection-model-fallback |
| TODO-23 | #64 | Harden kanban-as-scheduler edge cases in harness.py | (none) | feature/harden-kanban-scheduler-edge-cases |
| TODO-28 | #65 | Conditional kanban-task registration for optional pipeline phases | (none) | feature/conditional-phase-registration |
| TODO-39 | #66 | Revise selection prompt for plan-gated worktree execution | (none) | feature/plan-gated-worktree-selection |
| TODO-42 | #67 | Add per-project prompt client overrides | (none) | feature/per-project-prompt-client |
| TODO-43 | #68 | Refactor production orchestration hotspots | docs/superpowers/plans/2026-08-20-todo-43-production-orchestration-hotspots.md | feature/todo-43-orchestration-hotspots-impl |

## Not imported

- TODO-36: `[x]` done in TODOS.md (not yet archived)
- TODO-40: `[x]` done in TODOS.md (not yet archived)

## Dependencies

Edges created (`blocked_by`):

- (none)

Already satisfied (dependency outside this run):

- TODO-4 → TODO-2: done
- TODO-4 → TODO-3: done
- TODO-5 → TODO-2: done
- TODO-5 → TODO-3: done
- TODO-23 → TODO-20: done
- TODO-28 → TODO-24: done
- TODO-42 → TODO-41: done

## Plan manifests to rewrite

- `docs/superpowers/plans/2026-08-20-todo-43-production-orchestration-hotspots.md`: set manifest `todo_id` to `TODO-68` (currently `TODO-43`) — done in the runtime cutover commit

## Legacy references

Legacy `TODO-<n>` identifiers remain in plan filenames (`docs/superpowers/plans/*-todo-<n>-*.md`), branch names (`feature/todo-<n>-*`), `docs/pipeline/TODO-25-*`, and issues #16 and #21 carry legacy `TODO-N:` title prefixes. Each migrated issue carries `legacy-id:TODO-<n>` and a `### Legacy ID` section so those references stay resolvable.
