from __future__ import annotations
import datetime as _dt
import json as _json
from dataclasses import dataclass
from pathlib import Path
import subprocess as _sp
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

def _run_claude_subprocess(
    *,
    claude_cmd: str,
    prompt: str,
    tools: str,
    turns: int,
    timeout: int,
    cwd,
) -> dict:
    """Run the Claude CLI as a subprocess.

    Returns a dict with returncode, stdout, stderr keys.
    Tests monkey-patch this function to avoid hitting the real CLI.
    """
    r = _sp.run(
        [claude_cmd, "-p", prompt, "--tools", tools, "--turns", str(turns)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        check=False,
    )
    return {"returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr}

def _invoke_claude(*, todo_id: str, phase_key: str, tick_id: str, state_dir, project_slug: str, **kw) -> dict:
    """Execute a single phase via Claude subprocess and write ready_for_review on terminal success."""
    phases_cfg = {p.phase_key: p for p in load_phases()}
    phase = phases_cfg.get(phase_key)

    result = _run_claude_subprocess(
        claude_cmd=kw.get("claude_cmd", "claude"),
        prompt=phase.prompt if phase else f"Phase: {phase_key}",
        tools=phase.tools if phase else "none",
        turns=phase.turns if phase else 1,
        timeout=phase.timeout if phase else 1800,
        cwd=kw.get("project_dir"),
    )

    if result["returncode"] != 0:
        raise RuntimeError(
            f"phase failed: rc={result['returncode']} stdout={result['stdout'][:200]}"
        )

    # Write ready_for_review on terminal phase (phase9_*)
    is_terminal = phase_key.startswith("phase9")
    if is_terminal:
        todo_num = int(todo_id.removeprefix("TODO-"))
        from .state import ReadyForReview

        rec = ReadyForReview(
            project=project_slug,
            todo_id=todo_num,
            branch=f"todo-{todo_num}-{phase_key}",
            pr_url="",
            phase_summaries={phase_key: result["stdout"][:200]},
            kanban_task_id=None,
            merge_status="pending",
            created_at=_now_iso(),
            tick_id=tick_id,
        )
        sd = Path(state_dir)
        rfr_dir = sd / "ready_for_review"
        rfr_dir.mkdir(parents=True, exist_ok=True)
        (rfr_dir / f"{todo_num}.json").write_text(rec.to_json())

    return {"status": "success", "phase_key": phase_key, "tick_id": tick_id}

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
