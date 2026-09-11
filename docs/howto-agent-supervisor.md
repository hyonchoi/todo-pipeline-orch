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
`/proc` birth identities and pidfds; macOS requires `libproc` unique IDs and
audit-token signaling. Process ownership and client availability are checked
before admission. No dependency or authentication setup runs automatically.

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
block downgrade; pre-v6 code cannot consume active v6 registrations. Reverting
package code is not a process-cleanup operation and must not erase recovery evidence.
