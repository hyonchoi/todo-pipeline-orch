# How to configure selection and circuit breaker via `.hermes/config.toml`

Pipeline behavior that doesn't fit the environment-variable surface lives in
a TOML overlay at `.hermes/config.toml`. Two sections are read today:
`[selection]` (Hermes-agent model + prompt pinning) and `[circuit_breaker]`
(stall detection + cron backoff).

## Prerequisites

- The state dir exists (`~/.hermes` by default, or whatever
  `PIPELINE_STATE_DIR` points to).
- Python 3.12+ — the loader uses stdlib `tomllib`.
- Hermes installed and authenticated (`hermes login`) if you're enabling `auto_execute = true`.

## Steps

1. Create the file if it doesn't exist. There is no default config shipped —
   if `.hermes/config.toml` is absent, the dataclass defaults in
   `hermes_pipeline.config` apply:

   ```bash
   mkdir -p ~/.hermes
   touch ~/.hermes/config.toml
   ```

2. Add the sections you want to override. Every key is optional; unset keys
   keep their default.

   ```toml
   [selection]
   model = "claude-opus-4-7"
   max_tokens = 4000
   auto_execute = false
   prompt_path = ".hermes/prompts/selection.md"
   expected_prompt_sha = "abc123def456..."

   [circuit_breaker]
   no_progress_threshold = 3
   backoff_interval_min = 30
   alert_dedup_hours = 24
   max_phase_timeout_min = 120
   max_tick_duration_min = 10
   ```

3. Verify the TOML parses:

   ```bash
   python3 -c "import tomllib; tomllib.loads(open('$HOME/.hermes/config.toml').read())" && echo ok
   ```

   A `TOMLDecodeError` here will surface as `ValueError: malformed TOML` from
   `config.load_toml_overlay` on the next tick. Fix it now, not at runtime.

## Field reference

### `[selection]`

| Key | Default | Effect |
|---|---|---|
| `model` | `claude-opus-4-7` | Model id passed to `hermes chat -q -m <model>`. Pin a specific snapshot to keep eval results stable; bumping this is a real change — re-run the eval suite. |
| `max_tokens` | `4000` | Cap on the model's response. The selection JSON is small (<1KB typical); raising this rarely helps and costs more. |
| `auto_execute` | `false` | When `false`, decisions are persisted but the phase does not run (shadow mode). Set `true` only after the eval suite passes against the current prompt. |
| `prompt_path` | `.hermes/prompts/selection.md` | File the agent loads and hashes. Path is resolved relative to the working directory of `pipeline-watch`, not the state dir. |
| `expected_prompt_sha` | `None` | If set, mismatch with the file's actual SHA-256 aborts the tick (rationale `prompt_sha_mismatch:...`) without counting as no-progress. Leave unset only in dev. |

### `[circuit_breaker]`

| Key | Default | Effect |
|---|---|---|
| `no_progress_threshold` | `3` | Consecutive `picked=null` decisions (excluding `prompt_sha_mismatch:` and `tick_lock_held:`) before backoff engages. |
| `backoff_interval_min` | `30` | Minutes the cron skip-window holds once the breaker trips. |
| `alert_dedup_hours` | `24` | Identical alert bodies inside this window are suppressed by the sink. |
| `max_phase_timeout_min` | `120` | Upper bound on a single phase invocation. A phase that exceeds this is killable by `pipeline-watch kill` and surfaces as orphaned. |
| `max_tick_duration_min` | `10` | Upper bound on one tick (selection + phase invocation). Beyond this, the stale-marker sweep treats the tick lock as abandoned. |

## Tasks

### Pin a new prompt SHA

1. Compute it: `sha256sum .hermes/prompts/selection.md`
2. Update `expected_prompt_sha` in `[selection]` to the new value.
3. Run the eval suite: see [howto-eval-suite.md](howto-eval-suite.md).

If the eval suite fails, revert the pin — that's the whole point of pinning.

### Flip from shadow mode to live

1. Confirm the eval suite passes with the current `model` + prompt.
2. Set `auto_execute = true`.
3. Watch the next 3 ticks. If `.hermes/outcomes/*.json` shows
   `failed_at_phase_*` outcomes, flip back to `false` and investigate.

### Loosen the circuit breaker during onboarding

When a fresh repo has thin TODOS.md, `picked=null` is common and the breaker
trips fast. Temporarily raise `no_progress_threshold` to `10`. Lower it back
to `3` once the queue is real.

## Verification

The loaded config takes effect on the next tick. Confirm by inspecting the
newest decision and outcome files:

```bash
ls -t .hermes/decisions/ | head -1
ls -t .hermes/outcomes/ 2>/dev/null | head -1
```

Or trigger one manually:

```bash
uv run pipeline-watch auto
```

## Troubleshooting

**`ValueError: malformed TOML at ~/.hermes/config.toml`.**
A syntax error in the file. Run the `tomllib` parse step above to see the
line number.

**My override is ignored.**
The key name doesn't match a dataclass field. `_coerce_section` in
`config.py:76` silently drops unknown keys. Compare your key against the
table above. Common typo: `circuit-breaker` (hyphen) vs `circuit_breaker`
(underscore — required).

**`KeyError: 'ANTHROPIC_API_KEY'` on the next tick.**
Not a config issue — env var missing. As of v0.3, selection routes through Hermes
via `hermes_adapter.hermes_call()`, so `ANTHROPIC_API_KEY` is no longer read
directly by the orchestrator. If you see this error, it likely comes from the
eval suite (which still checks for it as a skip gate). Set it or run
`hermes login` for Hermes auth.

## Related

- [How to run the selection eval suite](howto-eval-suite.md)
- [How to recover from a prompt SHA mismatch](howto-prompt-sha-mismatch.md)
- Authoritative field definitions: `hermes_pipeline/config.py`
