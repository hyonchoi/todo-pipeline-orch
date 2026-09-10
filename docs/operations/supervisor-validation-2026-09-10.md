# Supervisor implementation validation — 2026-09-10

The approved supervisor plan was implemented on `feat/agent-supervisor` in
`.worktrees/agent-supervisor`. This is standard Tier C work: persisted authority,
concurrent admission, process termination, and crash recovery require adversarial
verification. Runtime head `2303485` passed independent whole-change review,
full CI, native platform checks, and isolated packaging validation. Live client
completion remains unqualified, and the operational recovery of issue 103
remains blocked; those limits are separate from the passing provider-free gates.

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
dispatch instructions. Scoped independent re-review of that earlier wave returned
**READY** with no remaining actionable findings at that point. Subsequent bounded
regressions also covered metadata path parsing, collection deadline outcomes,
original manifest base pinning, and affected harness fixtures. Review approval does not establish live
provider behavior or successful issue recovery.

The final independent whole-change review is **READY**, including the final
admission-waiting fix and **83 passing tests**. The P1 deadline finding was
resolved by `e027e2f`: Git collection, review, revalidation, and checkpoint
promotion share the original deadline, and the immutable terminal outcome is
written after eligibility checks. That scoped review also returned **READY**,
with **123 passing tests**. Subsequent fixes addressed downstream acceptance of
Hermes completion claims, cross-phase locking, stale delivery authority after a
failed retry, and publication races. The final review covers the shared
worktree guard, finish prerequisite revalidation, and admission-waiting behavior
in runtime head `2303485`.

## Validation

### Earlier full gates and packaging

An early full run (`rtk proxy uv run --no-sync pytest -q --basetemp .hermes/t`)
reported **3199 passed, 10 skipped, 6 failed** in 301.75 seconds. The six failures
were CLI/deprecation fixtures writing below read-only `~/.hermes`. Separately,
`9d5e4eb` corrected the original CI fixture assumptions about missing directories
and mocked Codex availability. A subsequent local full suite passed with
**3205 passed, 10 skipped**. These results precede the later IPC,
Darwin, client-tool, and preflight changes and do not certify the current head.

The unchanged baseline had collected 3069 cases: 3049 passed, 10 skipped, and
10 failed (six account-home writes and four Git diagnostics/fixture cases).
Obsolete unmanaged-shell tests were migrated to registered supervisor contracts;
case counts include parametrization. Final CI collected 3335 cases, a net
increase of **266** over the 3069-case baseline.

The earlier implementation gates also passed Ruff, release metadata validation
(at version 1.1.0), and `git diff --check`. A clean source export built both sdist
and wheel with `rtk uv build`; a direct dirty-worktree build had failed on ignored
scratch filenames. Isolated wheel installation succeeded: from `/tmp`, with
`PYTHONPATH` unset and `PATH=/usr/bin:/bin`, the installed launcher returned
`tpo-agent-supervisor 1` and `tpo --version` returned `tpo 1.1.0`. A more recent
clean-export build and isolated wheel installation also passed, but preceded
the final review fixes. Final packaging evidence is recorded below.

### Native macOS evidence

The first native process run exposed an exit-transition signaling race. Fix
`59499cd` added bounded retries against verified process identity; `49eee5f`
corrected native socket and variadic ABI test probes.
[Native CI run 34537125223](https://github.com/hyonchoi/todo-pipeline-orch/actions/runs/34537125223)
at `49eee5f` then passed **44 tests with zero skips on each runner**: macOS ARM
in 8.47 seconds and Intel in 10.86 seconds. The retained local log is
`.hermes/darwin-ci-green.log`.

After cleanup instrumentation was removed in `7579cce`,
[native CI run 34538092799](https://github.com/hyonchoi/todo-pipeline-orch/actions/runs/34538092799)
at `49c1947` passed on both Mac runners.

After deadline fix `e027e2f`,
[native CI run 34538978497](https://github.com/hyonchoi/todo-pipeline-orch/actions/runs/34538978497)
also passed on both Mac runners. It precedes the downstream authority changes.

Final [native CI run 34540494058](https://github.com/hyonchoi/todo-pipeline-orch/actions/runs/34540494058)
at `2303485` passed on macOS 15 ARM and Intel, with **44 provider-free tests on
each runner**.

These provider-free checks exercise native process ownership and cleanup,
Seatbelt file/authority containment, network and host Unix-socket denial,
Mach/signaling restrictions, and offline `uv`/pytest execution. The sandbox
`uv`/pytest, file-authority, and network checks had also passed on both Mac
architectures before the fixture correction. This evidence qualifies those
native test scopes at the recorded commit; it does not establish live Claude
or Codex execution on macOS or certify subsequent integration changes.

### Live client findings

Live Claude run #35 exited zero but failed result validation. Missing native
`Agent` tool permission is a likely contributor to the incomplete review/result
contract, not a proven explanation of all agent behavior. Live Codex run #36
produced commit `ca832b28` but checkpoint collection was blocked when Tokio
needed a stream socketpair. Neither run established successful completion.

`4079c09` permits anonymous Unix stream IPC inside verification while retaining
endpoint denial. `70559d3` enables Claude's native `Agent` tool for `native-sdd`
implementation subagents and reports pre-admission launch refusals.

The latest Claude run, #37 in workspace `harness-frfvhgy4`, returned
`lock_unconfirmed` before any attempt was admitted (generation zero). The cause
has not been established, and this run does not establish execution of the latest
code. Its cards are archived and the run is quiescent, with no active worker; issue #37
was closed after that confirmation. The workspace is preserved. Codex retry #38 in workspace `harness-xx542g19` also returned
`lock_unconfirmed` at generation zero. Its cards are archived, cancellation and
quiescence are confirmed, and its issue was closed; its workspace is preserved.
Both retries used the fresh isolated installation first on the harness PATH,
but the executable selected inside the Hermes worker has not been established.
Historical Codex run #36's commit `ca832b28` and untracked `uv.lock` remain
preserved; its issue was closed after quiescence. Neither client has a
successful final live completion result.
Provider-free checks cannot substitute for that evidence, and macOS live
Claude/Codex execution remains unqualified.
No raw model/provider payloads are included in this report.

### Current integration gates

Preflight integration `c15d6d8` passed **48 focused tests**. It checks required
process/client/collector capabilities before admission and reports bounded
refusal reasons without consuming an attempt.

The run in `.hermes/findings-full-pytest.log` completed with **28 failed,
3238 passed, 26 skipped**. Of those failures, 26 were `Agent` capability-contract
regressions fixed in `49c1947`. Focused validation recorded 332 passing tests in
independent review and implementer runs of 197 and 221 passing tests. The other
two failures involved Git diagnostic assertions under a long pytest base path;
the affected scope passed 91 tests with a short base path. These focused results
do not turn the failed full run into a passing one.

[CI run 34538092850](https://github.com/hyonchoi/todo-pipeline-orch/actions/runs/34538092850)
at `49c1947` passed all five jobs, and the native run linked above passed both
Mac jobs. The newer local full run (`rtk proxy uv run --no-sync pytest -q
--basetemp .hermes/t`) completed with **3269 passed, 26 skipped** in 327.70
seconds; its log is `.hermes/ci-fix-full.log`. That run started at `49c1947`,
before the deadline edits. These earlier local and CI results do not certify
later integration changes; final runtime evidence follows.

A later authority integration run reported **19 failed, 3274 passed, 26 skipped**.
All 19 failures were in two fixture files, which have been corrected. Focused
reruns passed **18 embedded harness tests in 50.20 seconds** and **1 stress test
in 33.42 seconds**. Those fixtures exercise real admission, checkpoint handling,
and result promotion with explicit provider-free review stubs; they do not
establish live reviewer or provider behavior. The full snapshot run recorded in
`.hermes/authority-final-full.log` passed **3303 tests, with 26 skipped**, in
488.14 seconds. It predates the final admission-waiting edits and does not
certify those edits.

Final [CI run 34540494094](https://github.com/hyonchoi/todo-pipeline-orch/actions/runs/34540494094)
at `2303485` passed all five jobs. Each supported Python suite reported
**3305 passed, 30 skipped**: Python 3.12 in 259.98 seconds, 3.13 in 231.75
seconds, and 3.14 in 199.86 seconds. The final native run above also passed
both Mac jobs. These results cover the final runtime changes.

A final clean export of `2303485` built both sdist and wheel with `uv build`.
Isolated `uv tool install` into `.hermes/release-tool` passed. From `/tmp`, with
`PYTHONPATH` unset and `PATH=/usr/bin:/bin`, the installed
`tpo-agent-supervisor --version`, `tpo --version`, and `python -I` import of the
new authority module all passed. This supersedes the earlier successful build
of the authority snapshot before admission-waiting changes.

The opt-in installed-Hermes registration contract check, run with
`TPO_RUN_LIVE_HERMES_CONTRACT=1`, passed **1 test in 2.41 seconds** on the
final rerun. It exercises
installed registration with temporary state and dry-run dispatch, without model
or provider execution. It does not qualify live Claude/Codex completion.

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
Its historical monitor exit remains unknown. No issue 103 card transition, Plan
edit, direct database write, or implementation rerun was performed as part of
this documentation update. Its separate report is preserved.

The primary checkout and issue 103 worktree were preserved. The implementation
worktree is retained for review; it is not removed by closeout.

The implementation commits are `32c5602` (durable attempts and owned processes),
`3a2ec8c` (progress and recovery intent), and `a8c0644` (registered dispatch,
client containment, and evidence collection), followed by the fixes and platform
work identified above. The branch has been pushed for
[PR #111](https://github.com/hyonchoi/todo-pipeline-orch/pull/111); it has not been
merged. Runtime head `2303485`, including the final review fixes, has been
pushed. This report records that runtime's completed validation and the remaining
live and legacy-recovery limitations. The existing patch release intent covers
this work; no manual version bump is required.
