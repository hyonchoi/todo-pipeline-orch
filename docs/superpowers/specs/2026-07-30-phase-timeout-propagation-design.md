# Phase Timeout Propagation Design

## Problem

Each phase profile declares a `timeout` in seconds, but the Kanban registration
path drops that value when it converts a `Phase` into a `PreparedPhaseTask`.
Hermes therefore creates phase tasks without `--max-runtime`, and delegated
Codex or Claude commands run as foreground terminal calls subject to Hermes's
independent 600-second foreground cap.

This violates the phase-profile contract for intentionally long-running work.
For example, the code-review phase allows 2,400 seconds, but its external Codex
process was terminated after 600 seconds.

## Decision

The phase `timeout` is the authoritative external-client deadline and the
source of the complete worker deadline:

1. The delegated external Codex or Claude process receives exactly
   `timeout` seconds.
2. The complete Hermes Kanban worker receives `timeout + 60` seconds, reserving
   a fixed cleanup grace after the client deadline.

Hermes must not replace the external-client value with its terminal tool's
foreground default. The cleanup grace is not additional implementation time.

## Data Flow

1. `load_phases()` reads `Phase.timeout` from the selected `phases.yaml`.
2. `prepare_todo_phases()` preserves it as
   `PreparedPhaseTask.timeout`.
3. `create_prepared_todo_phases()` passes executable tasks:
   `hermes kanban create --max-runtime <timeout + 60> --max-retries 1`.
4. The external-client delegation block states the same timeout and requires
   Hermes to:
   - launch the external client with tracked background execution;
   - monitor that process until it exits or the phase deadline expires;
   - terminate the process tree and confirm it is dead after deadline expiry;
   - write known command, timeout, session, and exit metadata through a Kanban
     comment before reporting failure;
   - use the external process's real exit status as the phase outcome.
5. Gate tasks remain detached and non-executable. They do not receive
   `--max-runtime`.

Using tracked background execution is required because Hermes currently clamps
foreground terminal calls to 600 seconds. Merely passing a larger `timeout`
argument to a foreground terminal call would still violate the phase contract.
The 60-second worker grace prevents Hermes from killing and requeuing the worker
before it can terminate the external process and persist its failure evidence.
`--max-retries 1` makes a runtime expiry terminal instead of allowing the
original client and a retry to mutate the same checkout concurrently.

## Completion and Failure Contract

Hermes may complete an executable phase only after the external client exits
zero within the configured deadline.

If the external client exits non-zero, cannot be launched, or exceeds the
deadline, Hermes must write a structured audit comment and use the supported
`kanban_block(kind="needs_input", reason=...)` transition with the exact reason.
It must not inspect partial changes, finish the implementation itself, commit
partial work, or report successful external delegation.

Successful task result metadata must include:

- `external_agent_command`
- `external_agent_timeout_seconds`
- the external session identifier, when available
- `external_agent_exit_code`

Timeout and failure outcomes must preserve the same known fields in the audit
comment before blocking, so operators can inspect the attempted delegation.

## Validation

Phase loading must reject invalid timeout values before tick persistence or
Kanban mutation. A valid timeout has `type(timeout) is int and timeout > 0`;
booleans, strings, zero, and negative values are invalid. Profiles that omit the
field retain the 1,800-second default.

## Compatibility

- Existing phase files already contain positive integer timeout values, so no
  profile migration is required.
- The `Phase.timeout` default remains 1,800 seconds for custom profiles that
  omit it.
- Lower-level Python APIs that construct `PreparedPhaseTask` directly must
  supply the timeout explicitly or use a compatibility default, depending on
  what the existing public test surface requires.
- This change does not alter `prompt_client`; that setting continues to select
  prompt vocabulary and the external command only.

Regression coverage will prove:

1. Phase timeout survives load, preparation, and registration.
2. Executable task creation contains the exact
   `--max-runtime <phase.timeout + 60> --max-retries 1` pairs.
3. Gate task creation contains neither runtime nor retry flags.
4. Codex and Claude delegation bodies carry the exact timeout and require
   tracked background execution, confirmed termination, an audit comment, and
   the supported `needs_input` block transition.
5. Invalid timeout values fail before task mutation, while omission preserves
   the 1,800-second default.
6. The contract explicitly forbids successful completion after launch failure,
   non-zero exit, or timeout.
7. Existing registration atomicity, dependency ordering, and prompt rendering
   tests remain green.

## Non-Goals

- Raising Hermes's global foreground timeout.
- Changing timeout values in the bundled phase profiles.
- Modifying Hermes Agent internals or publishing a Hermes release.
- Adding per-project timeout overrides outside the selected phase profile.
