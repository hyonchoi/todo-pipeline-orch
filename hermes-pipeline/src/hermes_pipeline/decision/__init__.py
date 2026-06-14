"""Hermes-agent selection sub-package — public API.

Imports:
    from hermes_pipeline.decision import (
        HermesSelectionDecision, SelectionContext, run_selection,
    )

`run_selection(tick_id, ctx, *, cfg)` is the orchestration entrypoint
called by the Hermes `pipeline-tick` command. It builds the prompt, calls
the Anthropic API, parses the response, persists an immutable decision,
and returns it.
"""
from .schema import HermesSelectionDecision, SelectionContext, Outcome

__all__ = [
    "HermesSelectionDecision",
    "SelectionContext",
    "Outcome",
    "run_selection",
]

def run_selection(tick_id, ctx, *, cfg):
    """Stub — implemented in Task 6 once agent.py + store.py exist."""
    raise NotImplementedError("implemented in Task 6")
