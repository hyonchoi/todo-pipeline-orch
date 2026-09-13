"""Tick-owned recovery routing for a phase's registered execution.

The execution record is the source of truth for attempt state; Kanban stays
the authority for live card state. When a phase attempt ends in a terminal
failure with confirmed cleanup, the tick asks ``auto_approve_resume`` for an
approval under its closed policy and, when approved, retires the phase's
stale cards and issues one card for the next attempt generation. A refusal
writes the validation-blocked marker, which remains the human boundary;
exhaustion also abandons the run.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .agent_execution import TERMINAL, ExecutionError, ExecutionStore, identity_matches
from .agent_recovery import RECOVERY_REASONS, auto_approve_resume, recovery_state
from .kanban_tasks import (
    HERMES_COMMAND_TIMEOUT,
    _archive_tasks,
    _record_validation_blocked,
    phase_cards_in_snapshot,
)
from .result_contract import ResultContractError
from .state import _atomic_write_text

log = logging.getLogger(__name__)

# Tick-only refusal codes; every other code comes from agent_recovery.RECOVERY_REASONS.
RECOVERY_LOCAL_CODES = frozenset({"recovery_archive_unconfirmed", "recovery_admission_stalled"})
# Refusals caused by a lock somebody else holds right now: the next tick retries them.
TRANSIENT_REFUSALS = frozenset({"recovery_busy", "recovery_approval_failed", "recovery_worktree_busy"})
# Card statuses whose worker has given up on the card: Hermes blocks a card on
# request and parks a card blocked twice in triage, where no worker claims it.
GAVE_UP = frozenset({"blocked", "triage"})
# A card whose Hermes worker is still executing; archiving it would orphan the worker.
LIVE = frozenset({"running"})

PROCEED = "proceed"
WAIT = "wait"
REISSUED = "reissued"
REFUSED = "refused"


def phase_cards(*, tenant: str, tick_id: str, phase_key: str) -> list[dict] | None:
    """Non-archived cards of this phase, or None when the snapshot is unreadable."""
    return phase_cards_in_snapshot(tenant=tenant, tick_id=tick_id, phase_key=phase_key)


def archive_cards(task_ids: list[str], *, tenant: str) -> bool:
    return _archive_tasks(list(task_ids), tenant=tenant)


def approve_resume(store: ExecutionStore, identity: str) -> dict:
    """The tick already holds the worktree lock inside ``locked_run_authority``."""
    return auto_approve_resume(store, identity, worktree_lock_held=True)


def last_attempt(store: ExecutionStore, identity: str) -> dict | None:
    """The record's last attempt; None when there is no readable record."""
    try:
        record = store.load(identity)
    except (ExecutionError, OSError, ValueError):
        return None
    return record["attempts"][-1] if record["attempts"] else None


def resumable(attempt: dict) -> bool:
    """A terminal failure with confirmed cleanup; ``exited`` is the ordinary flow."""
    return attempt["status"] in TERMINAL - {"exited"} and attempt["cleanup"] == "confirmed"


def daemon_alive(attempt: dict) -> bool:
    return attempt["status"] not in TERMINAL and identity_matches(attempt["supervisor"])


def approved_generation(store: ExecutionStore, identity: str) -> int | None:
    """Generation whose resume the tick has approved and nobody consumed yet."""
    try:
        state = recovery_state(store, identity)
    except ExecutionError:
        return None
    if state is None or state["state"] != "approved":
        return None
    return state["generation"]


def unblock_marker(state_dir: Path, tick_id: str, key: str, generation: int) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", key)
    return state_dir / "runs" / tick_id / f"unblocked-{safe}-{digest}-g{generation}"


def unblock_once(*, state_dir: Path, tenant: str, tick_id: str, key: str, generation: int, task_id: str,
                 reason: str | None = None) -> bool:
    """Unblock a card at most once per card generation.

    Hermes parks a card blocked twice in triage, so the tick spends at most one
    unblock per card and prefers issuing a new card otherwise. The marker is
    written only after Hermes confirmed the unblock, so a failed attempt does
    not spend the allowance.
    """
    marker = unblock_marker(state_dir, tick_id, key, generation)
    if marker.exists():
        return False
    reason = reason or f"tpo tick: supervisor attempt g{generation} for {key} is still running"
    try:
        result = subprocess.run(["hermes", "kanban", "unblock", task_id, "--reason", reason],
                                capture_output=True, text=True, timeout=HERMES_COMMAND_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("kanban unblock of %s failed: %s", task_id, type(exc).__name__)
        return False
    if result.returncode != 0:
        log.warning("kanban unblock of %s exited %s", task_id, result.returncode)
        return False
    _atomic_write_text(marker, task_id + "\n")
    log.warning("tpo tick unblocked %s (g%s of %s): %s", task_id, generation, key, reason)
    return True


def abandon_run(state_dir: Path, tick_id: str, reason: str) -> bool:
    from .run_registration import abandon_run_if_registered

    abandoned = abandon_run_if_registered(state_dir, tick_id, reason)
    if not abandoned:
        log.warning("tick %s: no registered run to abandon after %s", tick_id, reason)
    return abandoned


def _retire_cards(*, tenant: str, tick_id: str, key: str) -> bool:
    """Archive the phase's quiescent cards; False when a live worker still owns one."""
    cards = phase_cards(tenant=tenant, tick_id=tick_id, phase_key=key)
    if cards is None:
        raise ResultContractError("recovery_archive_unconfirmed")
    if any(card["status"] in LIVE for card in cards):
        return False
    stale = [card["id"] for card in cards]
    if stale and not archive_cards(stale, tenant=tenant):
        raise ResultContractError("recovery_archive_unconfirmed")
    return True


def _refuse(state_dir: Path, *, tick_id: str, key: str, code: str) -> str:
    assert code in RECOVERY_REASONS or code in RECOVERY_LOCAL_CODES, code
    _record_validation_blocked(state_dir, tick_id=tick_id, step_key=key, code=code, reason=code)
    log.warning("tick recovery refused for %s: %s", key, code)
    return REFUSED


def route_recovery(*, store: ExecutionStore, identity: str, state_dir: Path, tenant: str, tick_id: str,
                   key: str, worker, reissue: Callable[[int], None]) -> str:
    """Decide what the tick does for a phase whose card is missing or not done.

    ``worker`` is the phase's current card (``task_id``, ``status``,
    ``generation``) or None. ``reissue(generation)`` creates the card for the
    next attempt generation. Returns PROCEED (no resumable attempt: the
    ordinary admission flow applies), WAIT (a worker or daemon is still
    responsible, or a lock holder must finish first), REISSUED (a new card was
    created) or REFUSED (a marker was written).

    A reissue deliberately skips the scheduler's ``head_mismatch`` guard: HEAD
    moved because the previous generation committed, and the resume policy
    validates HEAD against the progress journal itself.
    """
    attempt = last_attempt(store, identity)
    if attempt is None:
        return PROCEED
    generation = attempt["generation"]
    gave_up = worker is not None and worker.status in GAVE_UP
    card_generation = worker.generation if worker is not None else None
    if attempt["status"] not in TERMINAL:
        if worker is not None and worker.status == "blocked" and daemon_alive(attempt):
            # A worker gave up on a live daemon; the daemon still owns the attempt.
            unblock_once(state_dir=state_dir, tenant=tenant, tick_id=tick_id, key=key,
                         generation=card_generation, task_id=worker.task_id)
        return WAIT
    if not resumable(attempt):
        return PROCEED
    if worker is not None and card_generation > generation:
        if not gave_up:
            return WAIT
        if approved_generation(store, identity) == generation:
            # The reissued card's worker reported admission_failed while the
            # approval is still open: give its daemon one more chance.
            if worker.status == "blocked" and unblock_once(
                    state_dir=state_dir, tenant=tenant, tick_id=tick_id, key=key, generation=card_generation,
                    task_id=worker.task_id,
                    reason=f"tpo tick: resume of {key} as attempt g{card_generation} is approved; "
                           "run the card command again"):
                return WAIT
            return _refuse(state_dir, tick_id=tick_id, key=key, code="recovery_admission_stalled")
    elif worker is not None and not gave_up:
        # The card's own worker is still reporting the terminal outcome.
        return WAIT
    verdict = approve_resume(store, identity)
    if not verdict["approved"]:
        code = verdict["reason"]
        if code in TRANSIENT_REFUSALS:
            log.warning("tick recovery for %s deferred: %s", key, code)
            return WAIT
        if code == "recovery_generation_exhausted":
            if not _retire_cards(tenant=tenant, tick_id=tick_id, key=key):
                return WAIT
            abandon_run(state_dir, tick_id, code)
        return _refuse(state_dir, tick_id=tick_id, key=key, code=code)
    if not _retire_cards(tenant=tenant, tick_id=tick_id, key=key):
        return WAIT
    reissue(generation + 1)
    log.warning("tick reissued %s as attempt g%s after %s", key, generation + 1, attempt["status"])
    return REISSUED
