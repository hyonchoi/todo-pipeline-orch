"""Tests for mock harness report generation."""

from __future__ import annotations

import json


def test_generate_report_marks_blocked_phase_as_failure(tmp_path):
    from hermes_pipeline.test_report import generate_report

    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": "phase_started",
                        "phase_key": "phase_4_development",
                        "timestamp": "2026-07-30T00:00:00Z",
                        "todo_id": "TODO-1",
                    }
                ),
                json.dumps(
                    {
                        "event_type": "phase_blocked",
                        "phase_key": "phase_4_development",
                        "timestamp": "2026-07-30T00:01:00Z",
                        "todo_id": "TODO-1",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = generate_report(events, tmp_path / "reports")

    assert report["failed_phases"] == 1
    assert report["phases"] == [
        {
            "phase_key": "phase_4_development",
            "status": "blocked",
            "duration_ms": 0,
            "error_message": "phase blocked",
            "start_timestamp": "2026-07-30T00:00:00Z",
            "todo_id": "TODO-1",
        }
    ]


def test_summarize_report_lists_blocked_phase(tmp_path):
    from hermes_pipeline.test_report import summarize_report

    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "total_phases": 1,
                "passed_phases": 0,
                "failed_phases": 1,
                "phases": [
                    {
                        "phase_key": "phase_4_development",
                        "status": "blocked",
                        "duration_ms": 0,
                        "error_message": "phase blocked",
                        "start_timestamp": "2026-07-30T00:00:00Z",
                        "todo_id": "TODO-1",
                    }
                ],
            }
        )
        + "\n"
    )

    assert summarize_report(report) == "0/1 phases passed; failed: phase_4_development: blocked"
