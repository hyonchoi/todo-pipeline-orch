from __future__ import annotations
import datetime as _dt
import json as _json
import logging
import os as _os
from dataclasses import dataclass
from pathlib import Path
import yaml

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class Phase:
    phase_key: str
    name: str
    prompt: str
    tools: str
    turns: int
    timeout: int = 1800
    terminal: bool = False

def load_phases(config_path: Path | str | None = None) -> list[Phase]:
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "configs" / "phases.yaml"
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

class MarkerHeld(Exception):
    """Raised when a phase_started marker for this todo_id is already present."""

def _write_marker(state_dir: Path, *, todo_id: str, tick_id: str, phase_key: str) -> Path:
    """Atomically claim a phase marker. Refuses to overwrite an existing one."""
    p = _marker_path(state_dir, todo_id)
    payload = _json.dumps({
        "todo_id": todo_id,
        "tick_id": tick_id,
        "phase_key": phase_key,
        "started_at": _now_iso(),
        "pid": _os.getpid(),
    }, sort_keys=True)
    try:
        fd = _os.open(str(p), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
    except FileExistsError as e:
        raise MarkerHeld(f"phase marker held for {todo_id}") from e
    try:
        with _os.fdopen(fd, "w") as f:
            f.write(payload)
    except Exception:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        raise
    return p

def _update_marker_pid(state_dir: Path, todo_id: str, child_pid: int) -> None:
    """Record the subprocess PID into an already-written marker."""
    p = _marker_path(state_dir, todo_id)
    if not p.exists():
        return
    try:
        data = _json.loads(p.read_text())
    except (FileNotFoundError, _json.JSONDecodeError):
        return
    data["child_pid"] = child_pid
    p.write_text(_json.dumps(data, sort_keys=True))

def _delete_marker(state_dir: Path, todo_id: str, *, tick_id: str | None = None) -> None:
    """Delete a marker only if it belongs to the given tick_id.

    Passing tick_id=None deletes unconditionally — reserved for the kill path.
    """
    p = _marker_path(state_dir, todo_id)
    if not p.exists():
        return
    if tick_id is not None:
        try:
            data = _json.loads(p.read_text())
        except (FileNotFoundError, _json.JSONDecodeError):
            return
        if data.get("tick_id") != tick_id:
            return
    try:
        p.unlink()
    except FileNotFoundError:
        pass

def _run_hermes_subprocess(
    *,
    prompt: str,
    tools: str,
    turns: int,
    timeout: int,
    cwd,
    on_pid=None,
) -> dict:
    """Run a phase via `hermes chat -q`.

    Returns a dict with returncode, stdout, stderr, timed_out keys — same
    shape as the old Claude subprocess call for drop-in compatibility.
    The `tools` parameter is a comma-separated list (e.g., "Read,Write,Bash")
    enforced via ``-t/--toolsets`` CLI flag and also encoded in the
    AGENT_MODE prompt header as an advisory constraint.
    Tests monkey-patch this function to avoid hitting the real CLI.
    """
    from .hermes_adapter import hermes_agent_call

    result = hermes_agent_call(
        prompt=prompt,
        tools=tools,
        turns=turns,
        timeout=timeout,
        cwd=cwd,
        on_pid=on_pid,
    )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
    }

class UnknownPhaseError(KeyError):
    """phase_key is not defined in phases.yaml."""

def _render_phase_prompt(template: str, *, todo_id: str, tick_id: str, project_slug: str) -> str:
    """Inject the pipeline context the phase prompt needs.

    A picked TODO must be visible to the LLM — otherwise a TODO-7 pick can
    silently produce work for whatever TODO the LLM latches onto next. We
    prepend a non-templated context header and ALSO support `{todo_id}` /
    `{tick_id}` / `{project_slug}` substitution for phases that want to
    weave the values into prose. `.format()` with named-only fields is safe
    here because every prompt in configs/phases.yaml is repo-owned.
    """
    header = (
        f"Pipeline context:\n"
        f"- todo_id: {todo_id}\n"
        f"- tick_id: {tick_id}\n"
        f"- project_slug: {project_slug}\n"
        f"Work on {todo_id} ONLY. Do not pick a different TODO.\n\n"
    )
    try:
        body = template.format(todo_id=todo_id, tick_id=tick_id, project_slug=project_slug)
    except (KeyError, IndexError):
        # Template uses a `{name}` we don't supply — fall back to verbatim
        # body. The header still scopes the run to this TODO.
        body = template
    return header + body

def _invoke_hermes(*, todo_id: str, phase_key: str, tick_id: str, state_dir, project_slug: str, **kw) -> dict:
    """Execute a single phase via hermes subprocess and write ready_for_review on terminal success."""
    phases_cfg = {p.phase_key: p for p in load_phases()}
    phase = phases_cfg.get(phase_key)
    if phase is None:
        raise UnknownPhaseError(
            f"phase_key {phase_key!r} not found in phases.yaml; "
            f"known keys: {sorted(phases_cfg)}"
        )

    sd = Path(state_dir)

    def _record_child_pid(pid: int) -> None:
        _update_marker_pid(sd, todo_id, pid)

    prompt = _render_phase_prompt(
        phase.prompt, todo_id=todo_id, tick_id=tick_id, project_slug=project_slug,
    )

    result = _run_hermes_subprocess(
        prompt=prompt,
        tools=phase.tools,
        turns=phase.turns,
        timeout=phase.timeout,
        cwd=kw.get("project_dir"),
        on_pid=_record_child_pid,
    )

    if result["returncode"] != 0:
        raise RuntimeError(
            f"phase failed: rc={result['returncode']} "
            f"stdout={result['stdout'][:200]} "
            f"stderr={result['stderr'][:200]}"
        )

    if phase.terminal:
        todo_num = int(todo_id.removeprefix("TODO-"))
        from .state import ReadyForReview, State

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
        # Write through State so the file lands at the same path
        # `State.read_ready_for_review` (and merge.run_phase9) will look at.
        state = State(
            project=project_slug,
            lock_dir=sd / "pipeline_locks",
            checkpoint_dir=sd / "pipeline_checkpoints",
            ready_dir=sd / "ready_for_review",
        )
        state.write_ready_for_review(rec)

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

    Marker write must happen BEFORE any Hermes invocation so that the next
    pipeline-tick sees this TODO as in-flight even if we crash mid-phase.

    On failure, writes a `failed_at_phase_<phase_key>` outcome sidecar so the
    decision is not left perpetually `in_flight` after the marker is cleared.
    """
    sd = Path(state_dir)
    _write_marker(sd, todo_id=todo_id, tick_id=tick_id, phase_key=phase_key)
    try:
        result = _invoke_hermes(todo_id=todo_id, phase_key=phase_key, tick_id=tick_id, state_dir=sd, **kw)
    except Exception as e:
        # Record the failure outcome before the marker disappears. The
        # decision/store path is write-once; swallow FileExistsError so we
        # don't mask the original exception with a sidecar collision.
        try:
            from .decision import store as _decision_store
            _decision_store.append_outcome(
                sd, tick_id,
                outcome=f"failed_at_phase_{phase_key}",
                detail={"todo_id": todo_id, "error": str(e)[:500]},
            )
        except FileExistsError:
            pass
        except Exception as se:
            # Sidecar write failed (e.g., disk full, permission denied).
            # Log but don't mask the original phase failure.
            log.warning(
                "failed to write outcome sidecar for %s (original error: %s): %s",
                todo_id, e, se,
            )
        _delete_marker(sd, todo_id, tick_id=tick_id)
        raise
    _delete_marker(sd, todo_id, tick_id=tick_id)
    return result
