# How to kill a stuck in-flight phase

When a phase is wedged — Claude subprocess hung, prompt looping, exceeded
`circuit_breaker.max_phase_timeout_min` — `pipeline-watch kill` is the
operator handle that confirms exit, writes a `killed_by_operator` outcome
sidecar, deletes the phase marker, and releases the tick lock if (and only
if) the killed tick owned it.

## Prerequisites

- Shell access to the host running `pipeline-watch`.
- The TODO id, or willingness to kill all in-flight phases.
- The state dir is the one the wedged tick wrote to (default `~/.hermes`,
  honors `PIPELINE_STATE_DIR`).

## See what's in flight first

```bash
ls .hermes/phase_started/
```

Each `TODO-N.json` file represents one running phase. Inspect for context:

```bash
cat .hermes/phase_started/TODO-7.json
```

You'll see `child_pid`, `job_id`, `tick_id`, and the phase name. If neither
`child_pid` nor `job_id` is present, kill confirmation will degrade — see
Troubleshooting.

## Steps

### Kill one phase by TODO id

```bash
uv run pipeline-watch kill --todo TODO-7
```

What happens (`hermes_pipeline.cli.cmd_kill`):

1. Read `.hermes/phase_started/TODO-7.json`.
2. SIGTERM the session process group of `child_pid`. Wait up to 5s, then
   SIGKILL. Confirm the pid is gone.
3. If `job_id` present, also send `hermes run kill <job_id>` as a belt-and-
   suspenders second handle.
4. Append `outcome=killed_by_operator` to `.hermes/outcomes/<tick_id>.json`
   (the immutable sidecar; existing terminal outcomes are not overwritten —
   `FileExistsError` is swallowed).
5. Delete `.hermes/phase_started/TODO-7.json`.
6. If the tick lock's holder.json names the killed tick, release
   `.hermes/tick.lock/`.

Exit code:
- `0` — every targeted phase exited cleanly.
- `1` — at least one phase could not be confirmed dead; its marker was left
  in place to preserve in-flight visibility.
- `2` — usage error (no `--all`, no `--todo`, or `--todo` referenced a
  TODO with no marker).

### Kill every in-flight phase

```bash
uv run pipeline-watch kill --all
```

Same per-phase semantics; iterates every `*.json` in
`.hermes/phase_started/`. Use after a system-wide stall (e.g. host reboot
left orphans).

## Verification

1. The phase marker is gone:

   ```bash
   ls .hermes/phase_started/ | grep TODO-7  # should print nothing
   ```

2. The outcome sidecar records the kill:

   ```bash
   jq '.outcomes[] | select(.outcome=="killed_by_operator")' \
     .hermes/outcomes/<tick_id>.json
   ```

3. If the killed tick owned the lock, the next Hermes cron tick should
   acquire the tick lock without complaining about a stale holder:

   ```bash
   ls .hermes/tick.lock/ 2>/dev/null  # should print nothing
   ```

## Why the confirmation step matters

A SIGTERM-ignoring Claude subprocess can keep mutating the repo (running
tests, editing files, pushing commits) after the kill command "succeeds".
If `_confirm_pid_exited` (in `cli.py:82`) cannot prove the pid is gone,
`pipeline-watch kill` leaves the marker in place and exits non-zero. The
next tick will still see the TODO as in-flight and skip it — preventing two
runners from racing on the same TODO.

**Do not delete the marker by hand to "fix" a failed kill.** The pid is
still alive. Track it down (`ps -p <pid>`) and SIGKILL the process group
yourself before retrying.

## Troubleshooting

**`no in-flight phases`.**
Nothing under `.hermes/phase_started/`. Either nothing is running, or you're
pointing at the wrong state dir. Check `PIPELINE_STATE_DIR`.

**`no in-flight phase for TODO-7`.**
The marker doesn't exist. If you expected one, the phase already terminated
(check `.hermes/ready_for_review/todo-7.json` and `.hermes/outcomes/<tick>.json`).

**`warning: no child_pid or job_id on marker; cannot confirm kill`.**
An older marker shape, or a phase that crashed mid-write. The command
leaves the marker in place and exits 1. Manually identify and kill any
surviving processes, then delete the marker:

```bash
ps -ef | grep claude
rm .hermes/phase_started/TODO-7.json
```

**`error: failed to confirm exit for TODO-7.json (pid=12345 ...)`.**
The pid is still alive after SIGTERM + 5s + SIGKILL + 2s. The process group
is probably stuck in uninterruptible I/O (network, NFS, FUSE). Investigate
the pid manually. Re-run `pipeline-watch kill --todo TODO-7` once the
process actually exits.

**The tick lock is still held after kill.**
By design — the lock is only released when its holder.json names a tick
that we just confirmed dead. If you killed a phase whose tick already
finished, the current lock holder is some other tick. Inspect
`.hermes/tick.lock/holder.json` to see whose it is, and decide whether to
kill that tick as well.

## Related

- [Pipeline state machine](hermes-state-machine.md) — `pipeline-watch kill` row
- `hermes_pipeline.cli.cmd_kill` for the authoritative behavior
