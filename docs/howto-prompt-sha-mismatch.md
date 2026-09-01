# How to recover from a prompt SHA mismatch

The selection agent SHA-pins the resolved selection prompt against
`selection.expected_prompt_sha` in `.hermes/config.toml`. By default, that
prompt is bundled at `hermes_pipeline/data/prompts/selection.md`; a project can
override it with `[selection].prompt_path`. On mismatch, the
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
Check TPO selection prompt for drift.
```

`tpo status` continues to show no new in-flight phase; ticks keep
firing and keep producing the same mismatch until you intervene.

## Prerequisites

- Shell access to the host running `tpo`.
- `sha256sum` (Linux) or `shasum -a 256` (macOS) on PATH.
- Read access to `.hermes/config.toml` and the resolved selection prompt.
- Hermes CLI installed and authenticated (as of v0.3, selection routes through
  `hermes chat -q`).

## After upgrading to the GitHub Issues backlog

The bundled prompt changed when the backlog moved from `TODOS.md` to GitHub
Issues ([ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md)): the input
fence is now `<candidate_todos>` (a compiled candidate list, not a file dump)
and the rules refer to candidate entry headers. Its SHA-256 is:

```
11c04ee5cf7fb92e369cc3e095b9f62aef28f52ea815ffd09486ee818032bf6e
```

Operators who pin `selection.expected_prompt_sha` to the bundled prompt will see
a mismatch on the first tick after upgrading. Follow Path A below with this
value; nothing drifted unexpectedly.

## Decide which side is correct

A mismatch means either the prompt file drifted (someone edited it without
updating the pin) or the pin drifted (config rolled back, wrong env). Decide
which is canonical before changing anything.

1. Find the resolved prompt path:

   ```bash
   uv run python - <<'PY'
   import tomllib
   from importlib.resources import as_file, files
   from pathlib import Path

   cfg = Path(".hermes/config.toml")
   prompt_path = None
   if cfg.exists():
       prompt_path = tomllib.loads(cfg.read_text()).get("selection", {}).get("prompt_path")

   if prompt_path:
       print(Path(prompt_path))
   else:
       with as_file(files("hermes_pipeline.data").joinpath("prompts", "selection.md")) as p:
           print(p)
   PY
   ```

2. Compute the actual SHA:

   ```bash
   sha256sum <resolved-prompt-path>
   # macOS: shasum -a 256 <resolved-prompt-path>
   ```

3. Read the pinned SHA:

   ```bash
   grep expected_prompt_sha .hermes/config.toml
   ```

4. Pick one of the two paths below.

## Path A — accept the new prompt (prompt change was intentional)

Use this when the prompt change was intentional and the new behavior is
desired. You're updating the pin to match the resolved prompt.

1. Re-read the prompt to confirm it's the version you want:

   ```bash
   cat <resolved-prompt-path>
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

4. Verify by waiting for the next Hermes cron tick and inspecting
   `.hermes/decisions/`. The rationale must not start with
   `prompt_sha_mismatch:`.

## Path B — revert the prompt (file change was unintentional)

Use this when the prompt file was edited by mistake. You're restoring the file
to match the pin. For the bundled default, restore it in the TPO repository and
ship a normal package change. For a project override, restore the override file.

1. Find the prompt's last good revision:

   ```bash
   git log -- hermes_pipeline/data/prompts/selection.md
   # or, for an override:
   git log -- <configured-prompt-path>
   ```

2. Restore the file to the version whose SHA matches
   `expected_prompt_sha`:

   ```bash
   git checkout <good-sha> -- hermes_pipeline/data/prompts/selection.md
   # or, for an override:
   git checkout <good-sha> -- <configured-prompt-path>
   ```

3. Verify the SHA now matches:

   ```bash
   sha256sum <resolved-prompt-path>
   grep expected_prompt_sha .hermes/config.toml
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
tick reads. Check `tpo config get state_dir` (default `~/.hermes`) — config lookup
follows the same root.

**My alert is firing every 5 minutes, not deduped.**
`circuit_breaker.alert_dedup_hours` only dedups identical alert bodies. Each
new tick_id produces a new body. Dedup happens upstream in the alert sink;
if you're seeing per-tick spam, your sink isn't honoring the dedup hash. Fix
or accept until the pin is corrected.

**The SHA matches but selection still returns `picked=null`.**
Not a mismatch — read the rationale. The agent surveyed the queue and chose
not to act. This counts as no-progress and will trip the circuit breaker
after `circuit_breaker.no_progress_threshold` consecutive ticks. Check that
an open issue carries both `tpo:todo` and `ready-for-agent` (see
[issue tracker](agents/issue-tracker.md#tpo-backlog-items)).

## Related

- [How to run the selection eval suite](howto-eval-suite.md)
- [Pipeline state machine](hermes-state-machine.md)
- [Selection seat contract](../hermes_pipeline/decision/README.md)
