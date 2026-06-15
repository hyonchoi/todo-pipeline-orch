"""Decision + outcome storage. Decisions are immutable; outcomes are a sidecar."""
from __future__ import annotations
import json
import os as _os
import uuid as _uuid
from pathlib import Path
from .schema import HermesSelectionDecision

def _decisions_dir(state_dir: Path) -> Path:
    p = state_dir / "decisions"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _outcomes_dir(state_dir: Path) -> Path:
    p = state_dir / "outcomes"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _atomic_write_once(out: Path, payload: str, label: str) -> Path:
    """Write payload to out atomically; refuse to overwrite an existing file.

    Uses a uuid-suffixed temp file to avoid the shared `.json.tmp` collision
    two concurrent writers for the same tick_id would otherwise race on, and
    relies on `os.link` + `os.unlink` to give us cross-process O_EXCL
    semantics on the final filename (rename() would silently replace on
    POSIX, defeating the write-once contract).
    """
    if out.exists():
        raise FileExistsError(f"{label} {out.name} already written; write-once")
    tmp = out.with_name(f"{out.name}.{_uuid.uuid4().hex}.tmp")
    tmp.write_text(payload)
    try:
        _os.link(str(tmp), str(out))
    except FileExistsError as e:
        tmp.unlink()
        raise FileExistsError(f"{label} {out.name} already written; write-once") from e
    tmp.unlink()
    return out

def persist(state_dir: Path, d: HermesSelectionDecision) -> Path:
    """Write a decision file. Raises FileExistsError if already persisted."""
    out = _decisions_dir(state_dir) / f"{d.tick_id}.json"
    return _atomic_write_once(out, d.to_json(), "decision")

def append_outcome(state_dir: Path, tick_id: str, *, outcome: str, detail: dict) -> Path:
    """Write an outcome sidecar. Raises FileExistsError if already written."""
    out = _outcomes_dir(state_dir) / f"{tick_id}.json"
    payload = json.dumps({"tick_id": tick_id, "outcome": outcome, "detail": detail}, sort_keys=True)
    return _atomic_write_once(out, payload, "outcome")

def load_recent(state_dir: Path, n: int) -> list[dict]:
    """Return the n most recent decisions, joined with outcome sidecars."""
    dec_files = [p for p in _decisions_dir(state_dir).iterdir() if p.is_file() and p.suffix == ".json"]
    decs = sorted(dec_files, key=lambda p: p.name, reverse=True)[:n]
    out = []
    for d_path in decs:
        rec = json.loads(d_path.read_text())
        out_path = _outcomes_dir(state_dir) / d_path.name
        if out_path.exists():
            sidecar = json.loads(out_path.read_text())
            rec["outcome"] = sidecar["outcome"]
            rec["outcome_detail"] = sidecar.get("detail", {})
        else:
            rec["outcome"] = "in_flight"
            rec["outcome_detail"] = {}
        out.append(rec)
    return out

def rotate_if_needed(state_dir: Path, *, hot_cap: int = 50) -> int:
    """Move excess decisions (and outcome siblings) to archive/. Returns count moved."""
    dec_files = [p for p in _decisions_dir(state_dir).iterdir() if p.is_file() and p.suffix == ".json"]
    decs = sorted(dec_files, key=lambda p: p.name)
    if len(decs) <= hot_cap:
        return 0
    archive_d = _decisions_dir(state_dir) / "archive"
    archive_o = _outcomes_dir(state_dir) / "archive"
    archive_d.mkdir(exist_ok=True)
    archive_o.mkdir(exist_ok=True)
    excess = len(decs) - hot_cap
    moved = 0
    for d_path in decs[:excess]:
        d_path.rename(archive_d / d_path.name)
        sib = _outcomes_dir(state_dir) / d_path.name
        if sib.exists():
            sib.rename(archive_o / sib.name)
        moved += 1
    return moved
