"""Thin shim — delegates phase execution to phases.run.

The deterministic selection, auto-tick, and hash-tracking logic
have been removed; Hermes owns scheduling.
"""

from __future__ import annotations


def run_phase(*, todo_id, tick_id, phase_key, project_slug, **kw):
    """Thin shim — delegates to phases.run so regression tests stay green."""
    from .phases import run as phases_run

    return phases_run(
        state_dir=kw.get("state_dir"),
        todo_id=f"TODO-{todo_id}" if isinstance(todo_id, int) else todo_id,
        tick_id=tick_id,
        phase_key=phase_key,
        project_slug=project_slug,
        **{k: v for k, v in kw.items() if k != "state_dir"},
    )
