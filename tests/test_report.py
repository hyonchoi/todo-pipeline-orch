"""Tests for test_report.py — report generation, diff, edge cases."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from hermes_pipeline.test_report import (
    diff_reports,
    generate_report,
    summarize_diff,
    summarize_report,
)


class TestGenerateReport:
    def test_generate_report_from_jsonl(self, tmp_path: Path):
        jsonl_path = tmp_path / "events.jsonl"
        jsonl_path.write_text(
            '{"event_type": "phase_started", "phase_key": "phase_2_autoplan", "timestamp": "2026-07-14T00:00:00Z", "todo_id": 1}\n'
            '{"duration_ms": 5000, "event_type": "phase_completed", "phase_key": "phase_2_autoplan", "timestamp": "2026-07-14T00:00:05Z", "todo_id": 1}\n'
            '{"event_type": "phase_started", "phase_key": "phase_3_writing_plan", "timestamp": "2026-07-14T00:00:05Z", "todo_id": 1}\n'
            '{"duration_ms": 3000, "event_type": "phase_failed", "phase_key": "phase_3_writing_plan", "return_code": 1, "timestamp": "2026-07-14T00:00:08Z", "todo_id": 1}\n'
        )

        output_dir = tmp_path / "reports"
        report = generate_report(jsonl_path, output_dir)

        report_json = output_dir / "report.json"
        assert report_json.exists()

        data = json.loads(report_json.read_text())
        assert "phases" in data
        assert len(data["phases"]) == 2
        assert data["phases"][0]["phase_key"] == "phase_2_autoplan"
        assert data["phases"][0]["status"] == "completed"
        assert data["phases"][1]["status"] == "failed"

        report_md = output_dir / "report.md"
        assert report_md.exists()
        md_content = report_md.read_text()
        assert "phase_2_autoplan" in md_content
        assert "phase_3_writing_plan" in md_content

    def test_generate_report_empty_jsonl(self, tmp_path: Path):
        jsonl_path = tmp_path / "empty.jsonl"
        jsonl_path.write_text("")

        output_dir = tmp_path / "reports"
        report = generate_report(jsonl_path, output_dir)

        assert report["total_phases"] == 0
        assert report["passed_phases"] == 0

    def test_generate_report_all_phases_pass(self, tmp_path: Path):
        jsonl_path = tmp_path / "events.jsonl"
        jsonl_path.write_text(
            '{"event_type": "phase_started", "phase_key": "phase_2", "timestamp": "2026-07-14T00:00:00Z", "todo_id": 1}\n'
            '{"duration_ms": 5000, "event_type": "phase_completed", "phase_key": "phase_2", "timestamp": "2026-07-14T00:00:05Z", "todo_id": 1}\n'
        )

        output_dir = tmp_path / "reports"
        report = generate_report(jsonl_path, output_dir)

        assert report["passed_phases"] == 1
        assert report["failed_phases"] == 0


class TestSummarizeReport:
    def test_summarize_report(self, tmp_path: Path):
        report_file = tmp_path / "report.json"
        report_file.write_text(json.dumps({
            "phases": [
                {"phase_key": "p2", "status": "completed", "duration_ms": 5000, "error_message": None},
                {"phase_key": "p3", "status": "failed", "duration_ms": 3000, "error_message": "hermes timeout"},
                {"phase_key": "p4", "status": "completed", "duration_ms": 2000, "error_message": None},
            ],
            "total_phases": 3,
            "passed_phases": 2,
            "failed_phases": 1,
        }))

        summary = summarize_report(report_file)
        assert "2/3" in summary
        assert "failed" in summary


class TestDiffReports:
    def test_diff_reports_shows_status_change(self, tmp_path: Path):
        prev_report = tmp_path / "report.1.json"
        prev_report.write_text(json.dumps({
            "phases": [
                {"phase_key": "phase_2_autoplan", "status": "failed", "duration_ms": 0, "error_message": "hermes timeout"},
                {"phase_key": "phase_3_writing_plan", "status": "completed", "duration_ms": 5000, "error_message": None},
            ],
            "total_phases": 2, "passed_phases": 1, "failed_phases": 1,
        }))

        curr_report = tmp_path / "report.2.json"
        curr_report.write_text(json.dumps({
            "phases": [
                {"phase_key": "phase_2_autoplan", "status": "completed", "duration_ms": 3000, "error_message": None},
                {"phase_key": "phase_3_writing_plan", "status": "failed", "duration_ms": 2000, "error_message": "kanban timeout"},
            ],
            "total_phases": 2, "passed_phases": 1, "failed_phases": 1,
        }))

        diff = diff_reports(prev_report, curr_report)

        assert len(diff) == 2
        p2_diff = [d for d in diff if d["phase_key"] == "phase_2_autoplan"][0]
        assert p2_diff["prev_status"] == "failed"
        assert p2_diff["curr_status"] == "completed"
        assert p2_diff["changed"] is True

        p3_diff = [d for d in diff if d["phase_key"] == "phase_3_writing_plan"][0]
        assert p3_diff["prev_status"] == "completed"
        assert p3_diff["curr_status"] == "failed"
        assert p3_diff["changed"] is True

    def test_diff_reports_no_change(self, tmp_path: Path):
        prev = tmp_path / "prev.json"
        prev.write_text(json.dumps({
            "phases": [
                {"phase_key": "p2", "status": "completed", "duration_ms": 5000, "error_message": None},
            ],
            "total_phases": 1, "passed_phases": 1, "failed_phases": 0,
        }))

        curr = tmp_path / "curr.json"
        curr.write_text(json.dumps({
            "phases": [
                {"phase_key": "p2", "status": "completed", "duration_ms": 5000, "error_message": None},
            ],
            "total_phases": 1, "passed_phases": 1, "failed_phases": 0,
        }))

        diff = diff_reports(prev, curr)
        assert len(diff) == 1
        assert diff[0]["changed"] is False

    def test_diff_reports_new_phase_added(self, tmp_path: Path):
        prev = tmp_path / "prev.json"
        prev.write_text(json.dumps({
            "phases": [
                {"phase_key": "p2", "status": "completed", "duration_ms": 0, "error_message": None},
            ],
            "total_phases": 1, "passed_phases": 1, "failed_phases": 0,
        }))

        curr = tmp_path / "curr.json"
        curr.write_text(json.dumps({
            "phases": [
                {"phase_key": "p2", "status": "completed", "duration_ms": 0, "error_message": None},
                {"phase_key": "p3", "status": "completed", "duration_ms": 0, "error_message": None},
            ],
            "total_phases": 2, "passed_phases": 2, "failed_phases": 0,
        }))

        diffs = diff_reports(prev, curr)
        p3_diff = [d for d in diffs if d["phase_key"] == "p3"][0]
        assert p3_diff["prev_status"] == "missing"
        assert p3_diff["curr_status"] == "completed"
        assert p3_diff["changed"] is True


class TestSummarizeDiff:
    def test_summarize_diff_no_change(self):
        diffs = [
            {"phase_key": "p2", "changed": False},
        ]
        summary = summarize_diff(diffs)
        assert "No phase status changes" in summary

    def test_summarize_diff_with_changes(self):
        diffs = [
            {"phase_key": "p2", "prev_status": "failed", "curr_status": "completed", "changed": True},
        ]
        summary = summarize_diff(diffs)
        assert "p2" in summary
        assert "failed" in summary
