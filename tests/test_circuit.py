from __future__ import annotations
import json
from pathlib import Path

import pytest
from unittest.mock import patch
from hermes_pipeline.circuit import CircuitBreaker

def _br(tmp_path, **kw):
    return CircuitBreaker(
        state_path=tmp_path / "circuit.json",
        no_progress_threshold=kw.get("threshold", 3),
        backoff_interval_min=kw.get("backoff", 30),
        alert_dedup_hours=kw.get("dedup", 24),
        slack_channel="alerts",
    )

def test_first_two_no_progress_no_alert(tmp_path):
    sent = []
    with patch("hermes_pipeline.circuit._send_slack", lambda **kw: sent.append(kw)):
        br = _br(tmp_path)
        br.observe(picked=None, counts_as_no_progress=True)
        br.observe(picked=None, counts_as_no_progress=True)
    assert sent == []
    assert json.loads((tmp_path / "circuit.json").read_text())["consecutive_no_progress"] == 2

def test_third_trips_alert_and_backoff(tmp_path):
    sent = []
    with patch("hermes_pipeline.circuit._send_slack", lambda **kw: sent.append(kw)), \
         patch("hermes_pipeline.circuit._set_cron_interval") as cron:
        br = _br(tmp_path)
        for _ in range(3):
            br.observe(picked=None, counts_as_no_progress=True)
    assert len(sent) == 1
    cron.assert_called_once_with(minutes=30)

def test_alert_deduped_within_window(tmp_path):
    sent = []
    with patch("hermes_pipeline.circuit._send_slack", lambda **kw: sent.append(kw)), \
         patch("hermes_pipeline.circuit._set_cron_interval"):
        br = _br(tmp_path)
        for _ in range(5):
            br.observe(picked=None, counts_as_no_progress=True)
    assert len(sent) == 1

def test_successful_pick_resets(tmp_path):
    with patch("hermes_pipeline.circuit._send_slack"), \
         patch("hermes_pipeline.circuit._set_cron_interval") as cron:
        br = _br(tmp_path)
        for _ in range(3):
            br.observe(picked=None, counts_as_no_progress=True)
        br.observe(picked="TODO-1", counts_as_no_progress=False)
    state = json.loads((tmp_path / "circuit.json").read_text())
    assert state["consecutive_no_progress"] == 0
    cron.assert_any_call(minutes=5)

class TestObserveFromOutcomes:
    """Tests for CircuitBreaker.observe_from_outcomes()."""

    def test_all_phases_complete_resets_counter(self, state_dir):
        """all_phases_complete outcome -> no_progress=False, counter reset."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        cb.observe(picked=None, counts_as_no_progress=True)
        cb.observe(picked=None, counts_as_no_progress=True)

        st = cb._load()
        assert st["consecutive_no_progress"] == 2

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "all_phases_complete", "completed_at": "2026-01-01T00:00:00Z"}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 0

    def test_phase_complete_resets_counter(self, state_dir):
        """phase_complete outcome -> no_progress=False, counter reset."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        cb.observe(picked=None, counts_as_no_progress=True)
        st = cb._load()
        assert st["consecutive_no_progress"] == 1

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "phase_complete", "phase_key": "phase_2_autoplan", "completed_at": "2026-01-01T00:00:00Z"}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 0

    def test_failed_at_phase_counts_as_no_progress(self, state_dir):
        """failed_at_phase_* outcome -> no_progress=True, counter increments."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "failed_at_phase_phase_4_development", "detail": {"error": "timeout"}}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 1

    def test_no_outcome_file_fallback(self, state_dir):
        """Missing phases file -> fall back to picked=None as no-progress."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 1

    def test_in_flight_no_count(self, state_dir):
        """No outcomes yet (tick still in-flight) -> no_progress=False."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text("")

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 0

    def test_picked_none_not_no_progress(self, state_dir):
        """picked_none outcome -> not counted as no-progress."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "picked_none"}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 0

    def test_high_watermark_no_replay(self, state_dir):
        """Calling observe_from_outcomes twice -> same outcome, no double count."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "phase_complete", "phase_key": "phase_2_autoplan"}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )
        st1 = cb._load()
        assert st1["consecutive_no_progress"] == 0

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )
        st2 = cb._load()
        assert st2["consecutive_no_progress"] == 0

    def test_backoff_reset_on_progress(self, state_dir):
        """When backed off and we get progress, backoff is cleared."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        # Trip the circuit breaker first
        with patch("hermes_pipeline.circuit._send_slack"), \
             patch("hermes_pipeline.circuit._set_cron_interval"):
            for _ in range(3):
                cb.observe(picked=None, counts_as_no_progress=True)

        st = cb._load()
        assert st["backed_off"] is True

        # Now simulate progress via observe_from_outcomes
        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text(
            '{"outcome": "phase_complete", "phase_key": "phase_2_autoplan"}\n'
        )

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )

        st = cb._load()
        assert st["backed_off"] is False
        assert st["consecutive_no_progress"] == 0

    def test_jsonl_parse_error(self, state_dir):
        """If outcome file has invalid JSON, raises JSONDecodeError."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        phases_file.write_text("this is not json\n")

        with pytest.raises(json.JSONDecodeError):
            cb.observe_from_outcomes(
                state_dir=state_dir,
                prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
            )

    def test_in_flight_no_terminal_outcomes(self, state_dir):
        """No terminal outcomes (tick still running) -> no_progress=False."""
        cb = CircuitBreaker(
            state_path=state_dir / "circuit.json",
            no_progress_threshold=3,
            backoff_interval_min=30,
            alert_dedup_hours=24,
            slack_channel="#alerts",
        )

        phases_file = state_dir / "outcomes" / "01HA6PH2V0ZJ7GK0S39D243TQX-phases.json"
        phases_file.parent.mkdir(parents=True, exist_ok=True)
        # Non-empty file with no recognized terminal outcomes
        phases_file.write_text('{"outcome": "some_unknown_outcome"}\n')

        cb.observe_from_outcomes(
            state_dir=state_dir,
            prior_tick_id="01HA6PH2V0ZJ7GK0S39D243TQX",
        )

        st = cb._load()
        assert st["consecutive_no_progress"] == 0


def test_sha_mismatch_does_not_count(tmp_path):
    sent = []
    with patch("hermes_pipeline.circuit._send_slack", lambda **kw: sent.append(kw)), \
         patch("hermes_pipeline.circuit._set_cron_interval"):
        br = _br(tmp_path)
        for _ in range(10):
            br.observe(picked=None, counts_as_no_progress=False)
    assert sent == []
