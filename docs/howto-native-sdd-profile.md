# How to Use the Native SDD Profile

The `native-sdd` profile is the Plan-to-Kanban compiler. The production flow is
`Hermes cron -> TPO tick -> eligible TODO -> pinned worktree -> Kanban ->
worker`. Hermes >= 0.19.0 dispatches workers; TPO never invokes Claude or Codex
directly.

## Prepare the TODO

Store an implementation-ready Plan inside the project and attach it to the
TODO with a project-relative path:

```markdown
- [ ] **TODO-42** Example change
  - **Plan:** docs/plans/TODO-42.md
```

The resolved path must remain inside the project and identify a readable
regular file. Missing, duplicate, absolute, escaping, symlink-escaping, or
unreadable Plan targets fail before tick state or kanban tasks are created.

For visible per-task execution, embed exactly one manifest (maximum 50 tasks):

```json tpo-plan
{
  "schema_version": 1,
  "todo_id": "TODO-42",
  "tasks": [{
    "id": "task-1",
    "title": "Implement behavior",
    "instructions": "Make the bounded change.",
    "acceptance_criteria": ["The behavior is observable."],
    "verification": ["uv run pytest tests/test_example.py"],
    "commit_message": "feat(scope): implement behavior"
  }]
}
```

A valid legacy Markdown Plan without the block still runs as exactly one
development card. `tpo plan validate` and `tpo doctor` warn because its internal
steps cannot be exposed as separate Kanban cards. On retries, TPO validates its
pinned base authority and then leaves the existing static development, review,
finish, and human-gate chain to the legacy lifecycle; manifest-only result,
dynamic review, and closeout reconciliation do not intercept that chain.

## Initialize and verify

```bash
tpo init <project> --profile native-sdd
tpo doctor <project>
```

The only skill prerequisite is Hermes `ai-coding-agents`. The selected worker
client must still be installed and callable as `claude -p` or `codex exec`, but
no gstack, superpowers, or client-side workflow skill is used.

## Compiled sequence

1. TPO records `.hermes/runs/<tick-id>/registration.json`, including the pinned
   base SHA, TODO and Plan hashes, branch, linked worktree, roles, and step keys.
2. Each Plan task becomes a worker followed by a controller gate. The worker
   reports bounded `metadata.tpo_result`; TPO opens the gate only after metadata
   and Git topology validate.
3. A fresh review session reports `clean` or structured P0-P3 findings. Findings
   create stable `review-fix`, fix-validation, and re-review cards. After five
   unsuccessful rounds the review gate stays `needs_input`; automation stops.
4. Clean review enables finish, deterministic issue closeout (the `tpo:todo`
   issue is closed via `gh` after the merge), remote-head/check verification,
   and the human merge gate.

Exactly one run is active per project. Retries reconcile the same keys. Drifted
authority, branch, worktree, PR, or remote head is preserved and blocked for
human input: TPO never resets, cleans, deletes, force-pushes, merges, or repairs
it automatically.
