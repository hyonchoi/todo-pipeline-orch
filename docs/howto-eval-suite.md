# How to run the selection eval suite

Exercise the live Anthropic selection agent against a fixture battery and
verify the model picks (or correctly refuses to pick) the expected TODO. Use
this before changing `decision/agent.py`, the prompt template
(`.hermes/prompts/selection.md`), or the pinned model id.

## Prerequisites

- `ANTHROPIC_API_KEY` exported in your shell (the suite is `pytest.mark.skipif`
  gated — without it, every fixture is silently skipped).
- `uv sync` has run at the repo root and inside `hermes-pipeline/`.
- A prompt file at `.hermes/prompts/selection.md` (or `SELECTION_PROMPT_PATH`
  pointing to one). The runner reads the bytes and hashes them on every call.

## Steps

1. Run the full battery:

   ```bash
   cd hermes-pipeline
   uv run pytest tests/eval/ -v
   ```

   Each fixture under `tests/eval/selection/*.md` produces one parameterized
   test. Expect ~8 tests today (1 API call each) — budget roughly $0.05–$0.15
   per full run with `claude-opus-4-7`.

2. Run a single fixture by id:

   ```bash
   uv run pytest tests/eval/ -v -k respects_in_flight
   ```

   Fixture id == filename stem. See `tests/eval/selection/` for the current
   list (`clean_strict`, `empty_todos`, `heavy_drift_no_metadata`,
   `injection_attempt`, `mid_drift_freeform_notes`,
   `outcome_aware_avoids_failed`, `respects_in_flight`, `clean_strict_schema`).

3. Pin a different model for a one-off run (e.g. testing a fallback):

   ```bash
   EVAL_MODEL=claude-sonnet-4-6 uv run pytest tests/eval/ -v
   ```

## Adding a fixture

Each fixture is a markdown file with YAML frontmatter (assertions) and a body
(TODOS.md content). The runner parses both and feeds the body in as
`SelectionContext.todos_md`.

```markdown
---
name: my_new_case
in_flight: []
recent_decisions:
  - tick_id: "prior"
    picked: "TODO-2"
    outcome: "failed_at_phase_autoplan"
expected_picked_in: ["TODO-1"]
expected_picked_not: ["TODO-2"]
---
- TODO-1 [priority:high] do the thing
- TODO-2 [priority:high] fix the build
```

Frontmatter keys the runner honors (`hermes-pipeline/tests/eval/runner.py:14`):
- `in_flight` — list of TODO ids passed as `SelectionContext.in_flight`
- `recent_decisions` — list of `{tick_id, picked, outcome}` for the outcome sidecar context
- `expected_picked_in` — assertion: model's `picked` must be one of these
- `expected_picked_not` — assertion: model's `picked` must NOT be any of these
- `expected_picked_is_none` — assertion: model must refuse to pick

Drop the file into `tests/eval/selection/` — it is auto-discovered.

## Verification

Pass output:

```
tests/eval/runner.py::test_selection_fixture[respects_in_flight] PASSED
```

Skip (no API key):

```
SKIPPED [1] eval suite requires ANTHROPIC_API_KEY
```

Fail (model picked wrong TODO):

```
AssertionError: picked='TODO-2' not in ['TODO-1'];
rationale='TODO-2 is highest priority...'
```

The rationale is printed on every failure — read it. It is often the cheapest
signal about whether the prompt is leading the model astray.

## Continuous integration

`.github/workflows/eval.yml` runs the same battery on every PR that touches:

- `hermes-pipeline/src/hermes_pipeline/decision/agent.py`
- `.hermes/prompts/**`
- `hermes-pipeline/tests/eval/**`

The workflow is `continue-on-error: true` — eval failures inform, they do not
block merge. `ANTHROPIC_API_KEY` must be set as a repo secret.

## Troubleshooting

**Every test is SKIPPED.**
`ANTHROPIC_API_KEY` is unset. The `pytest.mark.skipif` at
`tests/eval/runner.py:23` triggers when the env var is missing. Export it and
re-run.

**`anthropic.AuthenticationError: invalid x-api-key`.**
The key is malformed or revoked. Generate a new one in the Anthropic console
and re-export. The eval suite does not retry on auth errors.

**`KeyError: 'ANTHROPIC_API_KEY'` thrown from `agent.py:69`.**
Same root cause — env var missing in the subprocess. If you exported it in a
parent shell, confirm it's visible: `env | grep ANTHROPIC_API_KEY`.

**Parse error: `picked=None, rationale='parse error: ...'`.**
The model returned non-JSON or unfenced text. `agent.py:_parse` strips ` ```json `
fences. If the model is returning prose, the prompt likely lost its structured-
output instructions — diff against `.hermes/prompts/selection.md` HEAD.

**A fixture that used to pass now fails.**
Either the prompt drifted (run `sha256sum .hermes/prompts/selection.md` and
compare to the value in `.hermes/config.toml`'s
`selection.expected_prompt_sha`), or the model id moved. Both are
investigable; do not silently update `expected_picked_in` to match the new
behavior.

## Related

- [How to recover from a prompt SHA mismatch](howto-prompt-sha-mismatch.md)
- [Pipeline state machine](hermes-state-machine.md)
- [Selection seat contract](../hermes-pipeline/src/hermes_pipeline/decision/README.md)
