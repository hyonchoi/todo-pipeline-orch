from __future__ import annotations
import datetime as _dt
import json as _json
from dataclasses import dataclass
from pathlib import Path
import yaml

@dataclass(frozen=True)
class Phase:
    phase_key: str
    name: str
    prompt: str
    tools: str
    turns: int
    timeout: int = 1800

def load_phases(config_path: Path | str | None = None) -> list[Phase]:
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent.parent / "configs" / "phases.yaml"
    config_path = Path(config_path)
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return [Phase(**p) for p in data["phases"]]

# ---------------------------------------------------------------------------
# Phase_started marker helpers (Task 8)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _marker_path(state_dir: Path, todo_id: str) -> Path:
    d = Path(state_dir) / "phase_started"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{todo_id}.json"

def _write_marker(state_dir: Path, *, todo_id: str, tick_id: str, phase_key: str) -> Path:
    p = _marker_path(state_dir, todo_id)
    p.write_text(_json.dumps({
        "todo_id": todo_id,
        "tick_id": tick_id,
        "phase_key": phase_key,
        "started_at": _now_iso(),
    }, sort_keys=True))
    return p

def _delete_marker(state_dir: Path, todo_id: str) -> None:
    p = _marker_path(state_dir, todo_id)
    if p.exists():
        p.unlink()

def _invoke_claude(*, todo_id: str, phase_key: str, **kw) -> dict:
    """Placeholder seam. The actual invocation logic lands in Task 9 when
    we lift it out of watcher.py. Tests patch this seam."""
    raise NotImplementedError("populated in Task 9 (watcher extraction)")

def run(
    *,
    state_dir,
    todo_id: str,
    tick_id: str,
    phase_key: str,
    **kw,
) -> dict:
    """Library entrypoint for phase execution. Owns the phase_started marker.

    Marker write must happen BEFORE any Claude invocation so that the next
    pipeline-tick sees this TODO as in-flight even if we crash mid-phase.
    """
    sd = Path(state_dir)
    _write_marker(sd, todo_id=todo_id, tick_id=tick_id, phase_key=phase_key)
    try:
        result = _invoke_claude(todo_id=todo_id, phase_key=phase_key, tick_id=tick_id, state_dir=sd, **kw)
    finally:
        _delete_marker(sd, todo_id)
    return result
