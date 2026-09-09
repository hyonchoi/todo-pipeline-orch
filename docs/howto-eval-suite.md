# How to run the selection eval suite

Exercise the live selection agent against a fixture battery and
verify the model picks (or correctly refuses to pick) the expected TODO. Use
this for intentional live validation of `decision/agent.py`, the prompt
template (`hermes_pipeline/data/prompts/selection.md`), or the configured model.

## Prerequisites

- Opt in for each live invocation with exactly `TPO_RUN_LIVE_EVALS=1`.
  Run outside a Kanban worker: the presence of any `HERMES_KANBAN_*`
  environment variable, even an empty one, refuses live evaluations.
  Do not unset worker context to bypass this guard.
- An installed, authenticated Hermes CLI (preferred), or Claude Code CLI
  (fallback when Hermes is unavailable). Each client resolves authentication
  internally; `ANTHROPIC_API_KEY` is not the eval permission gate.
- `uv sync` has run at the repo root.
- The bundled prompt at `hermes_pipeline/data/prompts/selection.md`, or
  `SELECTION_PROMPT_PATH` pointing to an override. The runner reads the bytes
  and hashes them on every call.

## Steps

1. Run the full battery:

   ```bash
   TPO_RUN_LIVE_EVALS=1 uv run pytest tests/eval/ -v
   ```

   Each fixture under `tests/eval/selection/*.md` produces one parameterized
   test. Each fixture calls the selected live client; provider usage and cost
   depend on the configured model and account.

2. Run a single fixture by id:

   ```bash
   TPO_RUN_LIVE_EVALS=1 uv run pytest tests/eval/ -v -k respects_in_flight
   ```

   Fixture id == filename stem. See `tests/eval/selection/` for the current
   list (`clean_strict`, `empty_todos`, `heavy_drift_no_metadata`,
   `injection_attempt`, `mid_drift_freeform_notes`,
   `outcome_aware_avoids_failed`, `respects_in_flight`, `clean_strict_schema`).

3. Pin a different model for a one-off run (e.g. testing a fallback):

   ```bash
   TPO_RUN_LIVE_EVALS=1 EVAL_MODEL=claude-sonnet-4-6 uv run pytest tests/eval/ -v
   ```

## Adding a fixture

Each fixture is a markdown file with YAML frontmatter (assertions plus the
legal candidate set) and a body (the rendered candidate list the orchestrator
would compile from `tpo:todo` issues). The runner builds
`SelectionContext(selection_markdown=body, candidate_ids=...)`; the body is
appended to the prompt inside the `<candidate_todos>` fence.

```markdown
---
name: my_new_case
candidate_ids: [TODO-1, TODO-2]
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

Frontmatter keys the runner honors (`tests/eval/runner.py`):
- `candidate_ids` — ordered list of ids the model may legally pick, passed as
  `SelectionContext.candidate_ids`; a pick outside it is rejected with
  `pick_not_known`, and the runner asserts `picked` is in this list
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

Skip (default invocation, worker context, or unavailable clients):

```
SKIPPED [1] live evals require TPO_RUN_LIVE_EVALS=1, no Kanban worker context, and hermes or Claude Code CLI
```

Fail (model picked wrong TODO):

```
AssertionError: picked='TODO-2' not in ['TODO-1'];
rationale='TODO-2 is highest priority...'
```

The rationale is printed on every failure — read it. It is often the cheapest
signal about whether the prompt is leading the model astray.

## Continuous integration

The normal `uv run pytest` gate skips live fixtures without probing installed
clients. Provider-free eval-runner tests still run. There is no dedicated
`eval.yml` workflow; a live run requires the explicit command-scoped opt-in
above and an authenticated client outside worker context.

## Isolation boundary

Selection queries disable client tools and strip inherited `HERMES_KANBAN_*`
variables. Hermes uses `-t none`; Claude disables builtins and configured MCP
servers. Authentication and profile configuration remain available. These
controls prevent inherited worker authority and normal tool access; they do
not sandbox arbitrary user-configured hooks, plugins, or executable wrappers.
Use a controlled client configuration for intentional live evaluations.

## Troubleshooting

**Every test is SKIPPED.**
Check that this invocation has exactly `TPO_RUN_LIVE_EVALS=1`, that it is
outside any `HERMES_KANBAN_*` context, and that Hermes or Claude Code CLI is
installed. Permission is checked both at collection and before each fixture
calls a provider, including when backend detection was cached. Authenticate
the selected client before an intentional live run.

**`HermesCallError: hermes call failed with return code N` thrown from `hermes_adapter.py`.**
Hermes returned a non-zero exit code. Check authentication and model
configuration in a controlled terminal outside worker context. Raw provider
stdout and stderr are intentionally omitted from the exception.

**Parse error: `picked=None, rationale='parse_error: invalid_response'`.**
The model returned non-JSON or unfenced text. `agent.py:_parse` strips ` ```json `
fences. Raw response content and decoder details are intentionally omitted from
the rationale. If the model is returning prose, the prompt likely lost its
structured-output instructions — diff against
`hermes_pipeline/data/prompts/selection.md` HEAD.

**A fixture that used to pass now fails.**
Either the prompt drifted (run `sha256sum` on
`hermes_pipeline/data/prompts/selection.md` or the configured
`SELECTION_PROMPT_PATH` override and compare to
`selection.expected_prompt_sha`), or the model id moved. Both are investigable;
do not silently update `expected_picked_in` to match the new behavior.

## Related

- [How to recover from a prompt SHA mismatch](howto-prompt-sha-mismatch.md)
- [Pipeline state machine](hermes-state-machine.md)
- [Selection seat contract](../hermes_pipeline/decision/README.md)
