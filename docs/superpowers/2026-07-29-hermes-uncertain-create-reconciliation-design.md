# Hermes-Compatible Durable Registration Design

## Problem

Hermes task creation can time out after the remote mutation succeeds. If both
idempotent create attempts time out and the task becomes visible only after the
immediate snapshot, registration reports failure while a card may exist.
Recovery must prevent that card, or any already-known parent, from becoming
runnable before the complete registration is durable.

The earlier design tried to solve this with `--initial-status blocked`.
Hermes 0.18.2 recomputes a parentless task created that way to `ready`, so the
flag is not an executable-safety boundary. Hermes also treats an archived
parent as a satisfied dependency, which makes parent-first cleanup unsafe.

## Verified Hermes 0.18.2 Contract

- A parentless task created with `--initial-status blocked` becomes `ready`.
- A `ready` task assigned to `-` is reported by
  `hermes kanban dispatch --dry-run --json` as `skipped_nonspawnable`.
- A child of that unassigned task remains in `todo`.
- Completing the unassigned parent releases the child to `ready`.
- Archiving a parent can also release its child, so cleanup must be child-first.
- Re-archiving can print `cannot archive` while exiting zero. Current task
  status, not archive command text or exit status alone, is the idempotent
  cleanup truth.

## Registration Design

Registration creates one infrastructure task before any phase:

1. Create a registration barrier assigned to `-`, without `--goal` and without
   `--initial-status`. Its stable idempotency key is derived from the tick and
   its JSON header identifies `infrastructure: registration_barrier`.
2. Parent the first executable phase to the barrier. Parent each later
   executable phase to its executable predecessor. A parented phase begins in
   `todo`; only the first phase becomes `ready` when the barrier completes.
3. Create gate phases detached from that chain and assigned to `-`. Once a gate
   ID is known, call `hermes kanban block --kind needs_input <id>` to record
   Hermes's sticky human-input block. The brief pre-block `ready` window is safe
   because the gate is nonspawnable.
4. Persist `expected-phases.json` only after the barrier and every phase task are
   known.
5. Complete the barrier only after that sentinel is durable. Barrier completion
   is the commit point that releases phase 1.

The public return value remains the phase task IDs in profile order; the
infrastructure barrier is intentionally omitted.

## Durable Recovery State

The atomic marker at
`<project>/.hermes/outcomes/pending-task-create.json` has two forms:

- Pending create: tenant, tick ID, current phase key, and known IDs in creation
  order.
- Cleanup-only: tenant, tick ID, and every task ID in required cleanup order.

After each validated create, registration writes cleanup-only state in reverse
creation order, which is child-first and leaves the barrier last. Before the
next remote create it atomically replaces that record with pending-create
state. This keeps all known tasks durably recoverable throughout registration.

For an uncertain current create:

1. If the current child is not visible, retain pending-create state and archive
   nothing. Archiving a known parent could release the invisible child.
2. Once the child is visible, replace the marker with cleanup-only state ordered
   as current child, known phases in reverse creation order, then barrier.
3. Issue archive commands in that order and query a snapshot with `--archived`.
   A task already reported as `archived` counts as success.
4. Remove the marker only after every recorded ID is confirmed `archived`.

A local `OSError` from spawning Hermes is conclusive: no current create ran.
Registration converts the marker to cleanup-only state for the known IDs,
archives them child-first, and retains the state when confirmation is
incomplete.

At the start of every later project tick, reconciliation runs before prior-tick
processing or selection. Unresolved creation, malformed state, incomplete
cleanup, or marker-removal failure skips that project tick.

## Failure Handling

- Sentinel, sticky-block, and barrier-completion failures use the already
  durable child-first cleanup record.
- Before barrier completion, registration clears the pre-commit cleanup marker.
  If completion then fails or is uncertain, it recreates the same ordered
  cleanup state before archiving.
- Snapshot status is authoritative. Archive return code and stderr are logged
  but cannot independently prove cleanup.
- Storage failures fail closed. They never turn an uncertain registration into
  success.

## Testing

Regression coverage proves:

- Barrier command shape, first-phase parent, executable chain, and
  sentinel-before-completion ordering.
- No executable task relies on `--initial-status blocked`.
- Gate tasks are unassigned, detached, non-goal, and explicitly sticky-blocked.
- A phase-2 child can remain invisible without any parent archive, later appear,
  and then be cleaned child-first.
- Already-archived tasks satisfy cleanup.
- A local phase-2 `OSError` becomes cleanup-only state and later clears.
- Barrier completion failure retains ordered cleanup state when cleanup is
  incomplete.
- Deferred reconciliation blocks later ticks.
- A live optional Hermes CLI test proves the nonspawnable barrier contract in an
  isolated `HERMES_HOME`; it skips with an explicit reason only when Hermes is
  not installed.

## Scope

This change is limited to Hermes phase registration and tick recovery. It does
not change prompt-client selection, phase prompt rendering, profile metadata,
or the public configuration contract.
