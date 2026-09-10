# External-agent supervision and recovery

New phase cards contain a registered execution identity. A thin Hermes worker
invokes the installed `tpo-agent-supervisor` launcher; deterministic Python code
starts Claude or Codex, delivers the pinned prompt through stdin, collects its
exit status, and validates its result. External agents still perform the
implementation and review. This is an internal Hermes interface, not a public
`tpo agent-run` command. A missing launcher blocks dispatch.

The launcher is installed with the package by `uv tool install` and uses that
environment's interpreter. Do not replace it with an ambient `python -m` call.
Plan/profile validation and prompt rendering precede card registration. The
execution record pins the Plan identity, prompt, client permissions, original
worktree and branch, timeout, and result contract.

## Ownership and deadlines

The supervisor survives the worker that invokes it. Re-entering the worker
attaches to the same attempt and never refreshes its budget. Each explicitly
admitted attempt receives `Phase.timeout` when its client launches. Hermes keeps
its existing `timeout + 60` ceiling and one worker retry.

On timeout, the supervisor attempts graceful then forced termination within the
60-second cleanup allowance, including stopped processes where supported. A
deadline outcome remains `timed_out` even if a late exit is zero. An exit observed
before the deadline remains eligible for result validation; zero exit alone
never completes a card.

Versioned records are atomically persisted outside the execution worktree.
Kernel-held advisory locks serialize admission. Host, boot, and process birth
identities prevent recovery from signaling a reused PID. An owner file alone
does not prove that a lock remains held. Supervisor loss before durable exit
collection means `interrupted/exit_unobservable`; disappearance cannot establish
success. A prior boot's monotonic timestamps are not compared with this boot.

TPO polling sweeps abandoned attempts and attempts cleanup of verifiable owned
processes. `running_detached`, `cleanup_unconfirmed`, and `lock_unconfirmed`
describe execution/admission state, not permission to complete a Kanban card.
Unknown ownership or cleanup blocks another attempt. Worker transitions use the
supported Kanban worker tools and their current run identity, preserving newer
attempts and unrelated or manual blocks.

Portable process management cannot guarantee termination of every detached
descendant. In particular, an escape can become unobservable while both the
supervisor and recovery polling are unavailable. No systemd or cgroup service is
required. Unsupported locking or ownership checks fail closed.
The current verified process-identity implementation uses Linux `/proc` and
pidfds; other platforms do not receive an unverified execution fallback.

## Operator recovery

Refresh the card and its runs through supported Kanban operations first. Verify
the registered execution identity and phase, current branch/HEAD, cleanup state,
and the original Plan. Keep unknown historical exit outcomes unknown.

Use the exact root and identity from the registered card. The internal interface
provides a concrete preview and separate approval:

```bash
tpo-agent-supervisor status --root "$execution_root" --execution "$execution_id"
tpo-agent-supervisor prepare-recovery --root "$execution_root" --execution "$execution_id" \
  --mode recovery_only > recovery-preview.json
# Inspect the complete preview before approving it.
tpo-agent-supervisor approve-recovery --root "$execution_root" --execution "$execution_id" \
  --preview-file recovery-preview.json
tpo-agent-supervisor run --root "$execution_root" --execution "$execution_id" \
  --recovery-event "$approved_event"
```

The returned approval event is single-use and binds the attempt generation,
Plan, HEAD, and staged, unstaged, and untracked work. Changed evidence requires a
fresh preview and approval. `recovery_only` requests verification and result
collection without repeating implementation. Use `--mode resume` only for a
modern execution with reliable progress evidence. Worker re-entry cannot approve
recovery, and an ordinary automatic retry is not an operator recovery event.

These commands require an existing supervisor execution record. Older
unsupervised Kanban runs are not automatically imported: this release does not
provide a generic bootstrap into supervised recovery when historical process
ownership and cleanup cannot be established. Existing run registration and
result reconciliation remain available for evidence review. Leave an uncertain
legacy run blocked rather than inventing a collected exit or confirmed cleanup.

Resume retains the original worktree, branch, and approved Plan. Inspect and
preserve partial changes before continuing the unfinished task. Never reset,
clean, or commit incomplete work to make recovery pass. Plan drift, rewritten or
missing modern checkpoint history, unexpected commits, Git index/HEAD locks,
and merge/rebase state block continuation.

Manifest progress records track task order, accepted commit ancestry, verification
evidence, and independent review receipts. Agents submit bounded checkpoint
requests only in their per-attempt staging directory; agent claims or commit
messages cannot authorize skipping a task. Only validated supervisor promotion
changes authoritative progress. A commit made before its checkpoint needs fresh
verification and review. Non-manifest profiles have attempt recovery without a
subtask checkpoint guarantee. Legacy evidence permits recovery-only validation.

After the implementation client exits, the supervisor uses the attempt's
remaining original deadline to validate candidate commits in order. It builds
an isolated snapshot of each exact commit, runs its pinned verification commands
inside a Linux `bwrap` sandbox, and invokes a fresh reviewer with read-only source
access. Only successful checks and a matching structured review verdict produce
receipts. Manifest implementation completion requires every task to be accepted.
If the implementation consumes its entire deadline, collecting missing evidence
requires a new explicitly approved recovery attempt.

Verification snapshots preserve the original worktree's partial changes by
excluding them. They omit Git metadata and reject symlinks and submodules;
Git-dependent or unsupported checks fail closed. Commands are bounded argv
commands rather than shell programs. Check processes have no network or host
Unix-socket access, cannot read authoritative execution storage, and may write
only their snapshot and temporary files. The syscall filter currently supports
Linux x86-64 and AArch64; other architectures fail closed. An existing worktree `.venv` is
used read-only with `uv` synchronization disabled; recovery does not install
dependencies. Ensure the approved verification commands can run under these
constraints before relying on automatic checkpoint acceptance.

## Storage, compatibility, and rollback

Clients receive narrowly scoped worktree, Git metadata, and submission access;
the authoritative registration/journal directory is protected. Checkpoint input
rejects symlinks, path escapes, unknown fields or versions, wrong attempt
identities, and oversized evidence. Do not store raw provider responses or
credentials in execution evidence.

Supervisor Git reads use a private metadata view with trusted configuration.
Repository-controlled hooks, filesystem monitors, clean filters, external diff
programs, replacement objects, and grafts cannot change the inspection behavior.
The reserved Git `tpo-inspection` directory is denied to clients. Inspection
ignores repository-specific encoding and line-ending transformations, and
submodule cleanliness or unsupported Git metadata formats fail closed; these
repositories need separate supported validation before automatic continuation.

Claude uses its native Linux Bash sandbox plus separately scoped native file
tools. It requires `bwrap`, `socat`, and the explicitly qualified Claude version
(`2.1.267` in this release); missing prerequisites or an unqualified upgrade block
launch. Hooks, additional MCP tools, and user/project settings cannot broaden
the generated grants. The installed client and administrator-managed policy
remain trusted; this does not contain a malicious client executable. Codex uses
an explicit named permission profile with authority denial. No client dependency
or authentication setup is performed automatically.

Non-manifest execution roots are derived from the operating-system account home
and conventional `.config/tpo/config.yaml`, `.tpo/config.yaml`, or
`.hermes/tpo.yaml` configuration. A `state_dir` configured there is supported.
Environment-only configuration overrides cannot establish execution authority;
an unconfirmed custom root blocks registration before publishing a card. Move
the intended configuration into a trusted conventional location explicitly;
the supervisor does not silently relocate state.

Existing registrations remain available for recovery validation; new dispatches
use supervision. Issue 103's separate historical evidence and unresolved gates
are recorded in [its recovery report](operations/issue-103-recovery-2026-09-10.md).

Before downgrading, pause the TPO tick/scan scheduler and new Hermes worker
dispatch. Let existing supervisors finish or reach their deadline, then inspect
all owned attempts and confirm cleanup. Preserve registrations, result receipts,
journals, and original worktrees. Unknown schemas or unresolved owned processes
block downgrade. Reverting package code is not a process-cleanup operation and
must not erase recovery evidence.
