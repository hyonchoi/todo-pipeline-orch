from __future__ import annotations
import json
from pathlib import Path
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

def test_sha_mismatch_does_not_count(tmp_path):
    sent = []
    with patch("hermes_pipeline.circuit._send_slack", lambda **kw: sent.append(kw)), \
         patch("hermes_pipeline.circuit._set_cron_interval"):
        br = _br(tmp_path)
        for _ in range(10):
            br.observe(picked=None, counts_as_no_progress=False)
    assert sent == []
