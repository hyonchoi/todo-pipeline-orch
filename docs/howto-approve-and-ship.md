# How to Approve and Ship a TODO

Ship a TODO that has passed all pipeline phases and is waiting at the Phase 9 ship gate.

## Prerequisites

- A TODO that passed phases 2 through 8 (all kanban tasks in completion statuses)
- The ship-gate sidecar at `.hermes/outcomes/<tick_id>-ship.json` (written automatically by the tick loop)
- `gh` CLI authenticated for the project repository
- Hermes CLI authenticated for kanban operations

## Steps

### 1. Confirm the TODO is ready to ship

Check that the pipeline detected ship-ready state by verifying kanban task statuses — all phases except `phase_9_ship` should be `done` or `failed`, and `phase_9_ship` should be `blocked`:

```bash
hermes kanban list <project> --query "todo_id=TODO-5"
```

Look for the TODO's kanban card with all phases except ship in completion status.

### 2. Run the approve command

```bash
uv run pipeline-watch approve myproject --todo TODO-5
```

This runs an all-deterministic guard set, then ships:

1. **Lock**: acquires a non-blocking exclusive flock on `.hermes/approve.lock`
2. **Sidecar check**: reads the ship sidecar to find the PR, branch, and SHA
3. **Guard: dirty tree**: refuses if the working tree has uncommitted changes (cannot be bypassed)
4. **Guard: SHA staleness**: refuses if the PR head SHA changed since review (bypass with `--force --force`)
5. **Bump in PR**: bumps VERSION, pyproject.toml, CHANGELOG.md on the work branch and pushes
6. **Gate: CI green**: checks GitHub status checks. Waits for nothing — if CI is red, refuses and tells you to re-run once green. The bump commit is already pushed.
7. **Squash merge**: merges the PR at the exact head SHA with `gh pr merge --squash --match-head-commit`
8. **Complete gate**: marks the kanban gate task as `done`
9. **Cleanup**: deletes the ship sidecar

On success:

```
Shipped TODO-5: merged todo-5-phase_9_ship to main (v0.4.8); gate completed.
```

### 3. Handle a SHA staleness refusal

If the PR head changed after the ship sidecar was written (e.g. someone pushed to the branch):

```
approve refused: PR head SHA changed since review (reviewed=abc123, live=def456); re-review, or pass --force --force to override
```

Two options:
- **Re-review**: manually check the new diff, then approve again. The bump commit is not pushed on refusal, so the sidecar SHA is still the reviewed value.
- **Force bypass**: `uv run pipeline-watch approve myproject --todo TODO-5 --force --force`. This is audited to `.hermes/approve_audit.log` with both SHAs.

### 4. Handle a CI-red refusal

If bump-in-PR succeeded but CI checks are not green:

```
approve refused: CI is not green yet; re-run approve once checks pass (the bump commit is already pushed)
```

The bump commit is already on the branch. Once CI turns green:

```bash
uv run pipeline-watch approve myproject --todo TODO-5
```

The SHA staleness guard is skipped because the sidecar was re-baselined with the bump SHA.

## Verification

After a successful ship:
- The PR state is `MERGED` on GitHub
- The kanban `phase_9_ship` task is `done`
- The ship sidecar is deleted from `.hermes/outcomes/`
- `git log main` shows the squash merge commit

## Troubleshooting

**"no pending ship for TODO-N (not ready, or already shipped)"**
- The tick loop has not detected ship-ready state yet, or the TODO was already shipped.
- Check kanban task statuses. If `phase_9_ship` is not blocked, the TODO is not in ship-ready state.

**"another approve is already in progress"**
- A concurrent `approve` command holds `.hermes/approve.lock`. Wait for it to finish or kill it.

**"working tree is dirty"**
- Commit or stash changes in the project directory before approving. This guard cannot be bypassed.

## Idempotency

If the process crashes after the merge but before completing the gate, re-running `approve` sees the PR is already `MERGED`, completes the gate, deletes the sidecar, and returns success.

## See Also

- [CLI reference](reference-cli.md) — `approve` subcommand
- [Circuit breaker explanation](explanation-circuit-breaker.md) — Why stalled ticks alert
- [Pipeline state machine](hermes-state-machine.md) — Ship gate state transitions
