"""Build SelectionContext per tick. Owns stale-marker sweep."""
from __future__ import annotations
import json
import subprocess
import time
from pathlib import Path
from .schema import SelectionContext
from . import store as _store

def _rfr_ids(state_dir: Path) -> list[str]:
    """Extract TODO IDs from ready_for_review/*.json files."""
    d = state_dir / "ready_for_review"
    if not d.exists():
        return []
    out = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            tid = int(p.stem)
            out.append(f"TODO-{tid}")
        except ValueError:
            out.append(p.stem)
    return out

def _phase_started_ids(state_dir: Path, *, max_phase_timeout_min: int) -> list[str]:
    """Extract TODO IDs from phase_started/ markers, sweeping stale files."""
    d = state_dir / "phase_started"
    if not d.exists():
        return []
    cutoff = time.time() - max_phase_timeout_min * 60
    out = []
    for p in d.iterdir():
        if not p.is_file():
            continue
        if p.stat().st_mtime < cutoff:
            p.unlink()
            continue
        out.append(p.stem)
    return out

def build_in_flight(state_dir: Path, *, max_phase_timeout_min: int) -> list[str]:
    """Compute in-flight set: ready_for_review union phase_started, minus stale."""
    return sorted(
        set(_rfr_ids(state_dir))
        | set(_phase_started_ids(state_dir, max_phase_timeout_min=max_phase_timeout_min))
    )

def _kanban_snapshot(project_slug: str) -> dict:
    """Capture current Kanban state via `hermes kanban list`."""
    try:
        r = subprocess.run(
            ["hermes", "kanban", "list", "--project", project_slug, "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {"columns": [], "_error": "kanban snapshot unavailable"}

def _recent_decisions(state_dir: Path, n: int) -> list[dict]:
    """Delegated to store.load_recent."""
    return _store.load_recent(state_dir, n=n)

def build_context(
    *,
    tick_id: str,
    state_dir: Path,
    todos_path: Path,
    project_slug: str,
    max_phase_timeout_min: int,
    recent_n: int = 5,
) -> SelectionContext:
    """Assemble the full SelectionContext for a tick."""
    return SelectionContext(
        todos_md=todos_path.read_text(),
        in_flight=build_in_flight(state_dir, max_phase_timeout_min=max_phase_timeout_min),
        recent_decisions=_recent_decisions(state_dir, recent_n),
        kanban_snapshot=_kanban_snapshot(project_slug),
        project_slug=project_slug,
    )
