"""Plan gate — gate primitives and stub decision sheet generator.

This module extracts only narrow primitives needed for the plan gate.
Ship gate (`ship.py`) keeps `approve_ship` ship-specific (T4 resolution).
"""
from __future__ import annotations

import json
import logging
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
from .kanban_tasks import (
    BLOCKED,
    COMPLETION_STATUSES,
    get_todo_kanban_status,
)
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
# Risk classifier
# ---------------------------------------------------------------------------

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
    sanitized = "".join(c for c in value if c.isprintable())
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


