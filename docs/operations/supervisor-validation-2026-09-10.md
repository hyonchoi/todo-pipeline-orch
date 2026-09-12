# Supervisor validation — 2026-09-10

The user revised the scope: remove the supervisor sandbox and establish live
execution before adding further isolation. This report supersedes the earlier
sandbox qualification on this branch; those historical results remain in Git.

## Implemented behavior

Claude and Codex launch directly with their noninteractive permission-bypass
flags. Checkpoint commands execute their pinned argv in an exact-commit snapshot,
using the existing worktree virtual environment and snapshot source paths.
There is no supervisor-added bwrap, seccomp, Seatbelt, client-version gate, or
permission-profile generation. Clients and checks are trusted as the invoking
OS user; journals are not isolated from that user.

Process ownership, original attempt deadlines, cleanup, atomic execution
records, checkpoint validation, and downstream completion authority remain.
New worker cards use `run --wait` and await terminal command completion instead
of interpreting repeated short status responses. Reconnection does not admit a
new generation or refresh its deadline.

The independent reviewer receives an explicit response path, observed original
worktree HEAD/cleanliness, and completed verification results. Its output still
must match the task, commit, Plan identity, diff digest, and accepted verdict.

## Review and deterministic checks

This is a Tier C change. Three independent discovery lenses checked waiting and
process lifetime, command/result contracts, and snapshot/environment behavior.
A reproduced source-layout issue was corrected: snapshot `src/` imports and the
worktree virtual environment now take precedence over editable live source and
ambient executables. The reviewer-handoff changes also received scoped review.
Final independent code review returned **READY**, with live completion tracked
separately below.

- Client/checkpoint tests: **42 passed**, including real argv execution,
  editable source-layout handling, partial-work preservation, and reviewer
  response delivery. New regressions failed before their fixes.
- Supervisor tests: **50 passed**, including original-deadline waiting,
  reconnection, previous-boot clocks, recovery generations, and the complete
  `metadata={"tpo_result": ...}` envelope required by the installed Kanban tool.
- Independent reviewer checks: **115 passed** before the environment fix,
  **40 passed** afterward, and **7 passed** for the final handoff scope.
- Earlier full suite after sandbox removal: **3275 passed, 17 skipped** in
  480.43 seconds. It preceded the environment and handoff follow-ups.
- Full suite after the environment and handoff fixes: **3279 passed, 17 skipped**
  in 491.78 seconds. The later worker metadata instruction and its new regression
  passed the 50-test supervisor suite. CI at `eb2d998` passed **3280 tests,
  17 skipped** on each of Python 3.12, 3.13, and 3.14.
  [CI run](https://github.com/hyonchoi/todo-pipeline-orch/actions/runs/34548087643).
  Final collection is 3297 cases versus 3069 at the branch baseline: **net +228**;
  the sandbox removal reduced the preceding branch suite by 38 cases.
- Ruff, release metadata at 1.1.0, and diff whitespace checks: passed.
- Updated documentation checks: **17 passed** with
  `uv run pytest tests/test_harness_docs.py tests/test_docs_links.py -q`.
  An initial invocation named a nonexistent native-doc test file and exited
  during collection; the corrected command above passed.
- Clean-export sdist/wheel build and installation of the project command:
  passed. The installed collector matches the source hash; the installed CLI
  exposes `--wait` and direct client arguments without an ambient project import.
  A separate `uv tool install` into disposable tool/bin directories also passed;
  both installed CLIs ran from `/tmp` with `PYTHONPATH` unset.
- Installed-Hermes registration dry run: **1 passed** in 1.80 seconds with
  `TPO_RUN_LIVE_HERMES_CONTRACT=1 uv run pytest tests/test_hermes_registration_contract.py -q`.
  This uses temporary state and no model execution.
- Native macOS CI at `eb2d998`: **50 passed** on each of ARM macOS 15
  (43.34 seconds) and Intel macOS 15 (98.05 seconds), covering native process
  ownership and direct checkpoint verification.
  [CI run](https://github.com/hyonchoi/todo-pipeline-orch/actions/runs/34548087658).

## Live execution

Repository: `hyonchoi/tpo-mock-project`. Commands use the globally installed
project build with separate client configuration and an empty Slack channel:

```sh
tpo test --repo hyonchoi/tpo-mock-project --profile native-sdd --timeout 1800 --keep
```

Earlier reinstalled runs #39 (Claude) and #40 (Codex) proved that the old
installation/schema mismatch was resolved. Both then failed sandboxed
`uv run pytest` with EPERM. After sandbox removal, Claude #41 passed verification
and exited zero with confirmed cleanup, but its review response was not accepted.
Its exact cause was not established; the subsequent handoff fix makes the output
path and already-collected facts explicit.

Claude #42 completed its supervised implementation, verification, and checkpoint
review, but the harness stalled because the worker flattened the result metadata.
The worker now passes the complete returned metadata envelope to Kanban.

Claude completed the full live harness: **3/3 phases passed, exit 0**, including
implementation, independent review, and PR creation. Run `re7hhk2x`, issue #43,
produced [PR #44](https://github.com/hyonchoi/tpo-mock-project/pull/44). The harness
validated its PR invariant and confirmed all tasks terminal during shutdown.
Each supervised phase exited zero with confirmed cleanup. Evidence is retained
at `~/.hermes/tmp/harness-gsm06azk/artifacts/reports/report.json`; the disposable
issue was then closed so the next client could run. Its PR and worktree remain.

Codex #45 passed implementation and independent review but failed delivery:
plain HTTPS `git push` could not obtain a username. The existing `gh` login
worked with a per-command Git credential helper, confirmed by a successful
`git -c credential.https://github.com.helper='!gh auth git-credential' push --dry-run`.
The failed run is preserved with confirmed cleanup. Hermes workers did not inherit
the harness process helper environment. The fresh Codex disposable clone therefore
sets `credential.https://github.com.helper` locally to `!gh auth git-credential`;
ordinary `git push --dry-run` then passed. No credentials are stored, and no login
or global Git configuration is changed. Later results from that run are maintained
in [PR #111’s Validation section](https://github.com/hyonchoi/todo-pipeline-orch/pull/111),
which is updated as live checks finish without rewriting qualification records.

Live macOS client execution is unavailable from this Linux session and remains
unqualified.

## Preserved state and rollback

Historical failed-run workspaces and commits are preserved. Disposable issues
are closed only after card quiescence and supervisor cleanup are confirmed.
The primary checkout and issue 103 worktree are unchanged. Issue 103 recovery
and generic bootstrap of unsupervised historical cards remain incomplete; see
[the issue 103 report](issue-103-recovery-2026-09-10.md).

Rollback pauses admission, confirms cleanup of owned processes, and preserves
execution records, journals, and worktrees. Unknown schemas or unresolved
ownership block downgrade. Portable process handling cannot guarantee observing
every detached descendant. No merge or issue 103 state transition is included.
