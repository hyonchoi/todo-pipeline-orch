"""Findings report generator for mock integration test harness.

Transforms JSONL event logs from HarnessMonitor into structured
report.json and human-readable report.md files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _bucket_duration_ms(ms: int) -> str:
    """Normalize duration to human-readable bucket."""
    if ms < 60000:
        return "<1m"
    elif ms < 300000:
        return "1-5m"
    elif ms < 900000:
        return "5-15m"
    else:
        return ">15m"


def generate_report(jsonl_path: Path, output_dir: Path) -> dict[str, Any]:
    """Parse JSONL event log into structured report files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    events = []
    for line in jsonl_path.read_text().strip().splitlines():
        if line.strip():
            events.append(json.loads(line))

    phases_by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        key = event.get("phase_key")
        if key is None:
            continue
        if key not in phases_by_key:
            phases_by_key[key] = {
                "phase_key": key,
                "status": "unknown",
                "duration_ms": 0,
                "error_message": None,
                "start_timestamp": event.get("timestamp"),
                "todo_id": event.get("todo_id"),
            }
        phase = phases_by_key[key]

        if event["event_type"] == "phase_started":
            phase["start_timestamp"] = event.get("timestamp")
            phase["status"] = "started"
        elif event["event_type"] == "phase_completed":
            phase["status"] = "completed"
            if "duration_ms" in event:
                phase["duration_ms"] = event["duration_ms"]
        elif event["event_type"] == "phase_failed":
            phase["status"] = "failed"
            if "duration_ms" in event:
                phase["duration_ms"] = event["duration_ms"]
            if "return_code" in event:
                phase["error_message"] = f"return_code={event['return_code']}"
        elif event["event_type"] == "phase_timed_out":
            phase["status"] = "timeout"
            phase["error_message"] = "phase timeout exceeded"

    phases_list = sorted(phases_by_key.values(), key=lambda p: p["phase_key"])

    passed = sum(1 for p in phases_list if p["status"] == "completed")
    failed = sum(1 for p in phases_list if p["status"] in ("failed", "timeout"))

    report = {
        "total_phases": len(phases_list),
        "passed_phases": passed,
        "failed_phases": failed,
        "phases": phases_list,
    }

    report_json = output_dir / "report.json"
    report_json.write_text(json.dumps(report, indent=2) + "\n")

    report_md = output_dir / "report.md"
    md_lines = [
        "# Pipeline Test Report",
        "",
        f"**Summary:** {passed}/{len(phases_list)} phases passed, {failed} failed.",
        "",
        "## Phase Progression",
        "",
        "| Phase | Status | Duration | Error |",
        "|-------|--------|----------|-------|",
    ]
    for p in phases_list:
        dur_bucket = _bucket_duration_ms(p["duration_ms"])
        md_lines.append(
            f"| {p['phase_key']} | {p['status']} | {dur_bucket} | {p['error_message'] or '-'} |"
        )

    md_lines.append("")
    md_lines.append("## What to Investigate")
    md_lines.append("")
    if failed > 0:
        for p in phases_list:
            if p["status"] in ("failed", "timeout"):
                err = p["error_message"] or "unknown"
                md_lines.append(
                    f"- **{p['phase_key']}**: {p['status']} — {err}"
                )
    else:
        md_lines.append("No failures. Pipeline completed successfully.")
        md_lines.append("")

    report_md.write_text("\n".join(md_lines) + "\n")

    return report


def summarize_report(report_path: Path) -> str:
    """Produce a one-line summary from a report.json file."""
    data = json.loads(report_path.read_text())
    total = data["total_phases"]
    passed = data["passed_phases"]
    failed_phases = [p for p in data["phases"] if p["status"] in ("failed", "timeout")]

    summary = f"{passed}/{total} phases passed"
    if failed_phases:
        failures = ", ".join(f"{p['phase_key']}: {p['status']}" for p in failed_phases)
        summary += f"; failed: {failures}"
    return summary


def diff_reports(prev_path: Path, curr_path: Path) -> list[dict[str, Any]]:
    """Compare two report.json files and return per-phase status diff."""
    prev_data = json.loads(prev_path.read_text())
    curr_data = json.loads(curr_path.read_text())

    prev_by_key = {p["phase_key"]: p for p in prev_data["phases"]}
    curr_by_key = {p["phase_key"]: p for p in curr_data["phases"]}

    all_keys = sorted(set(list(prev_by_key.keys()) + list(curr_by_key.keys())))
    diffs = []

    for key in all_keys:
        p = prev_by_key.get(key, {})
        c = curr_by_key.get(key, {})
        prev_status = p.get("status", "missing")
        curr_status = c.get("status", "missing")
        diffs.append({
            "phase_key": key,
            "prev_status": prev_status,
            "curr_status": curr_status,
            "prev_duration_ms": p.get("duration_ms", 0),
            "curr_duration_ms": c.get("duration_ms", 0),
            "prev_error": p.get("error_message"),
            "curr_error": c.get("error_message"),
            "changed": prev_status != curr_status,
        })

    return diffs


def summarize_diff(diffs: list[dict[str, Any]]) -> str:
    """Produce a one-line diff summary."""
    changed = [d for d in diffs if d["changed"]]
    if not changed:
        return "No phase status changes from previous run"
    parts = []
    for d in changed:
        parts.append(f"{d['phase_key']}: {d['prev_status']} -> {d['curr_status']}")
    return "; ".join(parts)
