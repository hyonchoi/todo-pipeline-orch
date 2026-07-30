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

The phase `timeout` is the authoritative deadline for both:

1. The complete Hermes Kanban worker execution.
2. The delegated external Codex or Claude process.

Hermes must not replace that value with its terminal tool's foreground default.

## Data Flow

1. `load_phases()` reads `Phase.timeout` from the selected `phases.yaml`.
2. `prepare_todo_phases()` preserves it as
   `PreparedPhaseTask.timeout`.
3. `create_prepared_todo_phases()` passes executable tasks:
   `hermes kanban create --max-runtime <timeout>`.
4. The external-client delegation block states the same timeout and requires
   Hermes to:
   - launch the external client with tracked background execution;
   - monitor that process until it exits or the phase deadline expires;
   - use the external process's real exit status as the phase outcome.
5. Gate tasks remain detached and non-executable. They do not receive
   `--max-runtime`.

Using tracked background execution is required because Hermes currently clamps
foreground terminal calls to 600 seconds. Merely passing a larger `timeout`
argument to a foreground terminal call would still violate the phase contract.

## Completion and Failure Contract

Hermes may complete an executable phase only after the external client exits
zero within the configured deadline.

If the external client exits non-zero, cannot be launched, or exceeds the
deadline, Hermes must block or fail the Kanban task with the exact reason. It
must not inspect partial changes, finish the implementation itself, commit
partial work, or report successful external delegation.

Successful task result metadata must include:

- `external_agent_command`
- `external_agent_timeout_seconds`
- the external session identifier, when available
- `external_agent_exit_code`

Timeout and failure outcomes should preserve the same fields when they are
known, so operators can audit the attempted delegation.

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

## Validation

Regression coverage will prove:

1. Phase timeout survives load, preparation, and registration.
2. Executable task creation contains the exact
   `--max-runtime <phase.timeout>` pair.
3. Gate task creation does not contain `--max-runtime`.
4. Codex and Claude delegation bodies carry the exact timeout and require
   tracked background execution.
5. The contract explicitly forbids successful completion after launch failure,
   non-zero exit, or timeout.
6. Existing registration atomicity, dependency ordering, and prompt rendering
   tests remain green.

## Non-Goals

- Raising Hermes's global foreground timeout.
- Changing timeout values in the bundled phase profiles.
- Modifying Hermes Agent internals or publishing a Hermes release.
- Adding per-project timeout overrides outside the selected phase profile.
