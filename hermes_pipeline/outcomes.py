"""Shared outcome constants — single source of truth for outcome string matching."""
from __future__ import annotations

OUTCOME_PHASE_COMPLETE = "phase_complete"
OUTCOME_ALL_COMPLETE = "all_phases_complete"
OUTCOME_FAILED_PREFIX = "failed_at_phase_"
OUTCOME_PICKED_NONE = "picked_none"

OUTCOMES_DIR = "outcomes"
CURRENT_TICK_ID_FILE = "current_tick_id.txt"
