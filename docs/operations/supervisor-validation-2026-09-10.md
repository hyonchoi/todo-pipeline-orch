# Supervisor implementation validation — 2026-09-10

The approved supervisor plan was implemented on `feat/agent-supervisor` in
`.worktrees/agent-supervisor`. This is standard Tier C work: persisted authority,
concurrent admission, process termination, and crash recovery require adversarial
verification. The operational recovery of issue 103 remains blocked, and the
full gate and live-environment limitations below prevent an unqualified verified
completion claim.

## Scope and review

The branch adds durable execution/admission records, an installed internal
supervisor, strict deadlines and recovery sweeping, pinned client permissions
and prompts, validated progress journals, isolated evidence collection, and
explicit recovery approval. Thin Hermes cards retain the existing worker
ceiling and retry setting. Result-contract checks remain mandatory.

Implementation subagents and separate storage, concurrency, and contract
adversarial reviewers examined the consequential scopes. Additional collector
containment and Git-inspection probes exercised real subprocesses. Bounded
coordinator fixes were necessary when the harness refused further subagent
resumption; those fixes subsequently received independent review.

One consolidated whole-change remediation wave addressed repository-controlled
Git configuration execution, Git metadata substitution, and stale unmanaged
dispatch instructions. Scoped independent re-review returned **READY** with no
remaining actionable findings. Subsequent bounded regressions also covered
metadata path parsing, collection deadline outcomes, original manifest base
pinning, and affected harness fixtures. Review approval does not establish live
provider behavior or successful issue recovery.

## Validation

The final full suite (`rtk proxy uv run --no-sync pytest -q --basetemp .hermes/t`)
finished in 301.75 seconds: **3199 passed, 10 skipped, 6 failed**. All six failures
are the baseline CLI/deprecation tests attempting log writes beneath read-only
`~/.hermes`; no new failing cases remain. The full gate is still not green.
The task-owned raw log is `.hermes/final-gates-pytest.log`.

The unchanged baseline run collected 3069 cases: 3049 passed, 10 skipped, and
10 failed. Six failures
attempted writes below the sandbox's read-only account home; four involved Git
diagnostics or fixture classification. This baseline was measured before the
integration changes, not inferred from the final failures.

The final run collected 3215 cases, a net increase of **146**. Obsolete tests of
Hermes-generated shell launch instructions were removed or migrated to pinned
registration and supervisor contracts; the count is the runner's case count,
including parametrization, rather than a count of test function definitions.

- `rtk uv run --no-sync ruff check .`: passed.
- `rtk uv run --no-sync python scripts/release_changesets.py check`: passed,
  release metadata consistent at 1.1.0.
- `rtk git diff --check`: passed.
- `rtk uv build`: passed from a clean source export; both sdist and wheel built.
  The export excluded task-owned ignored test/cache artifacts in the linked
  worktree. An earlier direct dirty-worktree build failed on scratch filenames
  and was not used for installation.
- Isolated `uv tool install` of that wheel: passed. From `/tmp`, with
  `PYTHONPATH` unset and `PATH=/usr/bin:/bin`, the installed
  `tpo-agent-supervisor --version` returned `tpo-agent-supervisor 1`, and
  `tpo --version` returned `tpo 1.1.0`. No ambient project import or model
  execution was needed.

All reported tests are provider-free. Real process and sandbox tests exercise
termination, stopped/resistant descendants, ownership loss, lock admission,
checkpoint containment, network/Unix-socket denial, and repository-config
execution attacks. They do not qualify installed model clients or providers.
Live Claude execution was skipped: the required `socat` prerequisite is absent.
No authentication or environment reconfiguration was performed.

## Documentation, recovery, and rollback

README, architecture, profile guides, scheduler guidance, and generated worker
identity instructions now describe supervised dispatch. The
[supervisor guide](../howto-agent-supervisor.md) documents process ownership,
operator approval, checkpoint constraints, supported platform/client bounds,
and rollback. A patch changeset records release intent; versions were not
manually changed.

Rollback requires pausing new dispatch, allowing owned attempts to finish or
reach their deadline, and confirming cleanup before downgrade. Preserve
execution records, journals, and worktrees. Unknown schemas or unresolved
ownership block downgrade.

Portable cleanup cannot guarantee every detached descendant stops. Unsupported
ownership, locking, Git metadata, or sandbox capabilities fail closed. Generic
bootstrap of unsupervised historical cards into supervisor recovery is not
implemented; existing registrations remain inspectable. This is an explicit
remaining plan limitation, not a claim of legacy recovery certification.

[Issue 103's report](issue-103-recovery-2026-09-10.md) records the original
registration, HEAD and diff digest, nine candidate commits, missing Plan
criteria, independent rejection, and unavailable supported board snapshots.
Its historical monitor exit remains unknown. No card transition, Plan edit,
direct database write, implementation rerun, push, merge, or PR change occurred.

The primary checkout and issue 103 worktree were preserved. The implementation
worktree is retained for review; it is not removed by closeout.

The implementation commits are `32c5602` (durable attempts and owned processes),
`3a2ec8c` (progress and recovery intent), and `a8c0644` (registered dispatch,
client containment, and evidence collection). Documentation and patch release
intent form a separate final commit. Nothing was pushed or merged.
