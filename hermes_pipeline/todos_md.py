"""Standalone TODOS.md Spec:/Reference: field extractor.

Not a full markdown parser, and not a reuse of the todos-manager skill's
prose-based logic (which isn't Python-importable, being an LLM-facing
skill). Scans only the sub-bullet block belonging to the requested
todo_id, anchored between its entry header and the next entry header
(or EOF), so a naive regex cannot bleed into a neighboring entry's
fields.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_ENTRY_HEADER_RE = re.compile(
    r"^- \[[ x→~]\] \*\*(TODO-\d+):", re.MULTILINE,
)
_SPEC_RE = re.compile(r"^\s*-\s*\*\*Spec:\*\*[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_REFERENCE_RE = re.compile(r"^\s*-\s*\*\*Reference:\*\*[ \t]*(.+?)[ \t]*$", re.MULTILINE)

_EMPTY_RESULT = {"spec": None, "references": []}


def find_todo_fields(todos_md_path: Path, todo_id: str) -> dict:
    """Locate the TODO-<n> entry in todos_md_path and extract Spec:/Reference:.

    Returns {"spec": str | None, "references": list[str]}.
    Never raises for parsing problems — missing file, missing todo_id, or
    a malformed entry all degrade to the empty/partial result.
    """
    try:
        text = todos_md_path.read_text()
    except (FileNotFoundError, OSError) as e:
        log.warning("todos_md: could not read %s: %s", todos_md_path, e)
        return dict(_EMPTY_RESULT)

    try:
        return _extract(text, todo_id)
    except Exception as e:  # pragma: no cover - defense in depth
        log.warning("todos_md: failed to parse entry for %s: %s", todo_id, e)
        return dict(_EMPTY_RESULT)


def _extract(text: str, todo_id: str) -> dict:
    headers = list(_ENTRY_HEADER_RE.finditer(text))
    start = None
    end = len(text)
    for i, m in enumerate(headers):
        if m.group(1) == todo_id:
            start = m.end()
            if i + 1 < len(headers):
                end = headers[i + 1].start()
            break
    if start is None:
        return dict(_EMPTY_RESULT)

    block = text[start:end]

    spec_match = _SPEC_RE.search(block)
    spec = spec_match.group(1).strip() or None if spec_match else None

    ref_match = _REFERENCE_RE.search(block)
    references: list[str] = []
    if ref_match:
        raw = ref_match.group(1)
        references = [r.strip() for r in raw.split(",") if r.strip()]

    return {"spec": spec, "references": references}
