"""approve-plan — CLI subcommand to approve or reject a plan-gate decision.

Mirrors the ship-gate approval flow (approve_lock, complete_gate_task) but
operates on the plan-gate decision sheet instead of a PR.

    pipeline-watch approve-plan <project> --todo TODO-N \
        [--approve|--reject] [--override q_id=LABEL] [--reason REASON]
"""
from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
from pathlib import Path

from .decision.schema import DecisionSheet, PlanGateError, validate_decision_sheet
from .gates import (
    PLAN_GATE_PHASE_KEY,
    _sanitize_override,
    read_decision_sheet,
    read_rejection_sidecar,
    write_decision_sheet,
    write_rejection_sidecar,
)
from .kanban_tasks import BLOCKED, KanbanTaskInfo, get_todo_kanban_tasks
from .outcomes import CURRENT_TICK_ID_FILE
from .ship import ApproveRefused, HERMES_TIMEOUT, approve_lock, complete_gate_task

log = logging.getLogger(__name__)


# Reuse HERMES_TIMEOUT from ship.py — avoid duplicating the timeout constant.


# ---------------------------------------------------------------------------
# Tick resolution — find the tick_id for a given TODO-N (DX-C2)
# ---------------------------------------------------------------------------


def _resolve_tick_for_todo(
    *,
    state_dir: Path | str,
    todo_id: int,
    project_slug: str,
) -> str:
    """Return the ULID tick_id whose decision sheet matches the given TODO-N.

    Operators know ``TODO-N``, not the ULID tick_id. Resolution order:
    1. The current tick from ``current_tick_id.txt``, if its decision sheet
       is for this TODO.
    2. A scan of ``decisions/`` for any sheet with a matching ``todo_id``
       (latest wins).
    3. A kanban list lookup for a blocked plan-gate task for this TODO.

    Raises ApproveRefused if no matching tick_id is found.
    """
    state_dir = Path(state_dir)

    # Step 1: the current tick, if its sheet matches this TODO.
    tick_file = state_dir / CURRENT_TICK_ID_FILE
    if tick_file.exists():
        tick_id = tick_file.read_text().strip()
        if tick_id:
            sheet = read_decision_sheet(state_dir=state_dir, tick_id=tick_id)
            if sheet is not None and sheet.todo_id == todo_id:
                return tick_id

    # Step 2: scan the decisions directory for a sheet with this todo_id.
    decisions_dir = state_dir / "decisions"
    if decisions_dir.exists():
        for path in reversed(sorted(decisions_dir.glob("*-plan.json"))):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("todo_id") != todo_id:
                continue
            try:
                validate_decision_sheet(data)
            except PlanGateError:
                continue
            return path.stem[: -len("-plan")]

    # Step 3: fall back to kanban — find a blocked plan-gate task for TODO-N.
    tick_id = _find_blocked_plan_gate_tick(project_slug, todo_id)
    if tick_id:
        return tick_id

    raise ApproveRefused(
        f"no plan-gate decision sheet or kanban task found for TODO-{todo_id} "
        f"(project {project_slug}). Run 'pipeline-watch tick' to start a tick "
        f"for this project, then try again."
    )


def _find_blocked_plan_gate_tick(project_slug: str, todo_id: int) -> str | None:
    """Scan kanban for a blocked plan-gate task for TODO-N; return its tick_id."""
    try:
        result = subprocess.run(
            ["hermes", "kanban", "list", "--tenant", project_slug, "--json"],
            capture_output=True, text=True, timeout=HERMES_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        tasks = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None

    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", [])
    for task in tasks:
        if task.get("status") != BLOCKED:
            continue
        first_line = task.get("body", "").split("\n")[0]
        try:
            header = json.loads(first_line)
        except json.JSONDecodeError:
            continue
        if (
            header.get("phase_key") == PLAN_GATE_PHASE_KEY
            and header.get("todo_id") == f"TODO-{todo_id}"
        ):
            return header.get("tick_id") or None
    return None


# ---------------------------------------------------------------------------
# Gate task resolution
# ---------------------------------------------------------------------------


def _resolve_plan_gate_task(*, project_slug: str, tick_id: str) -> KanbanTaskInfo:
    """Return the plan-gate KanbanTaskInfo for a tick, or raise ApproveRefused."""
    tasks = get_todo_kanban_tasks(project_slug, tick_id)
    gate = tasks.get(PLAN_GATE_PHASE_KEY)
    if gate is None:
        raise ApproveRefused(
            f"no plan-gate task found for tick {tick_id} "
            f"(phase_key={PLAN_GATE_PHASE_KEY}). "
            f"Run 'pipeline-watch tick' to register phases, then try again."
        )
    return gate


# ---------------------------------------------------------------------------
# Override parsing and validation
# ---------------------------------------------------------------------------


def _parse_overrides(override_args: list[str] | None) -> dict[str, str]:
    """Parse ``--override q_id=LABEL`` arguments into ``{question_id: label}``.

    Raises ApproveRefused on malformed input or duplicate question IDs
    (last-wins is ambiguous). Returns an empty dict if no overrides given.
    """
    if not override_args:
        return {}

    overrides: dict[str, str] = {}
    for arg in override_args:
        if "=" not in arg:
            raise ApproveRefused(
                f"invalid override {arg!r} (expected q_id=LABEL, e.g. 'q1=A')"
            )
        q_id, label = arg.split("=", 1)
        q_id = q_id.strip()
        if q_id in overrides:
            raise ApproveRefused(
                f"duplicate override for {q_id!r} (last-wins is ambiguous); "
                f"specify each question at most once"
            )
        overrides[q_id] = label.strip()
    return overrides


def _validate_overrides(*, sheet: DecisionSheet, overrides: dict[str, str]) -> None:
    """Validate override question IDs and labels; sanitize values.

    Raises ApproveRefused if a q_id is unknown or a label is not one of the
    question's option labels. All-or-nothing: raises before any answer write.
    """
    if not overrides:
        return

    by_id = {q.question_id: q for q in sheet.questions}
    for q_id, label in overrides.items():
        question = by_id.get(q_id)
        if question is None:
            raise ApproveRefused(
                f"override references unknown question {q_id!r}; "
                f"valid IDs: {sorted(by_id)}"
            )
        valid_labels = {opt.label for opt in question.options}
        if label not in valid_labels:
            raise ApproveRefused(
                f"override {q_id}={label!r}: label must be one of "
                f"{sorted(valid_labels)}"
            )
        # Defense-in-depth: reject injection patterns before the value is
        # written into the sheet (decision content later flows to prompts).
        # _sanitize_override raises PlanGateError on bad input; wrap in
        # ApproveRefused so all guard refusals flow through one type.
        try:
            _sanitize_override(label)
        except PlanGateError as e:
            raise ApproveRefused(f"override {q_id}={label!r}: {e}") from e


# ---------------------------------------------------------------------------
# Approve / Reject
# ---------------------------------------------------------------------------


def approve_plan(
    *,
    project_dir: Path | str,
    project_slug: str,
    todo_id: int,
    state_dir: Path | str,
    overrides: dict[str, str] | None = None,
    reject_reason: str | None = None,
) -> str:
    """Approve or reject a plan-gate decision sheet. Returns a success summary.

    When ``reject_reason`` is given, reject (write rejection sidecar, archive
    the gate task). Otherwise approve (fill answers, write the sheet back,
    complete the gate task so the tick advances into writing-plan).

    Raises ApproveRefused on any guard refusal (missing sheet, bad override,
    lock contention, gate not blocked).
    """
    with approve_lock(state_dir):
        tick_id = _resolve_tick_for_todo(
            state_dir=state_dir, todo_id=todo_id, project_slug=project_slug,
        )

        sheet = read_decision_sheet(state_dir=state_dir, tick_id=tick_id)
        if sheet is None:
            raise ApproveRefused(
                f"no decision sheet found for tick {tick_id} "
                f"(project {project_slug}). Autoplan may not have finished — "
                f"check the plan for a '## Decisions' section, then try again."
            )
        if sheet.todo_id != todo_id:
            raise ApproveRefused(
                f"decision sheet for tick {tick_id} is for TODO-{sheet.todo_id}, "
                f"not TODO-{todo_id}; the tick_id may be stale."
            )

        gate = _resolve_plan_gate_task(project_slug=project_slug, tick_id=tick_id)
        if gate.status != BLOCKED:
            raise ApproveRefused(
                f"plan-gate for TODO-{todo_id} is '{gate.status}', not 'blocked'; "
                f"it may have already been approved or rejected."
            )

        if reject_reason is not None:
            count = _reject(
                tick_id=tick_id, state_dir=state_dir, gate=gate, reason=reject_reason,
            )
            return (
                f"Rejected plan for TODO-{todo_id} (tick {tick_id}, "
                f"rejection #{count}): {reject_reason}"
            )

        # Approve: validate overrides all-or-nothing before writing answers.
        _validate_overrides(sheet=sheet, overrides=overrides or {})
        _approve(sheet=sheet, state_dir=state_dir, gate=gate, overrides=overrides or {})
        return f"Approved plan for TODO-{todo_id} (tick {tick_id}); gate completed."


def _approve(
    *,
    sheet: DecisionSheet,
    state_dir: Path | str,
    gate: KanbanTaskInfo,
    overrides: dict[str, str],
) -> None:
    """Fill each answer (override or recommendation), persist, complete gate.

    DecisionQuestion/DecisionSheet are frozen, so rebuild via dataclasses.replace.
    """
    answered = [
        dataclasses.replace(
            q, answer=overrides.get(q.question_id, q.recommendation)
        )
        for q in sheet.questions
    ]
    write_decision_sheet(
        dataclasses.replace(sheet, questions=answered), state_dir=state_dir,
    )
    complete_gate_task(gate.task_id)


def _reject(
    *,
    tick_id: str,
    state_dir: Path | str,
    gate: KanbanTaskInfo,
    reason: str,
) -> int:
    """Write the rejection sidecar and archive the gate task. Returns the count.

    The rejection sidecar is the source of truth for a FAILED plan gate
    (check_gate_status reads it, not kanban). The prior-tick handler
    (Task 8) keys off the sidecar to release the tick without stalling.
    We archive rather than complete the gate task: completing would unblock
    the child phase and let a rejected plan proceed.
    """
    existing = read_rejection_sidecar(state_dir=state_dir, tick_id=tick_id)
    count = (existing.get("rejection_count", 0) if existing else 0) + 1
    write_rejection_sidecar(
        state_dir=state_dir, tick_id=tick_id, reason=reason, rejection_count=count,
    )
    _archive_gate_task(gate.task_id)
    return count


def _archive_gate_task(task_id: str) -> None:
    """Archive a kanban gate task via the CLI (no `fail` command exists)."""
    result = subprocess.run(
        ["hermes", "kanban", "archive", task_id],
        capture_output=True, text=True, timeout=HERMES_TIMEOUT,
    )
    if result.returncode != 0:
        raise ApproveRefused(
            f"failed to archive kanban task {task_id}: {result.stderr.strip()[:200]}"
        )
