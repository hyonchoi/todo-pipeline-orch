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
admitted attempt receives `Phase.timeout` when its client launches. Hermes Kanban
worker receives `card_max_runtime(registration)` as its ceiling: timeout + 60s
for phases without a manifest, or timeout + 120s + deadline-collection-budget
for manifest phases (budget = min 600s, 10% of timeout). One worker retry applies.

Before admission, `waiting_for_admission` with reason `worktree_busy` or
`execution_busy` is an ephemeral waiting response, not a stored terminal outcome.
The launcher returns exit code zero so the worker can reconnect. Plain `run`
polls for up to five seconds; new worker cards use `run --wait` and await the
command, including any background tool session.

`run --wait` produces multi-line JSON output: one status line every 60 seconds
with `"final": false` (fields: status, generation, elapsed_s, remaining_s,
accepted_tasks), then a final report with `"final": true`. Consumers act only on
the final line. New terminal wait statuses: `recovery_invalidated` (approved
recovery intent no longer matches the worktree; only the tick may approve again)
and `admission_failed` (daemon spawned for an approved retry never admitted within
90 seconds). `waiting_for_admission` with reason `worktree_busy` or `execution_busy`
is retried by `run --wait`.

`--wait` is bounded by the phase timeout plus 60s for non-manifest phases, or
plus 120s + deadline-collection-budget for manifest phases (budget = min 600s, 10%
of timeout), from the call; it is also bounded by the original attempt deadline
plus the same tail when its host and boot match. It retries attachment only for
verified contention. Waiting neither admits an attempt nor refreshes its budget.
If bounded waiting returns a nonterminal state, the worker reconnects without
changing card state. Newly generated instructions prohibit `kanban_block` for
`running_detached` or `waiting_for_admission`. Existing card bodies are not
rewritten; older workers may need an explicit operator refresh or recovery
through supported Kanban operations.

On timeout, the supervisor attempts graceful then forced termination within the
60-second cleanup allowance, including stopped processes where supported. A
deadline outcome remains `timed_out` even if a late exit is zero. An exit observed
before the deadline remains eligible for result validation; zero exit alone
never completes a card.

Deadline-time checkpoint collection: when a timed-out attempt has confirmed
cleanup and a manifest, the supervisor collects already-committed checkpoint
tasks under a deadline budget of min(600s, 10% of timeout). Status reports:
`deadline_collection_pending` (transient) then `deadline_collection_complete`,
`deadline_collection_partial`, or `deadline_collection_incomplete` (terminal).
A timeout never becomes success; `recover()` finishes an interrupted collection
as `timed_out/deadline_collection_incomplete`.

Git inspection, snapshot collection, reviewer execution, revalidation, and
checkpoint promotion all share the original attempt deadline. The terminal
outcome is written once, after eligibility checks; a successful review cannot
refresh the budget or override an expired attempt.

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

### Status fields and observability

`tpo-agent-supervisor status` and `run --wait` reports include:
- `recovery`: object with `state`, `approver`, `generation`, and `reissues` fields
  (no event id), or `null` if no recovery intent
- `supervisor_alive`: boolean indicating if a supervisor still holds the
  execution lock
- `remaining_s`: seconds until deadline (null if not live or deadline unknown)
- `accepted_tasks`: count of accepted checkpoint tasks from manifest (null if no
  manifest)
- `reason`: for terminal attempts, a short code distinguishing outcome types
  (e.g., `deadline_collection_partial`)

Per-execution logs:
- `<execution-root>/<exec-dir>/supervisor.log`: identifiers, generations,
  statuses, and reason codes only
- `<execution-root>/<exec-dir>/client.stdout.log`: prompt client stdout
- `<execution-root>/<exec-dir>/client.stderr.log`: prompt client stderr

### Direct process ownership on Linux and macOS

Both platforms use the same process lifecycle: launch the configured Claude or
Codex command, persist its PID and birth identity, collect its exit, and confirm
that this directly launched process has terminated. Each supervisor-launched
checkpoint check and independent reviewer has its own direct process receipt.
The supervisor does not discover, track, signal, or wait for their descendants.
It assumes that client termination ends that client's code changes and
operations; subprocess cleanup is the client's responsibility. A descendant
remaining alive does not block completion or a later attempt under this contract.

Linux uses `/proc` birth identities and pidfds. macOS uses `libproc` unique IDs
and audit-token signaling, including `proc_signal_with_audittoken`. These native
identity mechanisms prevent signaling an unrelated process after PID reuse;
the cleanup policy is identical on both platforms. No systemd manager, cgroup
scope, process-group signaling, or host process inventory scan is used.

Cleanup sends TERM and CONT to the verified direct process, then KILL if needed,
within the original cleanup allowance. Confirmed termination of the direct
process is sufficient for cleanup; unavailable identity or a still-live process
leaves cleanup unconfirmed. A zero cleanup allowance permits an initial check
but does not add a waiting budget. Cleanup confirmation never reconstructs an
unobserved exit or bypasses deadline, checkpoint, or result validation.

Historical qualification evidence and its outstanding gates remain in the
[validation report](operations/supervisor-validation-2026-09-10.md); those older
results do not establish qualification of this direct-process contract.

## Recovery

### Automatic tick resume

When a supervised execution completes `timed_out` or `interrupted` with confirmed
cleanup and a manifest records accepted checkpoint progress, the tick automatically
evaluates resume: checking execution generation count (max 3 generations), progress
journal readability, and HEAD descent from the journal base. On approval, the tick
archives quiescent phase cards and creates one card for the next generation with
body header `generation: N+1` and idempotency key `tick:phase:gN+1` (generation 1
omits the header, key is `tick:phase`). Cards never carry `--recovery-event`;
the supervisor consumes the tick approval automatically.

Non-manifest profiles and refusals remain the human boundary: `tpo-agent-supervisor
prepare-recovery` / `approve-recovery` provide the operator fallback below.
Refusal codes distinguish transient contention (tick retries) from permanent
blocks: `recovery_generation_exhausted` (max generations reached; tick archives
the cards and writes `runs/<tick>/abandoned`), and validation-blocked markers
for human operator recovery.

### Operator fallback

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
`/proc` birth identities and pidfds. macOS requires `libproc` unique IDs and
audit-token signaling. Both platforms check client and process identity
capabilities before admission. No cgroup v2 or systemd delegation is required.
No dependency installation, service setup, or authentication configuration runs
automatically.

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

Execution records use schema 3 with explicit `direct_processes` receipts.
Schema-1 and schema-2 records remain readable. Their historical descendant and
cgroup inventories are retained as inert metadata, never as authority to signal
processes or invoke cgroup operations. The recorded `client_process` remains a
direct process identity; migration does not convert historical descendants into
direct roots. Collector launch marker version 3 binds a pending launch to its
direct receipt inventory baseline. Recovery can resolve it only through the
matching durable direct receipt and confirmed cleanup. An unknown launch remains
unconfirmed. For a legacy collector marker belonging to an unconfirmed attempt,
even `pending=false` only proves that launch registration finished; it does not
prove collector termination. Recovery preserves that uncertainty without
signaling historical descendants. Already-confirmed terminal records are preserved.

Recovery intent (`recovery-intent.json`) gains fields `approver` (operator or tick)
and `status` (`prepared`, `approved`, `consumed`, `invalidated`), and `reissues`
count. Legacy 3-key operator intents (`version`, `status`, `preview`) still read;
operator intents remain 3-key on disk. A pre-change `recover()` maps an interrupted
deadline collection to `interrupted/exit_unobservable`. Pre-change ticks do not
understand generation headers (they see only generation-1 keys); state the
downgrade consequence honestly.

Migration note for a run stuck before this change: the first tick after deployment
sees a record with a missing or not-done card, triggering `auto_approve_resume`.
If approved, it creates a generation-2 card with key `tick:phase:g2`. Manual
cleanup (if needed): archive the parent card and any Hermes-created children
(child-first), then run `tpo install-profile --force` to ensure
`kanban.auto_decompose: false` in the Hermes profile.

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
pre-schema-3 execution reader cannot consume schema-3 records. Keep a compatible
recovery reader available. Before upgrading, let old supervisors finish so their
original cleanup policy can run. Do not downgrade schemas or erase legacy
receipts to force acceptance. Reverting package code is not a process-cleanup
operation and must not erase recovery evidence.
