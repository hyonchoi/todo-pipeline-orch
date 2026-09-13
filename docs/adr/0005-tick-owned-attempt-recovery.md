# Tick-owned attempt recovery

Status: Accepted

## Context

Supervised agent execution can fail due to timeouts or unobservable exits while
making progress toward committed checkpoints. Historical operator recovery
required manual intervention—fresh preview, approval, and re-run dispatch—before
a phase could continue.

When a supervised execution completes timed out or interrupted with confirmed
cleanup, and a manifest records accepted checkpoint progress, a recovery
opportunity exists: reissue the execution with the same generation number
available. The tick owns its card lifecycle, can inspect execution state,
access the checkpoint evidence, and has no external authorization channel.

## Decision

Automatic resume of a registered execution within the same run, under the closed
tick policy, is not drift repair; refusals remain the human boundary.

The tick's `phase_recovery.route_recovery()` automatically routes a phase whose
card is missing or not done:

- `proceed`: reissue the execution with the next generation number
- `wait`: transient contention; retry later
- `refused`: human boundary reached; write a validation-blocked marker for operator recovery
- Refusal codes include RECOVERY_REASONS plus tick-local `recovery_archive_unconfirmed` and `recovery_admission_stalled`

Approval policy (auto-approve):
- Last attempt is `timed_out` or `interrupted` with cleanup confirmed
- Generation count < MAX_GENERATIONS (3)
- Progress journal exists and is readable
- Execution HEAD descends from the journal base commit (else
  `recovery_worktree_unsafe`, `recovery_evidence_legacy`, or
  `recovery_evidence_invalid`)
- Approval is idempotent: re-approval succeeds until the worktree changes
  (recover-state-changed, trigger fresh preview)
- Per generation, at most MAX_REISSUES (3) automatic reissues before refusing

Generation and card lifecycle:
- One card per generation, created with body header `generation: N+1` and
  idempotency key `tick:phase:gN+1` (generation 1 omits the header, key is `tick:phase`)
- Generation 1 cards follow the original dispatch; generations 2+ are automatic resumptions
- Cards never carry `--recovery-event`; `attach()` consumes the tick approval automatically
- On exhaustion (MAX_GENERATIONS reached), the tick archives the phase cards and
  writes `runs/<tick>/abandoned`

Kanban and execution state:
- Kanban stays the runtime authority for live card state (ADR-0002)
- The execution record is the authority for attempt state
- Selection release happens when all cards for a phase are archived
- A worker that gives up on a live daemon (card `blocked` while the attempt is
  running) is unblocked by the tick at most once per card generation
- Hermes parks a twice-blocked card in `triage`, which the tick treats as a
  worker that gave up (resume path)
- The reissue deliberately skips `head_mismatch` because the previous generation committed

## Consequences

- Phases with manifest evidence can recover automatically without operator intervention
- Operator recovery remains available as a fallback for non-manifest profiles or
  when automatic recovery is blocked
- Tick-local refusal codes distinguish transient contention (wait and retry) from
  human boundaries (validation-blocked marker)
- Generation headers and idempotency keys link card generations within the same
  execution
- Exhaustion triggers `runs/<tick>/abandoned` to signal permanent failure to external
  systems
- Pre-change ticks do not understand generation headers or reissue logic; migration
  requires manual cleanup of rewound cards before the first auto-recovery attempt
  can proceed
