"""Decision + outcome storage. Decisions are immutable; outcomes are a sidecar."""
from __future__ import annotations
import json
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

def persist(state_dir: Path, d: HermesSelectionDecision) -> Path:
    """Write a decision file. Raises FileExistsError if already persisted."""
    out = _decisions_dir(state_dir) / f"{d.tick_id}.json"
    if out.exists():
        raise FileExistsError(f"decision {d.tick_id} already persisted; decisions are write-once")
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(d.to_json())
    tmp.rename(out)
    return out

def append_outcome(state_dir: Path, tick_id: str, *, outcome: str, detail: dict) -> Path:
    """Write an outcome sidecar. Raises FileExistsError if already written."""
    out = _outcomes_dir(state_dir) / f"{tick_id}.json"
    if out.exists():
        raise FileExistsError(f"outcome {tick_id} already written; sidecars are write-once")
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"tick_id": tick_id, "outcome": outcome, "detail": detail}, sort_keys=True))
    tmp.rename(out)
    return out

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
