# How to recover from a prompt SHA mismatch

The selection agent SHA-pins `.hermes/prompts/selection.md` against
`selection.expected_prompt_sha` in `.hermes/config.toml`. On mismatch, the
tick aborts with `picked=None`, fires a Slack alert, and is **explicitly not
counted as a no-progress event** (so it doesn't trip the circuit breaker).
This guide unblocks the pipeline after a mismatch.

## What you'll see

In `.hermes/decisions/<tick_id>.json` the rationale starts with the literal
prefix:

```
prompt_sha_mismatch: expected=abc123def456 actual=789ghi012jkl
```

In the alerts channel:

```
[pipeline-tick <tick_id>] PROMPT SHA MISMATCH: expected=abc123def456
actual=789ghi012jkl. Selection skipped (NOT counted as no-progress).
Check Hermes config repo for prompt drift.
```

`pipeline-watch status` continues to show no new in-flight phase; ticks keep
firing and keep producing the same mismatch until you intervene.

## Prerequisites

- Shell access to the host running `pipeline-watch`.
- `sha256sum` (Linux) or `shasum -a 256` (macOS) on PATH.
- Read access to `.hermes/config.toml` and `.hermes/prompts/selection.md`.
- Hermes CLI installed and authenticated (as of v0.3, selection routes through
  `hermes chat -q`).

## Decide which side is correct

A mismatch means either the prompt file drifted (someone edited it without
updating the pin) or the pin drifted (config rolled back, wrong env). Decide
which is canonical before changing anything.

1. Compute the actual SHA:

   ```bash
   sha256sum .hermes/prompts/selection.md
   # macOS: shasum -a 256 .hermes/prompts/selection.md
   ```

2. Read the pinned SHA:

   ```bash
   grep expected_prompt_sha .hermes/config.toml
   ```

3. Pick one of the two paths below.

## Path A — accept the new prompt (prompt change was intentional)

Use this when a teammate intentionally edited the prompt and the new behavior
is desired. You're updating the pin to match the file.

1. Re-read the prompt to confirm it's the version you want:

   ```bash
   cat .hermes/prompts/selection.md
   ```

2. Update the pin in `.hermes/config.toml`:

   ```toml
   [selection]
   expected_prompt_sha = "<paste the actual sha from step 1 above>"
   ```

3. Run the eval suite against the new prompt before re-arming:

   ```bash
   uv run pytest tests/eval/ -v
   ```

   See [How to run the selection eval suite](howto-eval-suite.md). Do not
   skip this — pin updates without eval coverage are how silent regressions
   ship.

4. Verify by triggering one manual tick:

   ```bash
   uv run pipeline-watch auto
   ```

   Inspect the newest file in `.hermes/decisions/`. The rationale must not
   start with `prompt_sha_mismatch:`.

## Path B — revert the prompt (file change was unintentional)

Use this when the prompt file was edited by mistake (rebase artifact,
accidental save, untrusted source). You're restoring the file to match the
pin.

1. Find the prompt's last good revision in your Hermes config repo:

   ```bash
   cd ~/.hermes && git log -- prompts/selection.md
   ```

2. Restore the file to the version whose SHA matches
   `expected_prompt_sha`:

   ```bash
   git checkout <good-sha> -- prompts/selection.md
   ```

3. Verify the SHA now matches:

   ```bash
   sha256sum prompts/selection.md
   grep expected_prompt_sha ~/projects/<repo>/.hermes/config.toml
   ```

   The two values should be identical.

4. Trigger one manual tick (same as Path A step 4) to confirm selection
   resumes.

## Verification

After either path, `.hermes/decisions/<newest>.json` should show a real
`picked` value (or a non-mismatch `picked=null` with a behavioral
rationale). The Slack alerts channel should stop receiving the dedup'd
mismatch alert within `circuit_breaker.alert_dedup_hours`.

## Why this is treated as a config fault, not a stall

The circuit breaker's no-progress counter watches for `picked=null` decisions
that indicate the agent surveyed the queue and found nothing to do — that's
the signal the project is stuck. A SHA mismatch means selection never ran;
the agent has no opinion. Counting it as no-progress would trip the breaker
on a config typo. The `no-progress definition` in
[hermes-state-machine.md](hermes-state-machine.md) excludes both
`prompt_sha_mismatch:` and `tick_lock_held:` rationales for this reason.

## Troubleshooting

**The mismatch repeats even after I updated the pin.**
You edited `.hermes/config.toml` in a different state dir than the one the
tick reads. Check `PIPELINE_STATE_DIR` (default `~/.hermes`) — config lookup
follows the same root.

**My alert is firing every 5 minutes, not deduped.**
`circuit_breaker.alert_dedup_hours` only dedups identical alert bodies. Each
new tick_id produces a new body. Dedup happens upstream in the alert sink;
if you're seeing per-tick spam, your sink isn't honoring the dedup hash. Fix
or accept until the pin is corrected.

**The SHA matches but selection still returns `picked=null`.**
Not a mismatch — read the rationale. The agent surveyed the queue and chose
not to act. This counts as no-progress and will trip the circuit breaker
after `circuit_breaker.no_progress_threshold` consecutive ticks. Check
TODOS.md for actually-pickable items.

## Related

- [How to run the selection eval suite](howto-eval-suite.md)
- [Pipeline state machine](hermes-state-machine.md)
- [Selection seat contract](../hermes_pipeline/decision/README.md)
