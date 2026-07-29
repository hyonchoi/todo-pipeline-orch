# Hermes Uncertain Create Reconciliation Design

## Problem

Hermes task creation can time out after the remote mutation succeeds. The current
recovery path repeats the idempotent create once and then reads one task snapshot.
If both create calls time out and the task becomes visible only after that snapshot,
the task can remain runnable even though registration reports `failed_to_spawn`.

The fix must close this late-visibility race. Merely adding more immediate retries
or bounded polling reduces its probability but does not remove it.

## Design

Executable phase tasks are created in a non-runnable state. Registration then:

1. Creates the full prepared phase chain with stable idempotency keys.
2. Persists the expected-phase sentinel after every task is known.
3. Activates the first executable task only after registration and persistence
   complete successfully.

If a create result is uncertain, registration records enough durable information
to reconcile the idempotency key on a later attempt. No subsequent tick may
advance that project while unresolved task creation remains. Reconciliation looks
up the task by tenant, tick ID, and phase key, then archives the resolved task and
all known predecessors before clearing the durable marker.

Gate phases remain blocked markers and are never activated as executable work.

## Failure Handling

- A conclusive create failure archives all known tasks and raises.
- A timed-out or malformed create result attempts immediate idempotent recovery.
- If immediate recovery is inconclusive, the unresolved marker remains durable.
- Later project processing reconciles the marker before selection or phase
  progression.
- Marker write or cleanup failures do not turn uncertain registration into
  success; the pipeline fails closed and reports the primary error.

## Testing

Add regression coverage for:

- Both idempotent create calls time out.
- The first snapshot is empty.
- The task appears in a later snapshot.
- The late task is archived before the unresolved marker is cleared.
- No executable task is activated before full registration and sentinel
  persistence.
- A project with unresolved creation state cannot start another tick.
- Gate phases remain blocked and are never activated.

Run the targeted kanban and tick tests first, then the complete locked pytest
coverage suite and Ruff.

## Scope

This change is limited to Hermes phase registration and tick recovery. It does not
change prompt-client selection, phase prompt rendering, profile metadata, or the
public configuration contract.
