"""Pipeline execution contract — versioned TOML manifest read at tick start.

Declares which assignee and tool capabilities a project's phases require,
decoupled from the Hermes profile API (which doesn't support the
model/tools/skills flags this would otherwise need — see TODO-16).
"""
from __future__ import annotations

import atexit
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .phases import Phase, load_phases

CONTRACT_SCHEMA_VERSION = 3
CONTRACT_FILENAME = "pipeline.toml"
DEFAULT_CAPABILITIES: tuple[str, ...] = ("Read", "Write", "Edit", "Bash")
PROFILE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
# DEFAULT_PROFILE is the profile the authoring defaults (`tpo init`, the
# rendered default contract, `tpo test`) write and run.
# LEGACY_IMPLICIT_PROFILE is what a contract WITHOUT a `profile` key resolves
# to, preserving the pre-native-sdd behavior of existing projects.
DEFAULT_PROFILE = "native-sdd"
LEGACY_IMPLICIT_PROFILE = "gstack"


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
    # (e.g. "native-sdd", "gstack") — distinct from bundled_profile_dir()'s
    # unrelated "Hermes profile" (SOUL.md agent-identity) concept below.
    # Callers reconstructing an existing contract must pass
    # LEGACY_IMPLICIT_PROFILE explicitly rather than lean on this default.
    profile: str = DEFAULT_PROFILE
    review_assignee: str | None = None
    # False when the contract file had no `profile` key and `profile` was
    # filled from LEGACY_IMPLICIT_PROFILE.
    profile_declared: bool = True


def contract_path(project_state: Path) -> Path:
    """Return the path to a project's pipeline execution contract."""
    return project_state / CONTRACT_FILENAME


def default_contract() -> PipelineContract:
    return PipelineContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        assignee="default",
        capabilities=DEFAULT_CAPABILITIES,
    )


def _render_default_contract_toml(profile: str = DEFAULT_PROFILE) -> str:
    # Compute capabilities from the selected profile's phases.yaml so init
    # writes a contract that matches that profile's phase definitions, not
    # a stale hardcoded tuple or the wrong profile's requirements.
    from .phases import resolve_profile_phases_path

    caps = sorted(required_capabilities(load_phases(resolve_profile_phases_path(profile))))
    caps_toml = ", ".join(f'"{c}"' for c in caps)
    return (
        "# Pipeline execution contract — read at tick start.\n"
        "# See docs/tutorial-getting-started.md and `tpo doctor --help`.\n"
        f"schema_version = {CONTRACT_SCHEMA_VERSION}\n"
        'assignee = "default"\n'
        'review_assignee = "default"\n'
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
    review_assignee = contract.review_assignee or contract.assignee
    return (
        "# Pipeline execution contract — read at tick start.\n"
        "# See docs/tutorial-getting-started.md and `tpo doctor --help`.\n"
        f"schema_version = {contract.schema_version}\n"
        f'assignee = "{contract.assignee}"\n'
        f'review_assignee = "{review_assignee}"\n'
        f"capabilities = [{caps_toml}]\n"
        f'profile = "{contract.profile}"\n'
    )


def write_default_contract(project_state: Path, profile: str = DEFAULT_PROFILE) -> bool:
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
            f"no pipeline contract at {path} — run `tpo init <project>` to create one"
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
    if schema_version not in {2, CONTRACT_SCHEMA_VERSION}:
        raise ContractVersionMismatchError(
            f"{path} has schema_version={schema_version}, expected {CONTRACT_SCHEMA_VERSION} — "
            f"run `tpo init <project> --force` to regenerate, or edit it by hand"
        )

    assignee = data.get("assignee", "default")
    if not isinstance(assignee, str) or not assignee:
        raise ContractSchemaError(f"{path}: 'assignee' must be a non-empty string")
    review_assignee = data.get("review_assignee")
    if schema_version == 2:
        review_assignee = None
    elif not isinstance(review_assignee, str) or not review_assignee:
        raise ContractSchemaError(
            f"{path}: 'review_assignee' must be a non-empty string"
        )

    capabilities = data.get("capabilities", list(DEFAULT_CAPABILITIES))
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise ContractSchemaError(f"{path}: 'capabilities' must be a list of strings")

    profile = data.get("profile", LEGACY_IMPLICIT_PROFILE)
    if not isinstance(profile, str) or not PROFILE_NAME_RE.match(profile):
        raise ContractSchemaError(
            f"{path}: 'profile' must be a lowercase alphanumeric/hyphen string, "
            f"1-64 chars (got {profile!r})"
        )

    return PipelineContract(
        schema_version=schema_version,
        assignee=assignee,
        capabilities=tuple(capabilities),
        profile=profile,
        review_assignee=review_assignee,
        profile_declared="profile" in data,
    )


# Cache of (parts_key) → tempdir path, so zip-wheel extracts aren't
# repeated on every call and get cleaned up at exit.
_bundled_temp_cache: dict[str, Path] = {}


def _cleanup_bundled_temps():
    """Remove any temp directories created by _copy_traversable_to_tempdir."""
    for temp_dir in _bundled_temp_cache.values():
        shutil.rmtree(temp_dir, ignore_errors=True)
    _bundled_temp_cache.clear()


atexit.register(_cleanup_bundled_temps)


def _bundled_data_root():
    """Return the importlib.resources Traversable for hermes_pipeline.data.

    Isolated as its own function so tests can mock it to simulate a
    non-filesystem (zip-wheel) install without patching importlib itself.
    """
    from importlib.resources import files
    return files("hermes_pipeline.data")


def _copy_traversable_to_tempdir(traversable, key: str) -> Path:
    """Recursively copy a non-filesystem Traversable into a real tempdir.

    Results are cached by *key* so repeated calls (e.g. multiple features
    resolving against the same zip-wheel) reuse a single extraction.  All
    temp directories are cleaned up at process exit via an atexit handler.

    Used when importlib.resources yields a Traversable that isn't backed by
    a plain filesystem path (e.g. a zip-wheel install) — shutil.copytree and
    Path() operations need a real directory to work against.
    """
    if key in _bundled_temp_cache:
        return _bundled_temp_cache[key]

    dest_root = Path(tempfile.mkdtemp(prefix="hermes_pipeline_bundled_"))

    def _copy_node(node, dest: Path) -> None:
        if node.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            for child in node.iterdir():
                _copy_node(child, dest / child.name)
        else:
            dest.write_bytes(node.read_bytes())

    _copy_node(traversable, dest_root)
    _bundled_temp_cache[key] = dest_root
    return dest_root


def _resolve_bundled_dir(*parts: str) -> Path:
    """Resolve a directory under hermes_pipeline/data/ to a real filesystem Path.

    Resolves package-relative so it works whether running from a checkout
    or from an installed wheel. For zip-wheel installs, importlib.resources
    returns a Traversable that isn't a real filesystem path — in that case
    the directory is copied to a temp directory (cached per path) so callers
    can rely on a plain Path (shutil.copytree, Path.exists, etc.) unconditionally.
    """
    key = "/".join(parts)
    traversable = _bundled_data_root().joinpath(*parts)
    try:
        return Path(traversable)
    except (TypeError, NotImplementedError):
        return _copy_traversable_to_tempdir(traversable, key)


def bundled_profile_dir() -> Path:
    """Return the path to the directory containing the bundled pipeline SOUL.md.

    Resolves package-relative so it works whether running from a checkout
    or from an installed wheel.
    """
    return _resolve_bundled_dir("hermes-identity", "pipeline")


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
