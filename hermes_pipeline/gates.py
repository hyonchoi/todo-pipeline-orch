"""Plan gate — gate primitives and stub decision sheet generator.

This module extracts only narrow primitives needed for the plan gate.
Ship gate (`ship.py`) keeps `approve_ship` ship-specific (T4 resolution).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .decision.schema import (
    DecisionQuestion,
    DecisionSheet,
    PlanGateError,
    _Option,
    validate_decision_sheet,
)
from .kanban_tasks import get_todo_kanban_status, COMPLETION_STATUSES, BLOCKED
from . import slack

log = logging.getLogger(__name__)


def _decisions_dir(state_dir: Path | str) -> Path:
    """Return the decisions directory, creating it if needed."""
    p = Path(state_dir) / "decisions"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Decision sheet I/O
# ---------------------------------------------------------------------------


def write_decision_sheet(sheet: DecisionSheet, *, state_dir: Path | str) -> Path:
    """Atomically write a decision sheet to disk."""
    d = _decisions_dir(state_dir)
    target = d / f"{sheet.tick_id}-plan.json"
    tmp = target.with_suffix(target.suffix + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(sheet.to_json())
    shutil.move(str(tmp), str(target))
    return target


def read_decision_sheet(
    *, state_dir: Path | str, tick_id: str
) -> DecisionSheet | None:
    """Read and validate a decision sheet. Returns None if not found."""
    path = _decisions_dir(state_dir) / f"{tick_id}-plan.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return validate_decision_sheet(data)
    except (json.JSONDecodeError, PlanGateError):
        return None


# ---------------------------------------------------------------------------
# Rejection sidecar I/O
# ---------------------------------------------------------------------------


REJECTION_SUFFIX = "-rejected.json"


def _rejection_path(state_dir: Path | str, tick_id: str) -> Path:
    return _decisions_dir(state_dir) / f"{tick_id}{REJECTION_SUFFIX}"


def write_rejection_sidecar(
    *,
    state_dir: Path | str,
    tick_id: str,
    reason: str,
    rejection_count: int,
) -> Path:
    """Write a rejection sidecar atomically."""
    target = _rejection_path(state_dir, tick_id)
    # Sanitize reason: strip control chars, length-cap
    reason = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', reason).strip()
    if len(reason) > 500:
        reason = reason[:500]
    payload = json.dumps(
        {
            "tick_id": tick_id,
            "reason": reason,
            "rejection_count": rejection_count,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        sort_keys=True,
    )
    tmp = target.with_suffix(target.suffix + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(payload)
    shutil.move(str(tmp), str(target))
    return target


def read_rejection_sidecar(
    *, state_dir: Path | str, tick_id: str
) -> dict | None:
    """Read rejection sidecar. Returns None if not found."""
    path = _rejection_path(state_dir, tick_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Stub decision sheet generator
# ---------------------------------------------------------------------------


def stub_generate_decision_sheet(
    *,
    plan_md_path: Path | str,
    todo_id: int,
    tick_id: str,
    state_dir: Path | str,
) -> DecisionSheet:
    """Parse a '## Decisions' section from an Autoplan markdown file into a DecisionSheet.

    This is throwaway development infrastructure — it will be replaced when
    Autoplan (gstack skill) is modified to emit JSON directly.

    Expects format:
        ## Decisions

        ### Q1: Question text
        **Classification:** taste
        **Options:** A) desc | B) desc
        **Recommendation:** A
        **Rationale:** why

    Raises PlanGateError if no decisions found or parsing fails.
    """
    plan_md_path = Path(plan_md_path)
    text = plan_md_path.read_text()

    # Find ## Decisions section
    m = re.search(r"^##\s+Decisions\s*\n(.*)", text, re.MULTILINE | re.DOTALL)
    if not m:
        raise PlanGateError(
            f"no '## Decisions' section found in {plan_md_path}"
        )

    decisions_text = m.group(1)

    # Parse individual decision blocks
    blocks = re.split(r"^###\s+", decisions_text, flags=re.MULTILINE)
    questions: list[DecisionQuestion] = []
    q_counter = 0

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Extract question text from first line
        first_line = block.split("\n")[0].strip()
        if not first_line:
            continue

        # Extract fields
        classification_m = re.search(r"\*\*Classification:\*\*\s*(\S+)", block)
        options_m = re.search(r"\*\*Options:\*\*\s*(.+)", block)
        rec_m = re.search(r"\*\*Recommendation:\*\*\s*(\S+)", block)
        rat_m = re.search(r"\*\*Rationale:\*\*\s*(.+)", block)

        if not all([classification_m, options_m, rec_m]):
            continue  # Skip blocks missing required fields

        q_counter += 1
        question_id = f"q{q_counter}"

        # Parse options: "A) desc | B) desc"
        opts_text = options_m.group(1)
        opt_parts = re.split(r"\s*\|\s*", opts_text)
        options: list[_Option] = []
        for part in opt_parts:
            om = re.match(r"([A-Za-z])\)\s*(.+)", part.strip())
            if om:
                options.append(
                    _Option(label=om.group(1), description=om.group(2).strip())
                )

        if len(options) < 2:
            continue  # Skip if we couldn't parse >= 2 options

        questions.append(
            DecisionQuestion(
                question_id=question_id,
                classification=classification_m.group(1),
                prompt=first_line,
                options=options,
                recommendation=rec_m.group(1),
                rationale=rat_m.group(1).strip() if rat_m else "",
                answer=None,
            )
        )

    if not questions:
        raise PlanGateError(f"no valid decisions parsed from {plan_md_path}")

    sheet = DecisionSheet(
        schema_version="1.0",
        todo_id=todo_id,
        tick_id=tick_id,
        questions=questions,
    )

    # Write to disk
    write_decision_sheet(sheet, state_dir=state_dir)
    return sheet


# ---------------------------------------------------------------------------
# Risk classifier
# ---------------------------------------------------------------------------

PLAN_GATE_PHASE_KEY = "phase_2b_plan_gate"

# Keywords that indicate high-blast-radius changes
_HIGH_RISK_KEYWORDS = [
    # Dependency changes
    "depend", "library", "package", "migrate", "migration",
    # Architecture changes
    "refactor", "restructure", "re-architect", "redesign",
    # Security
    "security", "auth", "authentication", "authorization", "permission",
    # Data
    "database", "schema", "migration", "data model", "api contract",
    # Broad scope
    "all", "every", "entire", "global",
]


def is_high_risk(*, todo_id: str, todos_md: str, state_dir: Path | str) -> bool:
    """Classify a TODO as high-risk (needs gate) vs low-risk (bypass).

    High-risk signals:
    1. Dependency-changing: mentions add/changing dependencies
    2. High-blast-radius: refactor, restructure, migration, auth, security
    3. Rejection history: any rejection in this project (rejection_count > 0)

    Returns True if any high-risk signal is present.
    """
    # Find the TODO entry in TODOS.md
    todo_pattern = re.escape(todo_id) + r'[:\s]'
    m = re.search(todo_pattern, todos_md, re.IGNORECASE)
    if not m:
        return True  # Can't find TODO — gate conservatively

    # Extract surrounding context (~500 chars after match start)
    context = todos_md[m.start():m.start() + 500].lower()

    # Check for high-risk keywords
    for kw in _HIGH_RISK_KEYWORDS:
        if kw in context:
            return True

    # Check for prior rejection history
    state = Path(state_dir)
    decisions = _decisions_dir(state)
    if decisions.exists():
        for f in decisions.iterdir():
            if f.suffix == ".json" and f.name.endswith(REJECTION_SUFFIX):
                tick_id = f.name[: -len(REJECTION_SUFFIX)]
                sidecar = read_rejection_sidecar(state_dir=state, tick_id=tick_id)
                if sidecar and sidecar.get("rejection_count", 0) > 0:
                    return True

    return False


# ---------------------------------------------------------------------------
# Override sanitization
# ---------------------------------------------------------------------------

_OVERRIDE_MAX_LENGTH = 500


def _sanitize_override(value: str) -> str:
    """Sanitize an override value to prevent injection.

    Strips control characters, enforces length cap, rejects Python
    expression patterns that could be dangerous if passed to format/eval.
    """
    # Strip control characters (keep printable ASCII + common unicode)
    sanitized = "".join(c for c in value if c.isprintable() and c not in '\x00-\x1f\x7f-\x9f')
    # Length cap
    if len(sanitized) > _OVERRIDE_MAX_LENGTH:
        sanitized = sanitized[:_OVERRIDE_MAX_LENGTH]
    # Reject patterns that look like Python expressions (format-string braces,
    # dunder patterns, dangerous keywords). Standalone parentheses/brackets
    # are allowed (e.g. "Approach (B)", "item [1]").
    if re.search(r'\{[^}]*\}|__|eval|exec|import|lambda', sanitized):
        raise PlanGateError(
            "override value contains disallowed characters or patterns"
        )
    return sanitized


# ---------------------------------------------------------------------------
# Dispatcher pre-check: maybe_plan_gate_ready
# ---------------------------------------------------------------------------


def maybe_plan_gate_ready(
    *,
    project_dir: Path | str,
    project_slug: str,
    prior_tick_id: str,
    state_dir: Path | str,
    slack_channel: str,
) -> None:
    """Detect a plan-gate-ready TODO, and alert once.

    Best-effort: any failure is logged and swallowed so the tick continues.
    Must be called before _tick_project's all_phases_complete early-return,
    because a blocked gate makes all_phases_complete return False.

    Checks:
    1. Decision sheet exists for this tick.
    2. Plan-gate task is 'blocked'.
    3. All phases before the gate are in completion statuses.
    """
    try:
        sheet = read_decision_sheet(state_dir=state_dir, tick_id=prior_tick_id)
        if sheet is None:
            return  # No decision sheet yet — autoplan may not have finished

        status_map = get_todo_kanban_status(project_slug, prior_tick_id)
        gate_status = status_map.get(PLAN_GATE_PHASE_KEY)

        if gate_status is None:
            return  # No gate task registered yet
        if gate_status != BLOCKED:
            return  # Gate already resolved

        # Check all non-gate phases are in completion statuses
        non_gate = {k: v for k, v in status_map.items() if k != PLAN_GATE_PHASE_KEY}
        if not non_gate:
            return  # No phases at all
        if any(s not in COMPLETION_STATUSES for s in non_gate.values()):
            return  # Real work still in flight before gate

        # Gate is ready for human review
        todo_num = sheet.todo_id
        slack.notify(
            slack_channel,
            f":mag: {project_slug} TODO-{todo_num} plan needs review — "
            f"{len(sheet.questions)} decision(s) to approve. "
            f"Run: pipeline-watch approve-plan {project_slug} --todo TODO-{todo_num}",
        )
    except Exception as e:
        log.warning("maybe_plan_gate_ready failed for %s tick %s: %s",
                     project_slug, prior_tick_id, e)
