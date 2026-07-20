"""Pipeline execution contract — versioned TOML manifest read at tick start.

Declares which assignee and tool capabilities a project's phases require,
decoupled from the Hermes profile API (which doesn't support the
model/tools/skills flags this would otherwise need — see TODO-16).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .phases import Phase, load_phases

CONTRACT_SCHEMA_VERSION = 2
CONTRACT_FILENAME = "pipeline.toml"
DEFAULT_CAPABILITIES: tuple[str, ...] = ("Read", "Write", "Edit", "Bash")


class ContractError(Exception):
    """Base class for pipeline execution contract errors."""


class ContractMissingError(ContractError):
    """No pipeline.toml contract file exists for this project."""


class ContractSchemaError(ContractError):
    """The contract file is malformed or missing/misshapen required fields."""


class ContractVersionMismatchError(ContractError):
    """The contract's schema_version doesn't match CONTRACT_SCHEMA_VERSION."""


class CapabilityMismatchError(ContractError):
    """A phase requires a tool capability the contract doesn't grant."""


@dataclass(frozen=True)
class PipelineContract:
    schema_version: int
    assignee: str = "default"
    capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES
    # Which pipeline skill-set profile's phases.yaml this project runs
    # (e.g. "gstack", "agent-skills") — distinct from bundled_profile_dir()'s
    # unrelated "Hermes profile" (SOUL.md agent-identity) concept below.
    profile: str = "gstack"


def contract_path(project_state: Path) -> Path:
    """Return the path to a project's pipeline execution contract."""
    return project_state / CONTRACT_FILENAME


def default_contract() -> PipelineContract:
    return PipelineContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        assignee="default",
        capabilities=DEFAULT_CAPABILITIES,
    )


def _render_default_contract_toml(profile: str = "gstack") -> str:
    # Compute capabilities from the selected profile's phases.yaml so init
    # writes a contract that matches that profile's phase definitions, not
    # a stale hardcoded tuple or the wrong profile's requirements.
    from .phases import resolve_profile_phases_path

    caps = sorted(required_capabilities(load_phases(resolve_profile_phases_path(profile))))
    caps_toml = ", ".join(f'"{c}"' for c in caps)
    return (
        "# Pipeline execution contract — read at tick start.\n"
        "# See docs/tutorial-getting-started.md and `pipeline-watch doctor --help`.\n"
        f"schema_version = {CONTRACT_SCHEMA_VERSION}\n"
        'assignee = "default"\n'
        f"capabilities = [{caps_toml}]\n"
        f'profile = "{profile}"\n'
    )


def _render_contract_toml(contract: PipelineContract) -> str:
    """Render a PipelineContract to TOML text.

    Centralises TOML serialization so the output stays in sync with
    schema evolution — cli.py's --assignee patch path calls this instead
    of hand-rolling string templates.
    """
    caps_toml = ", ".join(f'"{c}"' for c in contract.capabilities)
    return (
        "# Pipeline execution contract — read at tick start.\n"
        "# See docs/tutorial-getting-started.md and `pipeline-watch doctor --help`.\n"
        f"schema_version = {contract.schema_version}\n"
        f'assignee = "{contract.assignee}"\n'
        f"capabilities = [{caps_toml}]\n"
        f'profile = "{contract.profile}"\n'
    )


def write_default_contract(project_state: Path, profile: str = "gstack") -> bool:
    """Write the default contract if one doesn't already exist.

    Returns:
        True if a new contract file was written, False if one already
        existed (idempotent no-op — the existing file is left untouched).
    """
    path = contract_path(project_state)
    if path.exists():
        return False
    project_state.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_default_contract_toml(profile))
    return True


def load_contract(project_state: Path) -> PipelineContract:
    """Load and validate a project's pipeline execution contract.

    Raises:
        ContractMissingError: No contract file exists.
        ContractSchemaError: The file is malformed or has invalid field types.
        ContractVersionMismatchError: schema_version != CONTRACT_SCHEMA_VERSION.
    """
    path = contract_path(project_state)
    if not path.is_file():
        raise ContractMissingError(
            f"no pipeline contract at {path} — run `pipeline-watch init <project>` to create one"
        )

    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ContractSchemaError(f"malformed TOML at {path}: {e}") from e

    if "schema_version" not in data:
        raise ContractSchemaError(f"{path} is missing required field 'schema_version'")
    schema_version = data["schema_version"]
    if not isinstance(schema_version, int):
        raise ContractSchemaError(f"{path}: 'schema_version' must be an integer")
    if schema_version != CONTRACT_SCHEMA_VERSION:
        raise ContractVersionMismatchError(
            f"{path} has schema_version={schema_version}, expected {CONTRACT_SCHEMA_VERSION} — "
            f"run `pipeline-watch init <project> --force` to regenerate, or edit it by hand"
        )

    assignee = data.get("assignee", "default")
    if not isinstance(assignee, str) or not assignee:
        raise ContractSchemaError(f"{path}: 'assignee' must be a non-empty string")

    capabilities = data.get("capabilities", list(DEFAULT_CAPABILITIES))
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise ContractSchemaError(f"{path}: 'capabilities' must be a list of strings")

    profile = data.get("profile", "gstack")
    if not isinstance(profile, str) or not profile:
        raise ContractSchemaError(f"{path}: 'profile' must be a non-empty string")

    return PipelineContract(
        schema_version=schema_version,
        assignee=assignee,
        capabilities=tuple(capabilities),
        profile=profile,
    )


def bundled_profile_dir() -> Path:
    """Return the path to the directory containing the bundled pipeline SOUL.md.

    Resolves package-relative so it works whether running from a checkout
    or from an installed wheel.

    Limitation: For zip-wheel installs, importlib.resources returns a
    Traversable that is not a real filesystem path, not a plain
    ``shutil.copy``-able path. In practice, hatchling + uv always produce
    filesystem installs, so this works.
    """
    from importlib.resources import files
    traversable = files("hermes_pipeline").joinpath("data", "profiles", "pipeline")
    return Path(traversable)


def required_capabilities(phases: list[Phase]) -> set[str]:
    """Union of tool names declared across all non-gate phases in phases.yaml."""
    caps: set[str] = set()
    for phase in phases:
        if phase.gate:
            continue
        caps.update(t.strip() for t in phase.tools.split(",") if t.strip())
    return caps


def missing_capabilities(contract: PipelineContract, phases: list[Phase]) -> set[str]:
    """Capabilities phases.yaml requires that the contract doesn't grant."""
    return required_capabilities(phases) - set(contract.capabilities)
