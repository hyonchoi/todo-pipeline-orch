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

Before admission, `waiting_for_admission` with reason `worktree_busy` or
`launch_pending` is an ephemeral waiting response, not a stored terminal outcome.
The launcher returns exit code zero so the worker can poll the same command.
Each command polls for up to five seconds and retries attachment only for
verified worktree contention. Waiting neither admits an attempt nor refreshes an
execution deadline. Newly generated worker instructions prohibit card transitions,
including `kanban_block`, while waiting. Existing card bodies are not rewritten;
older workers may need an explicit operator refresh or recovery through supported
Kanban operations.

On timeout, the supervisor attempts graceful then forced termination within the
60-second cleanup allowance, including stopped processes where supported. A
deadline outcome remains `timed_out` even if a late exit is zero. An exit observed
before the deadline remains eligible for result validation; zero exit alone
never completes a card. Git inspection, snapshot collection, reviewer execution,
revalidation, and checkpoint promotion all share the original attempt deadline.
The terminal outcome is written once, after eligibility checks; a successful
review cannot refresh the budget or override an expired attempt.

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
Linux ownership uses `/proc` birth identities and pidfds. macOS ownership uses
`libproc` process unique IDs and audit-token signaling. Darwin support is
detected from the installed library and kernel capabilities, including
`proc_signal_with_audittoken`; an OS version string alone does not establish
support. Missing capabilities fail closed without launching a client. Native
qualification evidence and outstanding gates are tracked in the
[validation report](operations/supervisor-validation-2026-09-10.md).

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
inside the platform verification sandbox, and invokes a fresh reviewer with
read-only source access. Only successful checks and a matching structured review
verdict produce receipts. Manifest implementation completion requires every task to be accepted.
If the implementation consumes its entire deadline, collecting missing evidence
requires a new explicitly approved recovery attempt.

Verification snapshots preserve the original worktree's partial changes by
excluding them. They omit Git metadata and reject symlinks and submodules;
Git-dependent or unsupported checks fail closed. Commands are bounded argv
commands rather than shell programs. Check processes have no network or host
Unix-socket access, cannot read authoritative execution storage, and may write
only their snapshot and temporary files. Anonymous Unix stream socketpairs are
allowed for local runtime IPC (including `uv`); opening network or host Unix
socket endpoints remains denied. Linux uses `bwrap` plus a seccomp filter on
x86-64 and AArch64. macOS uses a Seatbelt policy through `sandbox-exec`, with
resolved snapshot and authority paths and private temporary runtime storage.
Unsupported platforms or unavailable sandbox capabilities fail closed. An
existing worktree `.venv` is used read-only with `uv` synchronization disabled;
recovery does not install dependencies. Ensure the approved verification
commands can run under these constraints before relying on automatic checkpoint
acceptance.

## Client and platform prerequisites

Install and authenticate the selected client before dispatch. Both clients need
working process ownership and, for manifest checkpoint collection, the platform
verification sandbox. No dependency or authentication setup runs automatically.

| Client / platform | Client launch requirements | Checkpoint verification requirements |
|---|---|---|
| Claude / Linux | Qualified Claude `2.1.267`, native Bash sandbox, `bwrap` and `socat` | `bwrap`, supported seccomp architecture, functioning sandbox probe |
| Claude / macOS | Qualified Claude `2.1.267`, native macOS Bash sandbox; no Linux `bwrap` or `socat` requirement | `sandbox-exec` Seatbelt policy and functioning sandbox probe |
| Codex / Linux | Named permission profile support | `bwrap`, supported seccomp architecture, functioning sandbox probe |
| Codex / macOS | Named permission profile support | `sandbox-exec` Seatbelt policy and functioning sandbox probe |

Codex named profiles have been exercised with `0.154.0`; this is not a live
qualification of every client/platform pair. Claude's native file tools have
separate grants from its Bash sandbox. The `native-sdd` implementation phase
enables the native `Agent` tool so its client can delegate to fresh subagents.
Default phase tools and the separate collector review tools remain unchanged.
Hooks, additional MCP tools, and user/project settings cannot broaden the
generated grants. An unqualified Claude upgrade blocks launch. Codex receives an explicit
named permission profile with authority denial and scoped worktree/Git metadata
access. The installed clients and administrator-managed policy remain trusted;
these settings do not contain a malicious client executable.

Pre-admission checks report bounded reasons through the launcher and `status`:
`client_unavailable`, `client_sandbox_unavailable`,
`client_sandbox_unconfirmed`, `process_capability_unavailable`, or
`verification_sandbox_unavailable`. A pre-admission refusal consumes no attempt;
repair the prerequisite and retry the registered launch. Once an attempt has
been admitted, worker re-entry retains its original budget; a new recovery
attempt requires the preview and approval above. A successful capability probe
establishes local availability, not complete native or live-provider qualification.

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

Execution roots for profile runs without a pinned run registration are derived
from the operating-system account home
and conventional `.config/tpo/config.yaml`, `.tpo/config.yaml`, or
`.hermes/tpo.yaml` configuration. A `state_dir` configured there is supported.
Environment-only configuration overrides cannot establish execution authority;
an unconfirmed custom root blocks registration before publishing a card. Move
the intended configuration into a trusted conventional location explicitly;
the supervisor does not silently relocate state.

New run registrations use schema v5, pin `agent_policy_mode` (`inherit` or
`delegated`), and require durable supervisor authority. Supported legacy v2/v3/v4
registrations remain readable. Before dispatching a supervised phase for an older
registration, TPO durably records enrollment in `supervisor-required.json`. Missing
execution records cannot then make that run fall back to unsupervised acceptance.
Only older runs without enrollment or execution evidence retain legacy handling;
this does not bootstrap them into supervised recovery. The pipeline contract
schema is a separate versioned format and is unchanged.

Implementation, review, and finish consumers require the latest attempt to have
a collected zero exit, no exit signal, confirmed cleanup, a matching promoted
result, and trusted journal evidence. A shared worktree lock spans prerequisite
reads and review acceptance or finish delivery, alongside the execution locks.
Finish revalidates current implementation and review authority; an earlier
accepted-review marker cannot hide a later failed retry. Verified lock contention
(`EAGAIN`/`EWOULDBLOCK`) makes controller polling wait; unsupported or unconfirmed
locking blocks acceptance. Historical commit-topology checks let review advance
HEAD while retaining accepted implementation evidence. A Hermes completion claim
cannot authorize a timed-out, interrupted, or unconfirmed supervisor result.

Existing registrations remain available for recovery validation. Issue 103's
separate historical evidence and unresolved gates
are recorded in [its recovery report](operations/issue-103-recovery-2026-09-10.md).

Before downgrading, pause the TPO tick/scan scheduler and new Hermes worker
dispatch. Let existing supervisors finish or reach their deadline, then inspect
all owned attempts and confirm cleanup. Preserve registrations, result receipts,
journals, and original worktrees. Unknown schemas or unresolved owned processes
block downgrade. Reverting package code is not a process-cleanup operation and
must not erase recovery evidence.
