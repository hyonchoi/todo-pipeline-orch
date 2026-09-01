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

## Upgrade precondition and rollback

**Upgrade precondition:** no active schema-1 runs. `tpo doctor <project>` reports each as `REGISTRATION UNSUPPORTED: schema_version 1; finish or abandon this run before upgrading`. Finish or abandon (see [docs/howto-debugging-and-recovery.md](../howto-debugging-and-recovery.md)) before deploying this version. No v1→v2 registration conversion exists, by design. The selection prompt SHA changed; re-pin `selection.expected_prompt_sha` to `11c04ee5cf7fb92e369cc3e095b9f62aef28f52ea815ffd09486ee818032bf6e` ([docs/howto-prompt-sha-mismatch.md](../howto-prompt-sha-mismatch.md)).

**Rollback (this version → previous):**

1. Block: if any v2 `registration.json` under `<state>/runs/` is active (no `issue-closed`/`abandoned` marker), abandon those runs first (archive their Kanban cards per the recovery how-to; `touch <state>/runs/<tick>/abandoned`). v1 cannot read v2 registrations. Acceptance: on the reverted version `tpo doctor <project>` reports no active registration.
2. `git revert` the PR series in reverse order (restores TODOS.md with the seven entries as `[ ]`, the skill, and the tests; the revert is clean because no task edited the migrated entries' content).
3. Issue-state reconciliation using the mapping table above: remove `tpo:in-progress` from migrated issues; for any issue closed by a v2 closeout whose TODO is not `[x]` in the restored TODOS.md, either mark the TODO `[x]` (v1 `tpo todos complete`) or `gh issue reopen` — record the choice here. Other issues may stay open (harmless) or be closed with `--comment "rolled back to TODOS.md"`.
4. Verify: `uv run pytest`, `tpo doctor <project>`, one `tpo tick` on a fixture project.

Created labels (`tpo:todo`, `tpo:on-hold`, `tpo:in-progress`, triage vocabulary, `legacy-id:*`) are inert and may stay.
