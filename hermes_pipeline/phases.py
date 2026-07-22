from __future__ import annotations
import datetime as _dt
import logging
from dataclasses import dataclass
from pathlib import Path
import yaml

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class Phase:
    phase_key: str
    name: str
    prompt: str = ""
    tools: str = ""
    turns: int = 0
    timeout: int = 1800
    terminal: bool = False
    gate: bool = False

def resolve_profile_phases_path(profile: str) -> Path:
    """Resolve the bundled phases.yaml for a pipeline skill-set profile.

    Raises:
        ContractSchemaError: No phases.yaml exists for `profile`; the
            message lists the available profile names.
    """
    from importlib.resources import files

    from .contract import ContractSchemaError

    profiles_root = files("hermes_pipeline").joinpath("data", "profiles")
    candidate = profiles_root.joinpath(profile, "phases.yaml")
    if not candidate.is_file():
        available = sorted(
            p.name for p in Path(profiles_root).iterdir()
            if p.is_dir() and (p / "phases.yaml").is_file()
        )
        raise ContractSchemaError(
            f"unknown profile '{profile}'. Available profiles: {', '.join(available)}. "
            f"Use --profile to select one at init, or edit 'profile' in .hermes/pipeline.toml."
        )
    return Path(candidate)


def load_phases(config_path: Path | str | None = None) -> list[Phase]:
    if config_path is None:
        config_path = resolve_profile_phases_path("gstack")
    config_path = Path(config_path)
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return [Phase(**p) for p in data["phases"]]


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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

def _render_phase_prompt(
    template: str, *, todo_id: str, tick_id: str, project_slug: str,
    spec_path: str | None = None, reference_paths: list[str] | None = None,
) -> str:
    """Inject the pipeline context the phase prompt needs.

    A picked TODO must be visible to the LLM — otherwise a TODO-7 pick can
    silently produce work for whatever TODO the LLM latches onto next. We
    prepend a non-templated context header and ALSO support `{todo_id}` /
    `{tick_id}` / `{project_slug}` substitution for phases that want to
    weave the values into prose. `.format()` with named-only fields is safe
    here because every prompt in configs/phases.yaml is repo-owned.

    `spec_path`/`reference_paths` are optional, pre-validated (existence +
    project_dir containment already checked by the caller) TODOS.md
    Spec:/Reference: values for the pipeline's first phase only. Omitted
    entirely when absent so prompt output for TODOs without these fields
    stays byte-identical to before this feature existed.
    """
    header = (
        f"Pipeline context:\n"
        f"- todo_id: {todo_id}\n"
        f"- tick_id: {tick_id}\n"
        f"- project_slug: {project_slug}\n"
        f"Work on {todo_id} ONLY. Do not pick a different TODO.\n\n"
    )
    spec_reference_block = ""
    if spec_path:
        spec_reference_block += f"Spec (authoritative): {spec_path}\n"
    if reference_paths:
        spec_reference_block += f"Reference material: {', '.join(reference_paths)}\n"
    if spec_reference_block:
        header += spec_reference_block + "\n"
    try:
        body = template.format(todo_id=todo_id, tick_id=tick_id, project_slug=project_slug)
    except (KeyError, IndexError):
        # Template uses a `{name}` we don't supply — fall back to verbatim
        # body. The header still scopes the run to this TODO.
        body = template
    return header + body
