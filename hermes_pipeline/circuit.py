"""Circuit breaker — N consecutive no-progress ticks -> backoff + Slack alert."""
from __future__ import annotations
import datetime as _dt
import fcntl
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import logging

log = logging.getLogger(__name__)

from hermes_pipeline.outcomes import (
    OUTCOME_ALL_COMPLETE,
    OUTCOME_FAILED_PREFIX,
    OUTCOME_PHASE_COMPLETE,
    OUTCOME_PICKED_NONE,
    OUTCOME_TICK_STARTED,
)

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)

def _send_slack(*, channel: str, msg: str) -> None:
    try:
        subprocess.run(["hermes", "chan", "message", channel, msg], timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

def _set_cron_interval(*, minutes: int) -> None:
    try:
        subprocess.run(
            ["hermes", "cron", "set", "pipeline-tick", f"*/{minutes} * * * *"],
            timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

@dataclass
class CircuitBreaker:
    state_path: Path
    no_progress_threshold: int
    backoff_interval_min: int
    alert_dedup_hours: int
    slack_channel: str

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"consecutive_no_progress": 0, "last_alert_at": None, "backed_off": False}
        return json.loads(self.state_path.read_text())

    def _save(self, st: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st, sort_keys=True))

    def observe(self, *, picked: str | None, counts_as_no_progress: bool) -> None:
        st = self._load()
        log.debug("circuit breaker observe: picked=%s counts_as_no_progress=%s state=%s",
                  picked, counts_as_no_progress, st)
        if picked is not None:
            st["consecutive_no_progress"] = 0
            if st.get("backed_off"):
                log.debug("circuit breaker: resuming from backoff (was backed_off=True)")
                _set_cron_interval(minutes=5)
                st["backed_off"] = False
            self._save(st)
            return
        if not counts_as_no_progress:
            self._save(st)
            return
        st["consecutive_no_progress"] += 1
        if st["consecutive_no_progress"] >= self.no_progress_threshold and not st.get("backed_off"):
            last = st.get("last_alert_at")
            dedup_ok = True
            if last:
                last_dt = _dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
                if (_now() - last_dt).total_seconds() < self.alert_dedup_hours * 3600:
                    dedup_ok = False
            if dedup_ok:
                log.debug("circuit breaker: sending slack alert after %d consecutive no-progress ticks",
                          st["consecutive_no_progress"])
                _send_slack(
                    channel=self.slack_channel,
                    msg=f"pipeline-tick: {st['consecutive_no_progress']} consecutive no-progress ticks; backing off to {self.backoff_interval_min}m",
                )
                st["last_alert_at"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
            _set_cron_interval(minutes=self.backoff_interval_min)
            st["backed_off"] = True
            log.debug("circuit breaker: backed off to %d min interval", self.backoff_interval_min)
        self._save(st)

    def observe_from_outcomes(
        self,
        *,
        state_dir: Path,
        prior_tick_id: str,
    ) -> None:
        """Observe circuit breaker state from JSONL outcome file.

        Reads .hermes/outcomes/<prior_tick_id>-phases.json and derives
        the no-progress judgment from the outcomes. Called before the
        new selection in _cmd_tick, so picked is always None — the
        outcome file is the sole source of truth.

        - all_phases_complete / phase_complete -> progress (counter reset)
        - failed_at_phase_* -> no progress (counter increment)
        See hermes_pipeline.outcomes for the canonical string constants.
        - No file -> prior tick picked=None, no progress
        - Empty file -> tick still in-flight, don't count
        """
        phases_file = state_dir / "outcomes" / f"{prior_tick_id}-phases.json"
        if not phases_file.exists():
            log.debug("circuit breaker: no outcomes file for tick %s — counting as no-progress", prior_tick_id)
            return self.observe(picked=None, counts_as_no_progress=True)

        # Read with shared lock to prevent partial reads from concurrent writes
        with open(phases_file, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                content = f.read().strip()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        if not content:
            return self.observe(picked=None, counts_as_no_progress=False)

        outcomes = []
        for line in content.split("\n"):
            line = line.strip()
            if line:
                outcomes.append(json.loads(line))

        # Single pass: classify all outcomes in one iteration
        has_phase_complete = False
        has_all_complete = False
        has_failure = False
        has_picked_none = False
        has_tick_started = False
        for o in outcomes:
            outcome = o.get("outcome", "")
            if outcome == OUTCOME_PHASE_COMPLETE:
                has_phase_complete = True
            elif outcome == OUTCOME_ALL_COMPLETE:
                has_all_complete = True
            elif outcome.startswith(OUTCOME_FAILED_PREFIX):
                has_failure = True
            elif outcome == OUTCOME_PICKED_NONE:
                has_picked_none = True
            elif outcome == OUTCOME_TICK_STARTED:
                has_tick_started = True

        # If only tick_started was written (no terminal outcome), the prior tick
        # crashed after persisting but before kanban registration. Count as
        # no-progress so the circuit breaker can detect the stall.
        if has_tick_started and not (
            has_phase_complete
            or has_all_complete
            or has_failure
            or has_picked_none
        ):
            return self.observe(picked=None, counts_as_no_progress=True)

        if has_all_complete or has_phase_complete:
            # Progress detected — reset counter and backoff state
            st = self._load()
            st["consecutive_no_progress"] = 0
            if st.get("backed_off"):
                _set_cron_interval(minutes=5)
                st["backed_off"] = False
            self._save(st)
            return

        if has_failure:
            return self.observe(picked=None, counts_as_no_progress=True)

        if has_picked_none:
            # Selection picked nothing — all TODOs are complete or blocked.
            # Not a failure; the pipeline is idle. Don't count as no-progress.
            return self.observe(picked=None, counts_as_no_progress=False)

        # No terminal outcomes yet — in-flight, update observation without
        # counting as no-progress so the circuit breaker stays aware of the
        # current tick cadence.
        return self.observe(picked=None, counts_as_no_progress=False)
