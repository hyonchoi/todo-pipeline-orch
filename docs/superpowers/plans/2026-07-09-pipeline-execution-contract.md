# Pipeline Execution Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "register a dedicated Hermes profile" design for TODO-15 with a versioned pipeline execution contract — a TOML file, `.hermes/pipeline.toml`, that declares the assignee and tool capabilities a project's phases require — plus `pipeline-watch init`/`pipeline-watch doctor` subcommands and wiring so the tick flow reads the assignee from the contract and fails closed on capability drift.

**Architecture:** A new `hermes_pipeline/contract.py` module owns the contract dataclass, TOML load/write, and capability cross-validation against `configs/phases.yaml`. `pipeline-watch init <project>` writes the default contract (idempotent — no-op if one exists, unless `--force`). `pipeline-watch doctor <project>` loads and validates a project's contract, exiting 0/1/2 for clean/drift/missing-or-invalid. `_tick_project` in `cli.py` loads the contract at tick start (falling back to defaults if none exists, for backward compatibility with un-initialized projects), fails the project's tick closed if the contract's `schema_version` is stale or its `capabilities` don't cover what `phases.yaml` requires, and passes `contract.assignee` into the existing `register_todo_phases(assignee=...)` parameter (which already accepts it but is never called with anything but the "default" default today).

**Tech Stack:** Python 3.12+, stdlib `tomllib` for parsing (matches `hermes_pipeline/config.py`), `dataclasses`, `pytest` + `pytest-mock` for tests, `uv run pytest` to execute.

## Global Constraints

- Contract file lives at `<project_dir>/.hermes/pipeline.toml` — one contract per managed project, same directory tier as the existing `.hermes/config.toml` and `.hermes/project.toml`.
- Hermes profile registration (the original TODO-15 shape: `hermes profile create --model/--tools/--skills`) is **out of scope** — `hermes profile create` does not support those flags today. That work is deferred to TODO-16. This plan implements the contract only.
- Fail closed on drift: a contract that exists but has a stale `schema_version`, is malformed, or under-declares capabilities blocks that project's tick with an actionable error. A **missing** contract (project never ran `init`) falls back to defaults silently — existing projects must keep working without any migration step.
- No new Hermes subprocess calls — contract validation is pure local TOML/dataclass logic, testable without mocking `subprocess`.
- Follow existing conventions: `tomllib` + `dataclass(frozen=True)` + `ValueError`-with-path-embedded parsing errors (see `hermes_pipeline/config.py::load_toml_overlay`), and the `_resolve_project_dir` / exit-code-2-on-bad-project idiom already used by `recover-counter` and `tick` in `cli.py`.

---

### Task 1: Contract schema module

**Files:**
- Create: `hermes_pipeline/contract.py`
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: `hermes_pipeline.phases.Phase` (fields used: `phase_key: str`, `tools: str`, `gate: bool`).
- Produces (used by Tasks 2, 3, 4):
  - `CONTRACT_SCHEMA_VERSION: int = 1`
  - `CONTRACT_FILENAME: str = "pipeline.toml"`
  - `class PipelineContract` — frozen dataclass with `schema_version: int`, `assignee: str = "default"`, `capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES`
  - `class ContractError(Exception)`, `class ContractMissingError(ContractError)`, `class ContractSchemaError(ContractError)`, `class ContractVersionMismatchError(ContractError)`, `class CapabilityMismatchError(ContractError)`
  - `contract_path(project_state: Path) -> Path`
  - `default_contract() -> PipelineContract`
  - `write_default_contract(project_state: Path) -> bool` (`True` if written, `False` if a contract already existed)
  - `load_contract(project_state: Path) -> PipelineContract` (raises `ContractMissingError` / `ContractSchemaError` / `ContractVersionMismatchError`)
  - `required_capabilities(phases: list[Phase]) -> set[str]`
  - `missing_capabilities(contract: PipelineContract, phases: list[Phase]) -> set[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_contract.py`:

```python
"""Tests for contract.py — pipeline execution contract schema and validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pipeline.contract import (
    CONTRACT_SCHEMA_VERSION,
    ContractMissingError,
    ContractSchemaError,
    ContractVersionMismatchError,
    PipelineContract,
    contract_path,
    default_contract,
    load_contract,
    missing_capabilities,
    required_capabilities,
    write_default_contract,
)
from hermes_pipeline.phases import Phase


def test_default_contract_has_expected_defaults():
    contract = default_contract()
    assert contract.schema_version == CONTRACT_SCHEMA_VERSION
    assert contract.assignee == "default"
    assert set(contract.capabilities) == {"Read", "Write", "Edit", "Bash"}


def test_write_default_contract_creates_file(tmp_path):
    project_state = tmp_path / ".hermes"
    written = write_default_contract(project_state)
    assert written is True
    path = contract_path(project_state)
    assert path.is_file()
    assert "schema_version = 1" in path.read_text()


def test_write_default_contract_idempotent(tmp_path):
    project_state = tmp_path / ".hermes"
    write_default_contract(project_state)
    path = contract_path(project_state)
    path.write_text('schema_version = 1\nassignee = "custom"\ncapabilities = ["Read"]\n')

    written_again = write_default_contract(project_state)

    assert written_again is False
    assert 'assignee = "custom"' in path.read_text()


def test_load_contract_missing_raises(tmp_path):
    project_state = tmp_path / ".hermes"
    with pytest.raises(ContractMissingError, match="pipeline-watch init"):
        load_contract(project_state)


def test_load_contract_valid(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 1\nassignee = "reviewer-bot"\ncapabilities = ["Read", "Bash"]\n'
    )

    contract = load_contract(project_state)

    assert contract.schema_version == 1
    assert contract.assignee == "reviewer-bot"
    assert contract.capabilities == ("Read", "Bash")


def test_load_contract_malformed_toml_raises_schema_error(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text("schema_version = ")

    with pytest.raises(ContractSchemaError):
        load_contract(project_state)


def test_load_contract_missing_schema_version_raises(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text('assignee = "default"\n')

    with pytest.raises(ContractSchemaError, match="schema_version"):
        load_contract(project_state)


def test_load_contract_version_mismatch_raises(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text("schema_version = 99\n")

    with pytest.raises(ContractVersionMismatchError, match="99"):
        load_contract(project_state)


def test_load_contract_invalid_capabilities_type_raises(tmp_path):
    project_state = tmp_path / ".hermes"
    project_state.mkdir(parents=True)
    (project_state / "pipeline.toml").write_text(
        'schema_version = 1\ncapabilities = "Read"\n'
    )

    with pytest.raises(ContractSchemaError, match="capabilities"):
        load_contract(project_state)


def test_required_capabilities_excludes_gate_phases():
    phases = [
        Phase(phase_key="p1", name="P1", tools="Read,Write"),
        Phase(phase_key="gate", name="Gate", gate=True, tools="Edit"),
        Phase(phase_key="p2", name="P2", tools="Bash"),
    ]
    assert required_capabilities(phases) == {"Read", "Write", "Bash"}


def test_missing_capabilities_detects_gap():
    contract = PipelineContract(schema_version=1, capabilities=("Read",))
    phases = [Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")]
    assert missing_capabilities(contract, phases) == {"Write", "Bash"}


def test_missing_capabilities_empty_when_satisfied():
    contract = PipelineContract(schema_version=1, capabilities=("Read", "Write", "Bash"))
    phases = [Phase(phase_key="p1", name="P1", tools="Read,Write")]
    assert missing_capabilities(contract, phases) == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_contract.py -v`
Expected: `ModuleNotFoundError: No module named 'hermes_pipeline.contract'` (all tests error, not just fail).

- [ ] **Step 3: Implement `hermes_pipeline/contract.py`**

```python
"""Pipeline execution contract — versioned TOML manifest read at tick start.

Declares which assignee and tool capabilities a project's phases require,
decoupled from the Hermes profile API (which doesn't support the
model/tools/skills flags this would otherwise need — see TODO-16).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .phases import Phase

CONTRACT_SCHEMA_VERSION = 1
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


def contract_path(project_state: Path) -> Path:
    """Return the path to a project's pipeline execution contract."""
    return project_state / CONTRACT_FILENAME


def default_contract() -> PipelineContract:
    return PipelineContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        assignee="default",
        capabilities=DEFAULT_CAPABILITIES,
    )


def _render_default_contract_toml() -> str:
    caps = ", ".join(f'"{c}"' for c in DEFAULT_CAPABILITIES)
    return (
        "# Pipeline execution contract — read at tick start.\n"
        "# See docs/tutorial-getting-started.md and `pipeline-watch doctor --help`.\n"
        f"schema_version = {CONTRACT_SCHEMA_VERSION}\n"
        'assignee = "default"\n'
        f"capabilities = [{caps}]\n"
    )


def write_default_contract(project_state: Path) -> bool:
    """Write the default contract if one doesn't already exist.

    Returns:
        True if a new contract file was written, False if one already
        existed (idempotent no-op — the existing file is left untouched).
    """
    path = contract_path(project_state)
    if path.exists():
        return False
    project_state.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_default_contract_toml())
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

    return PipelineContract(
        schema_version=schema_version,
        assignee=assignee,
        capabilities=tuple(capabilities),
    )


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/contract.py tests/test_contract.py
git commit -m "feat: add pipeline execution contract schema"
```

---

### Task 2: `pipeline-watch init` subcommand

**Files:**
- Modify: `hermes_pipeline/cli.py` (add `_cmd_init`, wire into `build_parser()`)
- Test: `tests/test_cli_contract.py` (new file)

**Interfaces:**
- Consumes: `contract_path`, `write_default_contract` from Task 1; `_resolve_project_dir(config, slug) -> Optional[Path]` (existing, `hermes_pipeline/cli.py:45`); `_get_project_state_dir(project_dir) -> Path` (existing, `hermes_pipeline/state_migration.py:27`).
- Produces: `_cmd_init(args, config: Config) -> int` — exit 0 on success (written or already-exists no-op), 1 on write failure, 2 on invalid/unknown project.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_contract.py`:

```python
"""Tests for the init/doctor subcommands (pipeline execution contract)."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pipeline.cli import build_parser, _cmd_init
from hermes_pipeline.config import Config


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _create_project(projects_dir, name):
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "TODOS.md").write_text("# TODOS\n")
    return project_dir


class TestBuildParserInit:
    def test_init_help(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["init", "--help"])

    def test_init_parses_project_and_force(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo", "--force"])
        assert args.command == "init"
        assert args.project == "demo"
        assert args.force is True

    def test_init_force_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo"])
        assert args.force is False


class TestCmdInit:
    def test_init_unknown_project_returns_2(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = Config(projects_dir=projects_dir)
        result = _cmd_init(FakeArgs(project="nope", force=False), config)
        assert result == 2

    def test_init_writes_default_contract(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert contract.is_file()
        assert "Wrote pipeline execution contract" in capsys.readouterr().out

    def test_init_idempotent_without_force(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        _cmd_init(FakeArgs(project="demo", force=False), config)
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        contract.write_text('schema_version = 1\nassignee = "custom"\n')

        result = _cmd_init(FakeArgs(project="demo", force=False), config)

        assert result == 0
        assert "already exists" in capsys.readouterr().out
        assert 'assignee = "custom"' in contract.read_text()

    def test_init_force_overwrites(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        _cmd_init(FakeArgs(project="demo", force=False), config)
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        contract.write_text('schema_version = 1\nassignee = "custom"\n')

        result = _cmd_init(FakeArgs(project="demo", force=True), config)

        assert result == 0
        assert 'assignee = "custom"' not in contract.read_text()
        assert "schema_version = 1" in contract.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_contract.py -v`
Expected: FAIL with `ImportError: cannot import name '_cmd_init'` and `AttributeError`/`SystemExit` mismatches from `build_parser` (no `init` subcommand registered yet).

- [ ] **Step 3: Add `_cmd_init` and wire the `init` subparser in `hermes_pipeline/cli.py`**

Add this function near `_cmd_recover_counter` (after it, using `mcp__serena__insert_after_symbol` or an editor's "insert after" on `_cmd_recover_counter`):

```python
def _cmd_init(args, config: Config) -> int:
    """Handle 'init' subcommand — write the default pipeline execution contract."""
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    from .state_migration import _get_project_state_dir
    from .contract import contract_path, write_default_contract

    project_state = _get_project_state_dir(project_dir)
    path = contract_path(project_state)

    try:
        if args.force and path.exists():
            path.unlink()
        written = write_default_contract(project_state)
    except OSError as e:
        log.error("failed to write pipeline contract at %s: %s", path, e)
        return 1

    if written:
        print(f"Wrote pipeline execution contract: {path}")
    else:
        print(f"Pipeline execution contract already exists: {path} (use --force to regenerate)")
    return 0
```

Then in `build_parser()` (`hermes_pipeline/cli.py:394-498`), add the `init` subparser right after the `recover-counter` subparser block and before `return parser`:

```python
    # init: Write the default pipeline execution contract
    init_parser = subparsers.add_parser(
        "init",
        help="Write the default pipeline execution contract for a project",
    )
    init_parser.add_argument("project", help="Project name")
    init_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing contract with the current default",
    )
    init_parser.set_defaults(func=_cmd_init)

    return parser
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_contract.py -v`
Expected: all `TestBuildParserInit` and `TestCmdInit` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_cli_contract.py
git commit -m "feat: add pipeline-watch init subcommand"
```

---

### Task 3: `pipeline-watch doctor` subcommand

**Files:**
- Modify: `hermes_pipeline/cli.py` (add `_cmd_doctor`, wire into `build_parser()`)
- Modify: `tests/test_cli_contract.py` (append doctor tests)

**Interfaces:**
- Consumes: `load_contract`, `missing_capabilities`, `contract_path`, `ContractMissingError`, `ContractSchemaError`, `ContractVersionMismatchError` from Task 1; `load_phases()` (existing, already imported at module scope in `cli.py` via `from .phases import load_phases`).
- Produces: `_cmd_doctor(args, config: Config) -> int` — exit 0 clean, 1 drift (capability mismatch), 2 missing/invalid contract or unknown project.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_contract.py`:

```python
from hermes_pipeline.cli import _cmd_doctor
from hermes_pipeline.phases import Phase


class TestBuildParserDoctor:
    def test_doctor_help(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["doctor", "--help"])

    def test_doctor_parses_project(self):
        parser = build_parser()
        args = parser.parse_args(["doctor", "demo"])
        assert args.command == "doctor"
        assert args.project == "demo"


class TestCmdDoctor:
    def test_doctor_unknown_project_returns_2(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = Config(projects_dir=projects_dir)
        result = _cmd_doctor(FakeArgs(project="nope"), config)
        assert result == 2

    def test_doctor_missing_contract_returns_2(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "pipeline-watch init" in capsys.readouterr().out

    def test_doctor_invalid_contract_returns_2(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text("schema_version = 99\n")
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 2
        assert "INVALID" in capsys.readouterr().out

    def test_doctor_clean_returns_0(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\ncapabilities = ["Read", "Write"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write")],
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 0
        assert "OK" in capsys.readouterr().out

    def test_doctor_drift_returns_1(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\ncapabilities = ["Read"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"), config)

        assert result == 1
        out = capsys.readouterr().out
        assert "DRIFT" in out
        assert "Write" in out and "Bash" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_contract.py -v -k Doctor`
Expected: FAIL with `ImportError: cannot import name '_cmd_doctor'`.

- [ ] **Step 3: Add `_cmd_doctor` and wire the `doctor` subparser in `hermes_pipeline/cli.py`**

Add this function directly after `_cmd_init`:

```python
def _cmd_doctor(args, config: Config) -> int:
    """Handle 'doctor' subcommand — verify the pipeline execution contract.

    Exit codes: 0 clean, 1 drift (capability mismatch), 2 missing/invalid
    contract or unknown project.
    """
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    from .state_migration import _get_project_state_dir
    from .contract import (
        ContractMissingError,
        ContractSchemaError,
        ContractVersionMismatchError,
        contract_path,
        load_contract,
        missing_capabilities,
    )

    project_state = _get_project_state_dir(project_dir)

    try:
        contract = load_contract(project_state)
    except ContractMissingError as e:
        print(f"MISSING: {e}")
        return 2
    except (ContractSchemaError, ContractVersionMismatchError) as e:
        print(f"INVALID: {e}")
        return 2

    phases = load_phases()
    missing = missing_capabilities(contract, phases)
    if missing:
        print(
            f"DRIFT: contract capabilities {sorted(contract.capabilities)} at "
            f"{contract_path(project_state)} are missing {sorted(missing)} "
            f"required by configs/phases.yaml — edit the contract to add them"
        )
        return 1

    print(
        f"OK: schema_version={contract.schema_version} assignee={contract.assignee} "
        f"capabilities={sorted(contract.capabilities)}"
    )
    return 0
```

Then in `build_parser()`, add the `doctor` subparser right after the `init` subparser (still before `return parser`):

```python
    # doctor: Verify the pipeline execution contract
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Verify a project's pipeline execution contract against phases.yaml",
    )
    doctor_parser.add_argument("project", help="Project name")
    doctor_parser.set_defaults(func=_cmd_doctor)

    return parser
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_contract.py -v`
Expected: all tests PASS (both init and doctor classes).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_cli_contract.py
git commit -m "feat: add pipeline-watch doctor subcommand"
```

---

### Task 4: Wire the contract into the tick flow

**Files:**
- Modify: `hermes_pipeline/cli.py:950-1166` (`_tick_project`)
- Test: `tests/test_tick_contract.py` (new file)

**Interfaces:**
- Consumes: `load_contract`, `default_contract`, `missing_capabilities`, `contract_path`, `ContractMissingError`, `ContractSchemaError`, `ContractVersionMismatchError`, `CapabilityMismatchError` from Task 1; `load_phases` (already imported at `cli.py` module scope); `register_todo_phases(..., assignee: str = "default")` (existing, `hermes_pipeline/kanban_tasks.py:65`).
- Produces: `_tick_project` now raises `CapabilityMismatchError` (a subclass of the existing broad `Exception` already caught per-project in `_cmd_tick`) when a project's contract under-declares capabilities, and passes `assignee=contract.assignee` to `register_todo_phases`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tick_contract.py`:

```python
"""Tests for the pipeline execution contract wired into the tick flow."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from hermes_pipeline.cli import _cmd_tick
from hermes_pipeline.config import Config


def _make_decision(picked):
    decision = MagicMock()
    decision.picked = picked
    decision.rationale = "test"
    decision.candidates_considered = []
    return decision


def _create_project(projects_dir, name):
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-10: test\n")
    return project_dir


class FakeArgs:
    def __init__(self, **kwargs):
        kwargs.setdefault("project", None)
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestTickContractAssignee:
    def test_tick_uses_contract_assignee(self, tmp_path, mocker):
        """register_todo_phases is called with the contract's assignee."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch("hermes_pipeline.cli.register_todo_phases", return_value=["t_1"])

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\nassignee = "reviewer-bot"\ncapabilities = ["Read", "Write", "Edit", "Bash"]\n'
        )

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["assignee"] == "reviewer-bot"

    def test_tick_no_contract_falls_back_to_default_assignee(self, tmp_path, mocker):
        """No pipeline.toml -> falls back to assignee='default', doesn't block."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch("hermes_pipeline.cli.register_todo_phases", return_value=["t_1"])

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        mock_register.assert_called_once()
        assert mock_register.call_args.kwargs["assignee"] == "default"

    def test_tick_capability_mismatch_skips_project_not_whole_scan(self, tmp_path, mocker):
        """A project with a capability-deficient contract is skipped, scan continues."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch("hermes_pipeline.cli.register_todo_phases")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\ncapabilities = ["Read"]\n'
        )

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0  # scan-level result: per-project errors don't abort the scan
        mock_register.assert_not_called()

    def test_tick_stale_contract_version_skips_project(self, tmp_path, mocker):
        """A contract with a stale schema_version fails closed for that project."""
        mocker.patch("hermes_pipeline.cli.run_selection", return_value=_make_decision("TODO-10"))
        mock_register = mocker.patch("hermes_pipeline.cli.register_todo_phases")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text("schema_version = 99\n")

        config = Config(projects_dir=projects_dir, state_dir=tmp_path / "state")
        result = _cmd_tick(FakeArgs(), config)

        assert result == 0
        mock_register.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tick_contract.py -v`
Expected: `test_tick_uses_contract_assignee` and `test_tick_stale_contract_version_skips_project` FAIL — `register_todo_phases` is called without an `assignee` kwarg override (always `"default"`), and a stale-version contract is never even read today so it doesn't block anything.

- [ ] **Step 3: Wire the contract into `_tick_project` in `hermes_pipeline/cli.py`**

`_tick_project` currently starts (right after its docstring, at what is today line 984):

```python
    from .project_config import _resolve_slack_channel

    # Resolve per-project Slack channel
    slack_channel = _resolve_slack_channel(project_dir, env_channel=config.slack_channel, toml_data=project_toml)
```

Change it to load and validate the contract first:

```python
    from .contract import (
        CapabilityMismatchError,
        ContractMissingError,
        ContractSchemaError,
        ContractVersionMismatchError,
        contract_path,
        default_contract,
        load_contract,
        missing_capabilities,
    )

    try:
        contract = load_contract(project_state)
    except ContractMissingError:
        contract = default_contract()
    except (ContractSchemaError, ContractVersionMismatchError) as e:
        log.error(
            "project %s: pipeline contract invalid: %s — run `pipeline-watch doctor %s` for details",
            project_slug, e, project_slug,
        )
        raise

    missing = missing_capabilities(contract, load_phases())
    if missing:
        log.error(
            "project %s: pipeline contract at %s is missing capabilities %s required by "
            "phases.yaml — edit the contract to add them, or run `pipeline-watch doctor %s` for details",
            project_slug, contract_path(project_state), sorted(missing), project_slug,
        )
        raise CapabilityMismatchError(f"contract missing capabilities: {sorted(missing)}")

    from .project_config import _resolve_slack_channel

    # Resolve per-project Slack channel
    slack_channel = _resolve_slack_channel(project_dir, env_channel=config.slack_channel, toml_data=project_toml)
```

Then further down, the existing Step 5 kanban-registration call:

```python
        task_ids = register_todo_phases(
            todo_id=picked,
            tick_id=tick_id,
            board_slug=project_slug,
            project_dir=project_dir,
        )
```

becomes:

```python
        task_ids = register_todo_phases(
            todo_id=picked,
            tick_id=tick_id,
            board_slug=project_slug,
            project_dir=project_dir,
            assignee=contract.assignee,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tick_contract.py -v`
Expected: all 4 tests PASS.

Run the full suite to confirm no regressions from touching `_tick_project`:

Run: `uv run pytest tests/test_tick_subcommand.py tests/test_tick_subcommand_edge.py tests/test_cli.py -v`
Expected: all PASS (these exercise `_tick_project` without a `pipeline.toml`, so they exercise the `ContractMissingError` → `default_contract()` fallback path).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_tick_contract.py
git commit -m "feat: wire pipeline execution contract into tick assignee and capability checks"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md:107-117` (CLI Commands section) and `README.md:153-178` (Configuration section)
- Modify: `docs/tutorial-getting-started.md:81-105` (Step 3)

**Interfaces:**
- Consumes: nothing new — this task only documents Tasks 1-4's CLI surface.
- Produces: nothing consumed by later tasks (this is the last task).

- [ ] **Step 1: Add `init`/`doctor` to the README CLI Commands section**

In `README.md`, immediately after the existing `recover-counter` block (currently ends at line 110 with the closing ` ``` `) and before the `Global flags` block, insert:

```markdown

Write the default pipeline execution contract for a project (idempotent — run again with `--force` to regenerate after editing `configs/phases.yaml`):
```bash
uv run pipeline-watch init <project>
uv run pipeline-watch init <project> --force
```

Verify a project's pipeline execution contract against `configs/phases.yaml` (exit 0 clean, 1 drift, 2 missing/invalid):
```bash
uv run pipeline-watch doctor <project>
```
```

- [ ] **Step 2: Document the contract schema in the README Configuration section**

In `README.md`, immediately after the existing `### TOML overlay (`.hermes/config.toml`)` section (ends around line 177, right before `## Troubleshooting`), insert:

```markdown

### Pipeline execution contract (`.hermes/pipeline.toml`)

Each project declares the assignee and tool capabilities its phases require in
a versioned contract at `.hermes/pipeline.toml`. Run `pipeline-watch init
<project>` once to write the default:

```toml
schema_version = 1
assignee = "default"
capabilities = ["Read", "Write", "Edit", "Bash"]
```

- `schema_version` — bumped whenever the contract's field set changes. A tick
  against a stale version fails closed with a remediation message instead of
  silently running with mismatched settings.
- `assignee` — passed as `--assignee` when registering each phase's kanban task.
- `capabilities` — the tool set phases are allowed to use. `pipeline-watch
  doctor <project>` cross-checks this against the `tools` each phase in
  `configs/phases.yaml` declares and reports drift.

Projects that have never run `init` tick with the defaults above — the
contract is additive, not a migration requirement. A project's tick only
blocks when a contract *exists* but is stale or under-declares capabilities.
```

- [ ] **Step 3: Add a contract step to the getting-started tutorial**

In `docs/tutorial-getting-started.md`, at the end of `## Step 3: Configure pipeline-watch` (currently ending at line 102 with "The status command shows TODOs that are eligible to merge." followed by a `---` separator at line 104), insert a new subsection before the `---`:

```markdown

Write the pipeline execution contract for this project — a small TOML file declaring which assignee and tool capabilities its phases require:

```bash
uv run pipeline-watch init myproject
```

Expected output:
```
Wrote pipeline execution contract: /path/to/myproject/.hermes/pipeline.toml
```

Verify it's consistent with `configs/phases.yaml`:

```bash
uv run pipeline-watch doctor myproject
```

Expected output:
```
OK: schema_version=1 assignee=default capabilities=['Bash', 'Edit', 'Read', 'Write']
```
```

- [ ] **Step 4: Verify the docs build / render correctly**

Run: `grep -c "pipeline-watch init\|pipeline-watch doctor" README.md docs/tutorial-getting-started.md`
Expected: non-zero counts in both files (no command to "run" here — this is a prose/Markdown change, so verification is a manual read-through plus the grep sanity check).

- [ ] **Step 5: Commit**

```bash
git add README.md docs/tutorial-getting-started.md
git commit -m "docs: document pipeline-watch init/doctor and the execution contract"
```

---

## Deferred to TODOS.md (unchanged from the gstack review — not part of this plan)

- **TODO-16**: Hermes profile integration — once `hermes profile create` supports `--model`/`--tools`/`--skills`, wire profile registration as an implementation of this contract.
- **TODO-17**: Contract migration tooling — version migration helpers, `--repair` flag, deprecation warnings beyond the `--force` regenerate this plan ships.
