# External-agent supervision and recovery

New phase cards contain a registered execution identity. A thin Hermes worker
invokes the installed `tpo-agent-supervisor` launcher; deterministic Python code
starts Claude or Codex, delivers the pinned prompt through stdin, collects its
exit status, and validates its result. External agents still perform the
implementation and review. This is an internal Hermes interface, not a public
`tpo agent-run` command. A missing launcher blocks dispatch.

A supported isolated `uv tool install` pairs the launcher and helper with that
environment's interpreter and package version. New cards pin the absolute
launcher path selected beside the current interpreter. Nonstandard layouts
fall back to a launcher found on `PATH`, which cannot guarantee that pairing.
Existing card bodies are unchanged. Do not replace the launcher with an ambient
`python -m` call.
Plan/profile validation and prompt rendering precede card registration. The
execution record pins the Plan identity, prompt, client launch settings, original
worktree and branch, timeout, and result contract, including `phase_role`.
The same launcher command is used for every phase: `--execution` selects the
registered identity and its pinned prompt. The supervisor runs and collects that
execution; the TPO scheduler decides which phase comes next. The exact profile
`phase_key` is retained in card headers, execution records, results, and status.
Attempt generations identify retries separately and never rename the phase.

## Ownership and deadlines

The supervisor survives the worker that invokes it. Re-entering the worker
attaches to the same attempt and never refreshes its budget. Each explicitly
admitted attempt receives `Phase.timeout` when its client launches. Hermes keeps
its existing `timeout + 60` ceiling and one worker retry.

Before admission, `waiting_for_admission` with reason `worktree_busy` or
`launch_pending` is an ephemeral waiting response, not a stored terminal outcome.
The launcher returns exit code zero so the worker can reconnect. Plain `run`
polls for up to five seconds; new worker cards use `run --wait` and await the
command, including any background tool session. `--wait` is bounded by the phase
timeout plus 60 seconds from the call, and by the original attempt deadline plus
60 seconds when its host and boot match. It retries attachment only for verified
worktree contention. Waiting neither admits an attempt nor refreshes its budget.
If bounded waiting returns a nonterminal state, the worker reconnects without
changing card state. Newly generated instructions prohibit `kanban_block` for
`running_detached` or `waiting_for_admission`. Existing card bodies are not
rewritten; older workers may need an explicit operator refresh or recovery
through supported Kanban operations.

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

### Linux cgroup v2 ownership

New Linux launches use a delegated cgroup v2 scope for both Codex and Claude,
including checkpoint verification commands and independent reviewers. The
supervisor starts an inert helper through `systemd-run --user --scope` with
`Delegate=yes`. It verifies scope ownership and writable `cgroup.kill`, persists
the cgroup receipt and process evidence through the launch callbacks, then
releases the helper to execute the exact client or check argv with its original
stdin. The exact client environment mapping travels through a bounded private
anonymous pipe, separately from the manager environment, and is not persisted.
Spawn and scope setup overhead conservatively consume the existing deadline;
no budget is added. Launch or timeout failures before client exec may include
the bootstrap exit status; that status never establishes client success.

Cleanup signals current group members with TERM and CONT, pinning each process
with a pidfd and rechecking membership before graceful signaling. Forced cleanup
uses `cgroup.kill` and requires `cgroup.events` to report `populated=0`. Native
cgroup cleanup does not scan the host process inventory or infer descendants
from ancestry. Forked, detached, and reparented descendants remain in the scope
unless they deliberately migrate out of it.

Crash recovery uses retained receipts containing the host, boot identity, cgroup
path, device, inode, and UUID scope unit. Receipt version 2 also pins the cgroup
root device and inode (`root_device` and `root_inode`). Changed identity leaves
cleanup unconfirmed: a changed mount view cannot turn a missing scope into
confirmed cleanup. A missing version-2 scope confirms emptiness only with the
same host, boot, and root identity, under the trusted same-user contract that
clients do not deliberately migrate processes out of their owned cgroup.
Legacy version-1 receipts can still clean an existing scope with matching
identity, but a missing scope cannot newly confirm cleanup. Existing terminal
records are preserved; migration does not invent missing root evidence. Cgroups
provide process ownership, not a filesystem sandbox or a boundary against hostile
same-user code. Cleanup confirmation never reconstructs an unobserved exit or
bypasses existing result validators.

### Portable and legacy ownership

macOS and recovery of legacy Linux PID-only attempts retain the portable
backend. Its cleanup confirmation requires every previously observed or known
owned identity to be positively dead and an error-free current discovery scan.
Cleanup always permits one initial observation pass, including when
`cleanup_timeout=0`; only retries must both start and finish before their
applicable deadline. Live-owner polling uses the full cleanup allowance. Once
no known owner remains live, inventory-only retries are limited by a two-second
deadline measured from cleanup entry and by the remaining cleanup allowance.
Transient host PID churn does not permanently latch `cleanup_unconfirmed`.
Ownership ambiguity involving a known identity or observed candidate remains
`cleanup_unconfirmed`, even after a later error-free scan.

The portable backend cannot guarantee termination of every detached descendant:
a completely unobserved detached descendant can escape polling, even while the
supervisor is running. Portable cleanup confirmation does not prove that no such
descendant exists. Legacy Linux PID recovery uses `/proc` birth identities and
pidfds. macOS uses `libproc` unique IDs and audit-token signaling, including
`proc_signal_with_audittoken`, detected by installed capabilities rather than OS
version. Missing capabilities fail closed. macOS does not require systemd or
cgroups; its backend is unchanged and new macOS live testing is deferred.
Historical qualification evidence and its outstanding gates remain in the
[validation report](operations/supervisor-validation-2026-09-10.md); that report
does not qualify the new Linux cgroup backend.

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
tpo-agent-supervisor run --wait --root "$execution_root" --execution "$execution_id" \
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
a snapshot of each exact commit, runs its pinned verification commands directly,
and invokes a fresh reviewer. Only successful checks and a matching structured
review verdict produce receipts. Manifest implementation completion requires
every task to be accepted. If the implementation consumes its entire deadline,
collecting missing evidence requires a new explicitly approved recovery attempt.

Verification snapshots exclude the original worktree's partial changes. They
omit Git metadata and reject symlinks and submodules; Git-dependent or
unsupported checks fail closed. Commands execute as bounded argv, without a
shell wrapper, and inherit the environment and available worktree virtual
environment. Clients and checks run as the invoking OS user, with that user's
filesystem and network access. Snapshot construction is not storage isolation.
The supervisor still owns process cleanup, deadline enforcement, checkpoint
validation, and result promotion.

## Client and platform prerequisites

Install and authenticate the selected client before dispatch. Linux requires
cgroup v2 with `cgroup.kill`, `/proc` birth identities, pidfds, and an existing
systemd user manager that permits `systemd-run --user --scope` with
`Delegate=yes`. Preflight checks the kernel interfaces, required commands, and
reachable user manager before admission. Scope creation and writable
`cgroup.kill` are confirmed during the gated launch before releasing the helper.
A failure blocks execution; new Linux launches never fall back to portable PID
polling.
macOS requires `libproc` unique IDs and audit-token signaling. Client availability
is also checked before admission. No dependency installation, service setup, or
authentication configuration runs automatically.

Codex launches with `--dangerously-bypass-approvals-and-sandbox`; Claude launches
with `--dangerously-skip-permissions` and retains the configured tools. The
`native-sdd` implementation phase includes Claude's native `Agent` tool for
subagent delegation. The supervisor adds no client or verification sandbox;
clients, reviewers, and checks are trusted as the invoking OS user.

Pre-admission checks report bounded reasons such as `client_unavailable` or
`process_capability_unavailable` through the launcher and `status`. A refusal
consumes no attempt; repair the prerequisite and retry the registered launch.
Once admitted, worker re-entry retains the attempt's original budget; a new
recovery attempt requires the preview and approval above. Capability checks
establish availability, not successful live-provider execution.

## Storage, compatibility, and rollback

Execution records now use schema 2 with append-only `owned_cgroups` receipts.
The reader upgrades schema-1 records in memory with an empty cgroup inventory,
persisting schema 2 on the next write without inventing containment evidence for
legacy processes. Legacy PID recovery remains available. Collector launch marker
version 2 records the cgroup inventory baseline so a pending launch can be bound
to exactly one later durable receipt and its confirmed cleanup; an older receipt
cannot clear that pending launch.

Registrations and journals remain authoritative protocol records, but are not
isolated from clients or checks running as the same OS user. Checkpoint input
rejects symlinks, path escapes, unknown fields or versions, wrong attempt
identities, and oversized evidence. Do not store raw provider responses or
credentials in execution evidence.

Supervisor Git reads use a private metadata view with trusted configuration.
Repository-controlled hooks, filesystem monitors, clean filters, external diff
programs, replacement objects, and grafts cannot change the inspection behavior.
Inspection uses the reserved Git `tpo-inspection` directory and ignores
repository-specific encoding and line-ending transformations, and
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

New run registrations use schema v6, pin `agent_policy_mode` (`inherit` or
`delegated`) and the complete ordered phase definitions, and require durable
supervisor authority. Phase keys, prompts, tools, timeouts, roles, and gates
remain fixed for the run. Supported legacy v2/v3/v4/v5 registrations remain
readable with their original phase identities and are not rewritten. Before
dispatching a supervised phase for an older registration, TPO durably records
enrollment in `supervisor-required.json`. Missing
execution records cannot then make that run fall back to unsupervised acceptance.
Only older runs without enrollment or execution evidence retain legacy handling;
this does not bootstrap them into supervised recovery. The pipeline contract
schema is a separate versioned format and is unchanged.

Role metadata selects implementation, review, or delivery validation without
renaming the phase. An omitted role means `worker`; ordinary workers use generic
result validation without a single-commit restriction. Each special role may
appear at most once, and absent roles do not create implicit phases.

Implementation, review, and delivery consumers require the latest attempt to have
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
block downgrade; pre-v6 code cannot consume active v6 registrations, and a
schema-1-only execution reader cannot consume schema-2 records. Keep a compatible
recovery reader available. Do not downgrade schemas or edit records to hide
cgroup receipts. Reverting package code is not a process-cleanup operation and
must not erase recovery evidence.
