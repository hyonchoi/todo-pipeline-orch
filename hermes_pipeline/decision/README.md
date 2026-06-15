# hermes_pipeline.decision

This sub-package is the **Hermes-agent selection seat**. It owns:

- `HermesSelectionDecision`, `SelectionContext` — see `schema.py` docstrings.
  These are the **cross-repo contract** consumed by the Hermes config repo
  (`pipeline-tick`, `pipeline-phase` command definitions). Do not duplicate
  them in markdown — the docstrings are authoritative.
- `run_selection(*, tick_id, ctx, cfg)` — orchestration entrypoint:
  build prompt -> call Hermes API via `hermes_adapter.hermes_call()` -> parse -> persist immutable decision
  at `.hermes/decisions/<tick_id>.json` -> return.
- Outcome sidecars at `.hermes/outcomes/<tick_id>.json` are appended by
  `state.py` on terminal merge_status transitions. `store.load_recent()`
  joins decisions + outcomes by tick_id.

State-machine transitions: see `docs/hermes-state-machine.md`.
