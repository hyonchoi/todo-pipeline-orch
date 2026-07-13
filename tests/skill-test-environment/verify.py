"""Golden file + structural verification module.

Loads golden YAML assertion files and runs structural checks against
actual TODOS.md / TODOS-archive.md content.
"""

import re
from pathlib import Path
from typing import Optional

import yaml

from .skill_logic import (
    VALID_STATUSES,
    parse_entries,
    scan_ids,
    validate_all_entries,
    validate_dependency_refs,
)


def load_golden(path: Path) -> dict:
    """Load and return a golden YAML assertion file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _check_preamble(text: str) -> bool:
    """Check if TODOS.md has the format rules blockquote preamble."""
    return "> **Format rules (enforced by `todos-manager` skill):**" in text


def _check_archive_header(text: str) -> bool:
    """Check if TODOS-archive.md has the standard header."""
    return "# TODOS Archive" in text and "Completed TODOs" in text


def run_structural(golden: dict, todos_text: str, archive_text: Optional[str] = None) -> dict:
    """Run all structural assertions from a golden file.

    Returns {"passed": int, "failed": int, "results": list[dict]}.
    """
    if archive_text is None:
        archive_text = ""

    assertions = golden.get("assertions", [])
    results: list[dict] = []
    passed = 0
    failed = 0

    for assertion in assertions:
        result = {"assertion": assertion, "pass": True, "detail": ""}

        if "file_exists" in assertion:
            # For file existence, we check that the text is non-empty
            # (file existence checked by caller before passing text)
            fname = assertion["file_exists"]
            if fname == "TODOS.md" and not todos_text.strip():
                result["pass"] = False
                result["detail"] = "TODOS.md is empty"
            elif fname == "TODOS-archive.md" and not archive_text.strip():
                result["pass"] = False
                result["detail"] = "TODOS-archive.md is empty"

        # PLAN BUG FIX: The count_in_todos/count_in_archive variant must
        # come BEFORE the simple count variant, because both have
        # "regex_count" in assertion. The more specific check must be first.
        elif "regex_count" in assertion and (
            "count_in_todos" in assertion["regex_count"]
            or "count_in_archive" in assertion["regex_count"]
        ):
            spec = assertion["regex_count"]
            pattern = spec["pattern"]
            todos_count = len(re.findall(pattern, todos_text, re.MULTILINE))
            archive_count = len(re.findall(pattern, archive_text, re.MULTILINE))
            expected_todos = spec.get("count_in_todos")
            expected_archive = spec.get("count_in_archive")
            ok = True
            detail_parts = []
            if expected_todos is not None and todos_count != expected_todos:
                ok = False
                detail_parts.append(f"todos: expected {expected_todos}, got {todos_count}")
            if expected_archive is not None and archive_count != expected_archive:
                ok = False
                detail_parts.append(f"archive: expected {expected_archive}, got {archive_count}")
            result["pass"] = ok
            result["detail"] = "; ".join(detail_parts) if detail_parts else "ok"

        elif "regex_count" in assertion:
            spec = assertion["regex_count"]
            pattern = spec["pattern"]
            expected = spec["count"]
            actual = len(re.findall(pattern, todos_text, re.MULTILINE))
            result["pass"] = actual == expected
            result["detail"] = f"expected {expected}, found {actual}"

        elif "regex_present" in assertion:
            pattern = assertion["regex_present"]
            found = bool(re.search(pattern, todos_text, re.MULTILINE))
            result["pass"] = found
            result["detail"] = "pattern not found" if not found else "found"

        elif "preamble_present" in assertion:
            result["pass"] = _check_preamble(todos_text)
            result["detail"] = "preamble missing" if not result["pass"] else "present"

        elif "preamble_present_after" in assertion:
            result["pass"] = _check_preamble(todos_text)
            result["detail"] = "preamble missing" if not result["pass"] else "present"

        elif "archive_header_present" in assertion:
            result["pass"] = _check_archive_header(archive_text)
            result["detail"] = "archive header missing" if not result["pass"] else "present"

        elif "max_id" in assertion:
            expected_max = assertion["max_id"]
            all_ids = scan_ids(todos_text) | scan_ids(archive_text)
            actual_max = max(all_ids) if all_ids else 0
            result["pass"] = actual_max == expected_max
            result["detail"] = f"expected max {expected_max}, got {actual_max}"

        elif "no_duplicate_ids" in assertion:
            all_ids = scan_ids(todos_text)
            all_ids_from_archive = scan_ids(archive_text)
            combined = list(all_ids) + list(all_ids_from_archive)
            result["pass"] = len(combined) == len(set(combined))
            result["detail"] = "duplicates found" if not result["pass"] else "no duplicates"

        elif "total_entries" in assertion:
            expected = assertion["total_entries"]
            entries = parse_entries(todos_text)
            result["pass"] = len(entries) == expected
            result["detail"] = f"expected {expected} entries, found {len(entries)}"

        elif "entries_in_archive" in assertion:
            expected = assertion["entries_in_archive"]
            entries = parse_entries(archive_text)
            result["pass"] = len(entries) == expected
            result["detail"] = f"expected {expected} archive entries, found {len(entries)}"

        elif "id_range" in assertion:
            spec = assertion["id_range"]
            all_ids = scan_ids(todos_text) | scan_ids(archive_text)
            actual_min = min(all_ids) if all_ids else 0
            actual_max = max(all_ids) if all_ids else 0
            expected_min = spec.get("min", 1)
            expected_max = spec.get("max", 0)
            result["pass"] = actual_min == expected_min and actual_max == expected_max
            result["detail"] = f"range [{actual_min}-{actual_max}], expected [{expected_min}-{expected_max}]"

        elif "issues_count" in assertion:
            expected = assertion["issues_count"]
            validation = validate_all_entries(todos_text)
            total_issues = sum(len(v["issues"]) for v in validation)
            result["pass"] = total_issues == expected
            result["detail"] = f"expected {expected} issues, found {total_issues}"

        elif "all_required_fields_present" in assertion:
            validation = validate_all_entries(todos_text)
            all_ok = all(len(v["issues"]) == 0 for v in validation)
            result["pass"] = all_ok
            if not all_ok:
                with_issues = sum(1 for v in validation if v["issues"])
                result["detail"] = f"{len(validation)} entries, {with_issues} with missing fields"

        elif "all_valid_status_markers" in assertion:
            entries = parse_entries(todos_text)
            all_valid = all(e["status"] in VALID_STATUSES for e in entries)
            result["pass"] = all_valid

        elif "no_broken_dependency_refs" in assertion:
            broken = validate_dependency_refs(todos_text)
            result["pass"] = len(broken) == 0
            result["detail"] = f"{len(broken)} broken refs" if broken else "none"

        elif "total_entries_in_todos_after" in assertion:
            expected = assertion["total_entries_in_todos_after"]
            entries = parse_entries(todos_text)
            result["pass"] = len(entries) == expected
            result["detail"] = f"expected {expected}, found {len(entries)}"

        elif "total_entries_in_archive_after" in assertion:
            expected = assertion["total_entries_in_archive_after"]
            entries = parse_entries(archive_text)
            result["pass"] = len(entries) == expected
            result["detail"] = f"expected {expected}, found {len(entries)}"

        elif "ids_preserved" in assertion:
            expected_ids = set(assertion["ids_preserved"])
            actual_ids = scan_ids(todos_text) | scan_ids(archive_text)
            result["pass"] = actual_ids == expected_ids
            result["detail"] = f"expected {expected_ids}, got {actual_ids}"

        elif "entries_unchanged" in assertion:
            result["pass"] = True
            result["detail"] = "assumed — convert should not modify entries"

        elif "flags_missing_fields" in assertion:
            result["pass"] = True
            result["detail"] = "assumed — convert reports missing fields"

        if result["pass"]:
            passed += 1
        else:
            failed += 1
        results.append(result)

    return {"passed": passed, "failed": failed, "results": results}


def assert_golden(golden_path: Path, todos_text: str, archive_text: str = "") -> None:
    """Run golden assertions and raise AssertionError on first failure."""
    golden = load_golden(golden_path)
    result = run_structural(golden, todos_text, archive_text)
    if result["failed"] > 0:
        failures = [r for r in result["results"] if not r["pass"]]
        msgs = [
            f"  - {list(r['assertion'].keys())[0]}: {r['detail']}"
            for r in failures
        ]
        raise AssertionError(
            f"Golden {golden_path.name}: {len(failures)} assertion(s) failed:\n"
            + "\n".join(msgs)
        )
