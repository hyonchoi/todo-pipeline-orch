from __future__ import annotations

import datetime as _dt
import logging
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from .config import PromptClient

log = logging.getLogger(__name__)

CLIENT_VOCABULARY: Final[dict[PromptClient, dict[str, str]]] = {
    "claude": {"agent_product": "Claude Code", "skill_prefix": "/"},
    "codex": {"agent_product": "Codex", "skill_prefix": "$"},
}
_ALLOWED_PROMPT_FIELDS = frozenset(
    {"todo_id", "tick_id", "project_slug", "agent_product", "skill_prefix"}
)


class PhasePromptRenderError(ValueError):
    """A repository-owned phase prompt violates the strict template grammar."""


def _validate_prompt_template(template: str, source: str) -> None:
    formatter = string.Formatter()
    try:
        parsed = list(formatter.parse(template))
        # Formatter collapses both ``{field}`` and ``{field:}`` to an empty
        # format_spec. A marker makes the explicit empty construct observable
        # without disturbing escaped braces or literal colons.
        format_spec_probe = list(
            formatter.parse(template.replace(":}", ":__format_spec__}"))
        )
    except ValueError as exc:
        raise PhasePromptRenderError(
            f"{source}: malformed braces: {exc}"
        ) from exc
    for parsed_field, probed_field in zip(parsed, format_spec_probe, strict=True):
        _literal, field_name, format_spec, conversion = parsed_field
        _probed_literal, _probed_name, probed_format_spec, _probed_conversion = (
            probed_field
        )
        if field_name is None:
            continue
        if field_name == "" or field_name.isdecimal():
            raise PhasePromptRenderError(
                f"{source}: positional field {field_name!r} is not allowed"
            )
        if "." in field_name or "[" in field_name or "]" in field_name:
            raise PhasePromptRenderError(
                f"{source}: attribute/index traversal {field_name!r} is not allowed"
            )
        if field_name not in _ALLOWED_PROMPT_FIELDS:
            raise PhasePromptRenderError(
                f"{source}: unknown field {field_name!r}"
            )
        if conversion is not None:
            raise PhasePromptRenderError(
                f"{source}: conversion on {field_name!r} is not allowed"
            )
        if format_spec or probed_format_spec:
            kind = "nested replacement field" if "{" in format_spec else "format specification"
            raise PhasePromptRenderError(
                f"{source}: {kind} on {field_name!r} is not allowed"
            )


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

    profiles_root = files("hermes_pipeline").joinpath("data", "phase-profiles")
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
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

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
    template: str,
    *,
    todo_id: str,
    tick_id: str,
    project_slug: str,
    spec_path: str | None = None,
    reference_paths: list[str] | None = None,
    prompt_client: PromptClient = "claude",
    template_source: str | None = None,
) -> str:
    """Inject the pipeline context the phase prompt needs.

    A picked TODO must be visible to the LLM — otherwise a TODO-7 pick can
    silently produce work for whatever TODO the LLM latches onto next. We
    prepend a non-templated context header and ALSO support strict named
    substitution for phases that want to weave pipeline and client vocabulary
    into prose.

    `spec_path`/`reference_paths` are optional, pre-validated (existence +
    project_dir containment already checked by the caller) TODOS.md
    Spec:/Reference: values for the pipeline's first phase only. Omitted
    entirely when absent so prompt output for TODOs without these fields
    stays byte-identical to before this feature existed.
    """
    source = template_source or "<phase prompt>"
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
        vocabulary = CLIENT_VOCABULARY[prompt_client]
    except KeyError as exc:
        raise PhasePromptRenderError(
            f"{source}: prompt_client must be one of "
            f"{sorted(CLIENT_VOCABULARY)}, got {prompt_client!r}"
        ) from exc
    _validate_prompt_template(template, source)
    body = template.format(
        todo_id=todo_id,
        tick_id=tick_id,
        project_slug=project_slug,
        **vocabulary,
    )
    return header + body
