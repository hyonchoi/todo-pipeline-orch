"""Plan-gate status read — pure, read-only view of gate state.

Owns the single canonical rule for "is this gate resolved" (rejection
sidecar takes priority over kanban status). Writes to the rejection
sidecar and kanban status live in `gates.py`/`approve_plan.py`; this module
only reads.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from .gates import PLAN_GATE_PHASE_KEY, read_rejection_sidecar
from .kanban_tasks import get_todo_kanban_tasks


class GateStatus(Enum):
    """Canonical plan-gate states observed from kanban + sidecar.

    BLOCKED  — kanban task exists, still blocked (operator has not acted).
    READY    — kanban task exists, ready (operator reviewed but did not act).
    RUNNING  — kanban task exists, running/done (approved via approve-plan).
    FAILED   — rejection sidecar exists (rejected via approve-plan --reject).
    UNKNOWN  — no kanban task found for this gate_key and tick_id.
    """
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    FAILED = "failed"
    UNKNOWN = "unknown"


def gate_status(
    *,
    state_dir: Path | str,
    project_slug: str,
    gate_key: str = PLAN_GATE_PHASE_KEY,
    tick_id: str,
) -> GateStatus:
    """Pure read of plan-gate state from kanban + rejection sidecar.

    Called by both the runner (gate skip decision) and the dispatcher
    (maybe_plan_gate_ready readiness check). No side effects.
    """
    # Rejection sidecar takes priority — even if kanban shows blocked, a
    # sidecar means the operator rejected the plan.
    sidecar = read_rejection_sidecar(state_dir=state_dir, tick_id=tick_id)
    if sidecar is not None:
        return GateStatus.FAILED

    tasks = get_todo_kanban_tasks(project_slug, tick_id)
    gate = tasks.get(gate_key)
    if gate is None:
        return GateStatus.UNKNOWN

    try:
        return GateStatus(gate.status)
    except ValueError:
        # Kanban uses "done" for completed tasks, but our enum has "running"
        # (the approved status). Map "done" → RUNNING so approved gates
        # don't stall. Any other unknown status → UNKNOWN.
        if gate.status == "done":
            return GateStatus.RUNNING
        return GateStatus.UNKNOWN
