from __future__ import annotations

import datetime as _dt
import logging
import string
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import yaml

from .config import PromptClient

log = logging.getLogger(__name__)

CLIENT_VOCABULARY: Final[dict[PromptClient, dict[str, str]]] = {
    "claude": {
        "agent_product": "Claude Code",
        "skill_prefix": "/",
        "superpowers_skill_prefix": "/",
    },
    "codex": {
        "agent_product": "Codex",
        "skill_prefix": "$",
        "superpowers_skill_prefix": "$superpowers:",
    },
}
_ALLOWED_PROMPT_FIELDS = frozenset(
    {
        "todo_id",
        "tick_id",
        "project_slug",
        "plan_path",
        "agent_product",
        "skill_prefix",
        "superpowers_skill_prefix",
    }
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
    kind: Literal["worker", "controller_gate", "human_gate"] | None = None
    compile_plan_tasks: bool = False


@dataclass(frozen=True)
class PhaseProfile:
    phases: tuple[Phase, ...]
    requires_plan: bool = False
    deprecated: bool = False


@dataclass(frozen=True)
class ClientPrerequisite:
    discovery_root: str | None
    invocation: str | None


@dataclass(frozen=True)
class SkillPrerequisite:
    skill_id: str
    distribution_owner: str
    support: Literal["Conditional", "Unverified"]
    clients: dict[PromptClient, ClientPrerequisite]


@dataclass(frozen=True)
class ProfilePrerequisites:
    schema_version: int
    profile: str
    skills: tuple[SkillPrerequisite, ...]

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


def load_profile_prerequisites(profile: str) -> ProfilePrerequisites:
    phases_path = resolve_profile_phases_path(profile)
    path = phases_path.with_name("prerequisites.yaml")

    def invalid(field: str, detail: str) -> ValueError:
        return ValueError(f"{path}: invalid {field}: {detail}")

    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise invalid("metadata", str(exc)) from exc
    if not isinstance(raw, dict):
        raise invalid("metadata", "expected a mapping")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise invalid("schema_version", "expected 1")
    if raw.get("profile") != profile:
        raise invalid("profile", f"expected {profile!r}")

    raw_skills = raw.get("skills")
    if not isinstance(raw_skills, list):
        raise invalid("skills", "expected a list")

    skills: list[SkillPrerequisite] = []
    seen_skill_ids: set[str] = set()
    expected_clients = set(CLIENT_VOCABULARY)
    expected_client_names = " and ".join(repr(name) for name in CLIENT_VOCABULARY)
    expected_client_fields = {"discovery_root", "invocation"}
    for index, raw_skill in enumerate(raw_skills):
        prefix = f"skills[{index}]"
        if not isinstance(raw_skill, dict):
            raise invalid(prefix, "expected a mapping")

        skill_id = raw_skill.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise invalid(f"{prefix}.skill_id", "expected a non-empty string")
        if skill_id in seen_skill_ids:
            raise invalid(f"{prefix}.skill_id", f"duplicate value {skill_id!r}")
        seen_skill_ids.add(skill_id)

        distribution_owner = raw_skill.get("distribution_owner")
        if not isinstance(distribution_owner, str) or not distribution_owner.strip():
            raise invalid(
                f"{prefix}.distribution_owner", "expected a non-empty string"
            )

        support = raw_skill.get("support")
        if support not in ("Conditional", "Unverified"):
            raise invalid(
                f"{prefix}.support", "expected 'Conditional' or 'Unverified'"
            )

        raw_clients = raw_skill.get("clients")
        if not isinstance(raw_clients, dict) or set(raw_clients) != expected_clients:
            raise invalid(
                f"{prefix}.clients", f"expected exactly {expected_client_names}"
            )

        clients: dict[PromptClient, ClientPrerequisite] = {}
        for client in CLIENT_VOCABULARY:
            raw_client = raw_clients[client]
            client_prefix = f"{prefix}.clients.{client}"
            if (
                not isinstance(raw_client, dict)
                or set(raw_client) != expected_client_fields
            ):
                raise invalid(
                    client_prefix,
                    "expected exactly 'discovery_root' and 'invocation'",
                )
            discovery_root = raw_client["discovery_root"]
            invocation = raw_client["invocation"]
            for field, value in (
                ("discovery_root", discovery_root),
                ("invocation", invocation),
            ):
                field_path = f"{client_prefix}.{field}"
                if support == "Conditional":
                    if not isinstance(value, str) or not value.strip():
                        raise invalid(field_path, "must be non-null for Conditional")
                elif value is not None:
                    raise invalid(field_path, "must be null for Unverified")
            clients[client] = ClientPrerequisite(
                discovery_root=discovery_root,
                invocation=invocation,
            )

        skills.append(
            SkillPrerequisite(
                skill_id=skill_id,
                distribution_owner=distribution_owner,
                support=support,
                clients=clients,
            )
        )

    return ProfilePrerequisites(
        schema_version=raw["schema_version"],
        profile=raw["profile"],
        skills=tuple(skills),
    )


def load_phase_profile(config_path: Path | str | None = None) -> PhaseProfile:
    """Load a phase profile from ``config_path``.

    With no argument, falls back to the bundled legacy implicit profile
    (``contract.LEGACY_IMPLICIT_PROFILE``) — the one a contract without a
    ``profile`` key resolves to.
    """
    if config_path is None:
        # Lazy import: contract.py imports this module at top level.
        from .contract import LEGACY_IMPLICIT_PROFILE

        config_path = resolve_profile_phases_path(LEGACY_IMPLICIT_PROFILE)
    config_path = Path(config_path)
    with open(config_path) as f:
        data = yaml.safe_load(f)
    requires_plan = data.get("requires_plan", False) if isinstance(data, dict) else False
    if type(requires_plan) is not bool:
        raise ValueError(f"{config_path}: requires_plan must be a boolean")
    deprecated = data.get("deprecated", False) if isinstance(data, dict) else False
    if type(deprecated) is not bool:
        raise ValueError(f"{config_path}: deprecated must be a boolean")
    raw_phases = data.get("phases") if isinstance(data, dict) else None
    if not isinstance(raw_phases, list) or not raw_phases:
        raise ValueError(f"{config_path}: phases must contain at least one phase")
    phases: list[Phase] = []
    for index, raw_phase in enumerate(raw_phases):
        phase = Phase(**raw_phase)
        if type(phase.timeout) is not int or phase.timeout <= 0:
            source = raw_phase.get("phase_key", f"index {index}")
            raise ValueError(
                f"{config_path}:{source}: timeout must be a positive integer"
            )
        phases.append(phase)
    return PhaseProfile(
        phases=tuple(phases), requires_plan=requires_plan, deprecated=deprecated
    )


def load_phases(config_path: Path | str | None = None) -> list[Phase]:
    """Load the phase list while preserving the historical public API.

    With no argument this inherits :func:`load_phase_profile`'s fallback to the
    bundled ``contract.LEGACY_IMPLICIT_PROFILE`` — the profile a contract
    without a ``profile`` key resolves to, not ``contract.DEFAULT_PROFILE``.
    """
    return list(load_phase_profile(config_path).phases)


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
    plan_path: str | None = None,
    plan_hash: str | None = None,
    spec_path: str | None = None,
    reference_paths: list[str] | None = None,
    prompt_client: PromptClient = "claude",
    template_source: str | None = None,
    decisions: Mapping[str, str] | None = None,
) -> str:
    """Inject the pipeline context the phase prompt needs.

    A picked TODO must be visible to the LLM — otherwise a TODO-7 pick can
    silently produce work for whatever TODO the LLM latches onto next. We
    prepend a non-templated context header and ALSO support strict named
    substitution for phases that want to weave pipeline and client vocabulary
    into prose.

    `plan_path`/`spec_path`/`reference_paths` are optional, pre-validated (existence +
    project_dir containment already checked by the caller) values taken from the
    selected issue's Plan/Spec/Reference sections. Omitted entirely when absent
    so prompt output for TODOs without these fields stays byte-identical to
    before this feature existed.

    `decisions` (Priority, Effort, Security Review, ...) are the issue's decision
    values; workers read them from the rendered ``Decisions:`` block because no
    phase may consult a TODOS.md entry. Values are sanitized before rendering.
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
    if plan_path:
        spec_reference_block += f"Plan (execution authority): {plan_path}\n"
        if plan_hash:
            spec_reference_block += (
                f"Plan SHA-256: {plan_hash}\n"
                "Before using the Plan, verify its SHA-256 matches exactly; fail closed on drift.\n"
            )
    if spec_path:
        spec_reference_block += f"Spec (authoritative): {spec_path}\n"
    if reference_paths:
        spec_reference_block += f"Reference material: {', '.join(reference_paths)}\n"
    if spec_reference_block:
        header += spec_reference_block + "\n"
    if decisions:
        from .result_contract import sanitize_result_text

        header += "Decisions:\n" + "".join(
            f"- {sanitize_result_text(key, maximum=80)}: "
            f"{sanitize_result_text(value, maximum=200)}\n"
            for key, value in decisions.items()
        ) + "\n"
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
        plan_path=plan_path or "",
        **vocabulary,
    )
    return header + body
