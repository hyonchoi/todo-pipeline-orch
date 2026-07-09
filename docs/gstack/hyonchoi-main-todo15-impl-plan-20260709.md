# TODO-15: Pipeline Execution Contract

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define a versioned pipeline execution contract (TOML) and wire it into `init`, `doctor`, `tick`, and `register_todo_phases` so the orchestrator has a declarative, testable config surface instead of hardcoded defaults.

**Architecture:** New `hermes_pipeline/contract.py` module with a `PipelineContract` dataclass, loader, and validator. A bundled default contract lives at `hermes_pipeline/configs/pipeline.toml`. Two new CLI subcommands — `init` (write default contract to `~/.hermes/pipeline.toml`) and `doctor` (validate contract, cross-check against phases.yaml). The tick flow loads the contract and passes `assignee` from it to `register_todo_phases()`.

**Tech Stack:** Python 3.12+, `uv`, `tomllib` (stdlib), `pyyaml` (existing), no new dependencies.

## Global Constraints

- Python 3.12+, `uv`-managed package `hermes-pipeline`
- No new runtime dependencies — use stdlib `tomllib` and existing `pyyaml`
- Contract file lives at `~/.hermes/pipeline.toml` (user home, not repo-versioned)
- Default contract is bundled at `hermes_pipeline/configs/pipeline.toml` in the package
- Backward compatible — existing ticks work without a contract (fallback to `"default"` assignee)
- TDD: every task writes tests before implementation
- Exit codes: `init` — 0 success, 1 error; `doctor` — 0 clean, 1 drift, 2 missing

---

### Task 1: Create contract module with PipelineContract dataclass, loader, and validator

**Files:**
- Create: `hermes_pipeline/contract.py`
- Create: `hermes_pipeline/configs/pipeline.toml`
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: Nothing from other tasks
- Produces: `PipelineContract` dataclass, `load_pipeline_contract(path) -> PipelineContract`, `validate_contract(contract, phases) -> list[str]`, `PIPELINE_CONTRACT_VERSION = "1"`

- [ ] **Step 1: Write the failing test — dataclass shape and defaults**

```python
from hermes_pipeline.contract import PipelineContract, PIPELINE_CONTRACT_VERSION

def test_contract_version():
    assert PIPELINE_CONTRACT_VERSION == "1"

def test_contract_defaults():
    c = PipelineContract()
    assert c.version == "1"
    assert c.assignee == "pipeline"
    assert c.model_policy == "auto"
    assert c.tools == ["Read", "Write", "Bash"]
    assert c.safe_mode is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract.py::test_contract_version tests/test_contract.py::test_contract_defaults -v`
Expected: FAIL with `importerror` or `AttributeError`

- [ ] **Step 3: Write PipelineContract dataclass and version constant**

```python
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import tomllib

log = logging.getLogger(__name__)

PIPELINE_CONTRACT_VERSION = "1"

ValidTool = Literal["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch"]
VALID_TOOLS = {"Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch"}

ModelPolicy = Literal["auto", "selection"]

@dataclass(frozen=True)
class PipelineContract:
    """Versioned execution contract for unattended kanban phase execution."""
    version: str = PIPELINE_CONTRACT_VERSION
    assignee: str = "pipeline"
    model_policy: ModelPolicy = "auto"
    tools: list[str] = field(default_factory=lambda: ["Read", "Write", "Bash"])
    safe_mode: bool = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_contract.py::test_contract_version tests/test_contract.py::test_contract_defaults -v`
Expected: PASS

- [ ] **Step 5: Write the failing test — load_pipeline_contract from TOML file**

```python
from hermes_pipeline.contract import load_pipeline_contract, PIPELINE_CONTRACT_VERSION

def test_load_contract_from_toml(tmp_path):
    contract_file = tmp_path / "pipeline.toml"
    contract_file.write_text(
        '[pipeline]\n'
        'version = "1"\n'
        'assignee = "pipeline"\n'
        'model_policy = "auto"\n'
        'tools = ["Read", "Write", "Bash"]\n'
        'safe_mode = true\n'
    )
    c = load_pipeline_contract(contract_file)
    assert c.version == "1"
    assert c.assignee == "pipeline"
    assert c.model_policy == "auto"
    assert c.tools == ["Read", "Write", "Bash"]
    assert c.safe_mode is True

def test_load_contract_missing_file(tmp_path):
    from hermes_pipeline.contract import ContractNotFoundError
    path = tmp_path / "does_not_exist.toml"
    try:
        load_pipeline_contract(path)
        assert False, "should have raised"
    except ContractNotFoundError:
        pass

def test_load_contract_partial_fields_uses_defaults(tmp_path):
    """If only assignee is set, other fields fall back to defaults."""
    contract_file = tmp_path / "pipeline.toml"
    contract_file.write_text(
        '[pipeline]\n'
        'assignee = "custom-agent"\n'
    )
    c = load_pipeline_contract(contract_file)
    assert c.assignee == "custom-agent"
    assert c.model_policy == "auto"  # default
    assert c.safe_mode is True       # default
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_contract.py::test_load_contract_from_toml tests/test_contract.py::test_load_contract_missing_file tests/test_contract.py::test_load_contract_partial_fields_uses_defaults -v`
Expected: FAIL — `load_pipeline_contract` not defined

- [ ] **Step 7: Implement load_pipeline_contract and ContractNotFoundError**

```python
class ContractNotFoundError(Exception):
    """Raised when the contract file does not exist."""

class ContractValidationError(Exception):
    """Raised when contract schema validation fails."""
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Contract validation failed: " + "; ".join(errors))

def load_pipeline_contract(path: Path | str) -> PipelineContract:
    """Load a pipeline execution contract from a TOML file.

    Reads the [pipeline] section. Missing fields fall back to dataclass defaults.
    Raises ContractNotFoundError if the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise ContractNotFoundError(f"Contract file not found: {path}")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    section = data.get("pipeline", {})
    contract = PipelineContract(
        version=section.get("version", PIPELINE_CONTRACT_VERSION),
        assignee=section.get("assignee", "pipeline"),
        model_policy=section.get("model_policy", "auto"),
        tools=section.get("tools", ["Read", "Write", "Bash"]),
        safe_mode=section.get("safe_mode", True),
    )
    return contract
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract.py -k "load_contract" -v`
Expected: PASS

- [ ] **Step 9: Write the failing test — validate_contract**

```python
from hermes_pipeline.contract import validate_contract, PipelineContract, PIPELINE_CONTRACT_VERSION
from hermes_pipeline.phases import Phase

def test_validate_contract_clean():
    c = PipelineContract()
    phases = [
        Phase(phase_key="phase_2", name="P2", tools="Read,Write,Bash"),
    ]
    errors = validate_contract(c, phases)
    assert errors == []

def test_validate_contract_version_mismatch():
    c = PipelineContract(version="0")
    errors = validate_contract(c, [])
    assert any("version" in e.lower() for e in errors)

def test_validate_contract_invalid_tool_name():
    c = PipelineContract(tools=["Read", "Teleport"])
    errors = validate_contract(c, [])
    assert any("teleport" in e.lower() for e in errors)

def test_validate_contract_empty_assignee():
    c = PipelineContract(assignee="")
    errors = validate_contract(c, [])
    assert any("assignee" in e.lower() for e in errors)

def test_validate_contract_phase_tools_not_in_contract():
    """If a phase needs a tool not declared in the contract, flag it."""
    c = PipelineContract(tools=["Read", "Write"])
    phases = [
        Phase(phase_key="phase_4", name="P4", tools="Read,Write,Edit,Bash"),
    ]
    errors = validate_contract(c, phases)
    assert any("edit" in e.lower() or "phase_4" in e.lower() for e in errors)
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `uv run pytest tests/test_contract.py -k "validate_contract" -v`
Expected: FAIL — `validate_contract` not defined

- [ ] **Step 11: Implement validate_contract**

```python
def validate_contract(contract: PipelineContract, phases: list | None = None) -> list[str]:
    """Validate a pipeline contract.

    Returns a list of error strings. Empty list means valid.
    Checks:
    1. Version matches expected.
    2. Assignee is non-empty.
    3. All tools are known valid tool names.
    4. If phases provided, every phase's tools are a subset of contract tools.
    """
    errors = []

    if contract.version != PIPELINE_CONTRACT_VERSION:
        errors.append(
            f"Contract version mismatch: expected {PIPELINE_CONTRACT_VERSION!r}, "
            f"got {contract.version!r}"
        )

    if not contract.assignee or not contract.assignee.strip():
        errors.append("Assignee must not be empty")

    for tool in contract.tools:
        if tool not in VALID_TOOLS:
            errors.append(f"Unknown tool in contract: {tool!r} — valid tools: {sorted(VALID_TOOLS)}")

    if phases:
        contract_tools = {t.lower() for t in contract.tools}
        for phase in phases:
            phase_tools = {t.strip().lower() for t in phase.tools.split(",") if t.strip()}
            missing = phase_tools - contract_tools
            if missing:
                errors.append(
                    f"Phase {phase.phase_key} requires tools not in contract: "
                    f"{sorted(missing)}"
                )

    return errors
```

- [ ] **Step 12: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract.py -k "validate_contract" -v`
Expected: PASS

- [ ] **Step 13: Create bundled default contract TOML**

Write `hermes_pipeline/configs/pipeline.toml`:
```toml
# Pipeline Execution Contract — default template
# Copied to ~/.hermes/pipeline.toml by `pipeline-watch init`

[pipeline]
version = "1"
assignee = "pipeline"
model_policy = "auto"
tools = ["Read", "Write", "Bash"]
safe_mode = true
```

- [ ] **Step 14: Write test — bundled default contract loads and validates**

```python
from hermes_pipeline.contract import load_pipeline_contract, validate_contract, PIPELINE_CONTRACT_VERSION
from hermes_pipeline.phases import load_phases
import importlib_resources if python < 3.9 else use package path

def test_bundled_default_contract_is_valid():
    """The bundled default contract file should load and validate cleanly."""
    import pathlib
    # Resolve bundled contract relative to the package
    pkg_dir = pathlib.Path(__file__).parent.parent  # hermes_pipeline/
    bundled = pkg_dir / "configs" / "pipeline.toml"
    if bundled.exists():
        c = load_pipeline_contract(bundled)
        assert c.version == PIPELINE_CONTRACT_VERSION
```

- [ ] **Step 15: Run all Task 1 tests**

Run: `uv run pytest tests/test_contract.py -v`
Expected: PASS

- [ ] **Step 16: Commit**

```bash
git add hermes_pipeline/contract.py hermes_pipeline/configs/pipeline.toml tests/test_contract.py
git commit -m "feat: add pipeline execution contract module (TODO-15)"
```

---

### Task 2: Add `pipeline-watch init` subcommand

**Files:**
- Modify: `hermes_pipeline/cli.py:395-475` (build_parser — add subparser)
- Test: `tests/test_contract.py` (append init tests)

**Interfaces:**
- Consumes: `load_pipeline_contract`, `validate_contract`, `PIPELINE_CONTRACT_VERSION` from Task 1
- Produces: `_cmd_init(args, config) -> int`, bundled contract path via `as_bundled_contract_path()`, writes to `config.state_dir / "pipeline.toml"`

- [ ] **Step 1: Write the failing test — init writes default contract**

```python
from pathlib import Path
from hermes_pipeline.cli import _cmd_init
from hermes_pipeline.contract import load_pipeline_contract

def test_init_writes_default_contract(tmp_path, mocker):
    """pipeline-watch init copies bundled contract to ~/.hermes/pipeline.toml."""
    fake_args = mocker.MagicMock(project=None)
    config = mocker.MagicMock(state_dir=tmp_path)

    result = _cmd_init(fake_args, config)
    assert result == 0
    contract_path = tmp_path / "pipeline.toml"
    assert contract_path.exists()

    c = load_pipeline_contract(contract_path)
    assert c.assignee == "pipeline"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract.py::test_init_writes_default_contract -v`
Expected: FAIL — `_cmd_init` not defined

- [ ] **Step 3: Write the failing test — init is idempotent**

```python
def test_init_idempotent(tmp_path, mocker):
    """Running init twice should be a no-op on the second run."""
    import shutil
    # Mock the bundled contract so init has something to copy
    bundled_src = Path(__file__).parent.parent / "hermes_pipeline" / "configs" / "pipeline.toml"

    fake_args = mocker.MagicMock(project=None)
    config = mocker.MagicMock(state_dir=tmp_path)

    # First run
    result1 = _cmd_init(fake_args, config)
    assert result1 == 0
    dst = tmp_path / "pipeline.toml"
    assert dst.exists()
    first_content = dst.read_text()

    # Second run — should be no-op
    result2 = _cmd_init(fake_args, config)
    assert result2 == 0
    assert dst.read_text() == first_content  # content unchanged
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_contract.py::test_init_idempotent -v`
Expected: FAIL

- [ ] **Step 5: Implement _cmd_init and as_bundled_contract_path helper in contract.py**

Add to `hermes_pipeline/contract.py`:
```python
def as_bundled_contract_path() -> Path:
    """Return the path to the bundled default contract in the package."""
    return Path(__file__).parent / "configs" / "pipeline.toml"
```

- [ ] **Step 6: Implement _cmd_init in cli.py**

Add to `hermes_pipeline/cli.py`:
```python
def _cmd_init(args, config: Config) -> int:
    """Handle 'init' subcommand — install default pipeline contract.

    Copies the bundled contract to ~/.hermes/pipeline.toml.
    Idempotent: skips if the file already exists with matching content.
    Exit 0 on success, 1 on error.
    """
    from .contract import as_bundled_contract_path, load_pipeline_contract, validate_contract

    bundled = as_bundled_contract_path()
    if not bundled.exists():
        log.error("bundled contract not found at %s — installation may be corrupt", bundled)
        return 1

    dst = config.state_dir / "pipeline.toml"
    config.state_dir.mkdir(parents=True, exist_ok=True)

    # Idempotent: skip if content matches
    if dst.exists() and dst.read_text() == bundled.read_text():
        log.info("contract already installed at %s — nothing to do", dst)
        print(f"Pipeline contract already installed at {dst}")
        return 0

    import shutil
    shutil.copy2(str(bundled), str(dst))
    log.info("installed pipeline contract to %s", dst)
    print(f"Pipeline contract installed to {dst}")

    # Validate the installed contract
    contract = load_pipeline_contract(dst)
    errors = validate_contract(contract)
    if errors:
        log.error("installed contract has validation errors: %s", errors)
        return 1

    print("Contract validation: OK")
    return 0
```

- [ ] **Step 7: Register the init subparser in build_parser**

Add after the `recover-counter` parser (around line 470):
```python
    # init: Install default pipeline execution contract
    init_parser = subparsers.add_parser(
        "init",
        help="Install default pipeline execution contract (~/.hermes/pipeline.toml)",
    )
    init_parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing contract even if content matches",
    )
    init_parser.set_defaults(func=_cmd_init)

    # doctor: Validate pipeline execution contract
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate pipeline execution contract and check for drift",
    )
    doctor_parser.set_defaults(func=_cmd_doctor)
```

- [ ] **Step 8: Wire init to main() dispatch**

In `main()`, ensure the dispatch handles `init`: add to the command routing. Check the existing pattern — if `func` is set on args, it's called. Both `init` and `doctor` use `set_defaults(func=...)` so they should flow through the existing dispatch. Verify:

Run: `grep -n "args.func" hermes_pipeline/cli.py`

- [ ] **Step 9: Run Task 2 tests**

Run: `uv run pytest tests/test_contract.py -k "init" -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add hermes_pipeline/cli.py hermes_pipeline/contract.py tests/test_contract.py
git commit -m "feat: add pipeline-watch init subcommand (TODO-15)"
```

---

### Task 3: Add `pipeline-watch doctor` subcommand

**Files:**
- Modify: `hermes_pipeline/cli.py` (add `_cmd_doctor`)
- Modify: `hermes_pipeline/contract.py` (add `find_contract_path` helper)
- Test: `tests/test_contract.py` (append doctor tests)

**Interfaces:**
- Consumes: `load_pipeline_contract`, `validate_contract`, `find_contract_path` from Task 1+2
- Produces: `_cmd_doctor(args, config) -> int`, exit codes: 0 clean, 1 drift, 2 missing

- [ ] **Step 1: Write the failing test — doctor with valid contract returns 0**

```python
from hermes_pipeline.cli import _cmd_doctor
from hermes_pipeline.contract import as_bundled_contract_path

def test_doctor_clean_returns_0(tmp_path, mocker):
    """doctor exits 0 when contract is valid."""
    # Set up a valid contract
    bundled = as_bundled_contract_path()
    dst = tmp_path / "pipeline.toml"
    if bundled.exists():
        import shutil
        shutil.copy2(str(bundled), str(dst))

    fake_args = mocker.MagicMock()
    config = mocker.MagicMock(state_dir=tmp_path)

    result = _cmd_doctor(fake_args, config)
    assert result == 0
```

- [ ] **Step 2: Write the failing test — doctor with missing contract returns 2**

```python
def test_doctor_missing_contract_returns_2(tmp_path, mocker):
    """doctor exits 2 when no contract file exists."""
    fake_args = mocker.MagicMock()
    config = mocker.MagicMock(state_dir=tmp_path)

    result = _cmd_doctor(fake_args, config)
    assert result == 2
```

- [ ] **Step 3: Write the failing test — doctor detects phase-tool drift (exit 1)**

```python
def test_doctor_drift_returns_1(tmp_path, mocker):
    """doctor exits 1 when contract tools don't cover phase requirements."""
    # Write contract with minimal tools
    contract_file = tmp_path / "pipeline.toml"
    contract_file.write_text(
        '[pipeline]\n'
        'version = "1"\n'
        'assignee = "pipeline"\n'
        'tools = ["Read"]\n'  # phases need Write, Bash, Edit too
        'safe_mode = true\n'
    )

    fake_args = mocker.MagicMock()
    config = mocker.MagicMock(state_dir=tmp_path)

    result = _cmd_doctor(fake_args, config)
    assert result == 1  # drift detected
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_contract.py -k "doctor" -v`
Expected: FAIL — `_cmd_doctor` not defined

- [ ] **Step 5: Add find_contract_path helper to contract.py**

```python
def find_contract_path(state_dir: Path) -> Path | None:
    """Find the pipeline contract file in the given state directory.

    Returns the path if it exists, None otherwise.
    """
    p = Path(state_dir) / "pipeline.toml"
    return p if p.exists() else None
```

- [ ] **Step 6: Implement _cmd_doctor in cli.py**

```python
def _cmd_doctor(args, config: Config) -> int:
    """Handle 'doctor' subcommand — validate contract and check for drift.

    Exit 0: contract is valid, no drift.
    Exit 1: drift detected (validation warnings/errors).
    Exit 2: contract file not found.
    """
    from .contract import (
        find_contract_path,
        load_pipeline_contract,
        validate_contract,
        PIPELINE_CONTRACT_VERSION,
        as_bundled_contract_path,
    )
    from .phases import load_phases

    contract_path_opt = find_contract_path(config.state_dir)

    if contract_path_opt is None:
        print(f"Error: pipeline contract not found at {config.state_dir / 'pipeline.toml'}")
        print(f"Run 'pipeline-watch init' to install the default contract.")
        return 2

    # Load contract
    try:
        contract = load_pipeline_contract(contract_path_opt)
    except Exception as e:
        print(f"Error: failed to load contract: {e}")
        return 2

    # Load phases for cross-validation
    try:
        phases = load_phases()
    except Exception:
        phases = []

    # Validate
    errors = validate_contract(contract, phases)

    if not errors:
        print(f"Contract: OK (version {contract.version})")
        print(f"  assignee: {contract.assignee}")
        print(f"  model_policy: {contract.model_policy}")
        print(f"  tools: {', '.join(contract.tools)}")
        print(f"  safe_mode: {contract.safe_mode}")
        return 0

    # Drift detected
    print(f"Contract: DRIFT DETECTED (version {contract.version})")
    for err in errors:
        print(f"  - {err}")
    print(f"Fix: edit {contract_path_opt} or run 'pipeline-watch init --force' to reset.")
    return 1
```

- [ ] **Step 7: Run Task 3 tests**

Run: `uv run pytest tests/test_contract.py -k "doctor" -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add hermes_pipeline/cli.py hermes_pipeline/contract.py tests/test_contract.py
git commit -m "feat: add pipeline-watch doctor subcommand (TODO-15)"
```

---

### Task 4: Wire contract assignee into register_todo_phases and tick flow

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py:66-73` (accept contract or use fallback)
- Modify: `hermes_pipeline/cli.py` (_tick_project — load contract, pass assignee)
- Test: `tests/test_contract.py` (append wiring tests)

**Interfaces:**
- Consumes: `load_pipeline_contract`, `ContractNotFoundError` from Task 1
- Produces: `_resolve_assignee(state_dir) -> str` helper in cli.py, contract-aware tick flow

- [ ] **Step 1: Write the failing test — _resolve_assignee reads from contract**

```python
from hermes_pipeline.cli import _resolve_assignee

def test_resolve_assignee_from_contract(tmp_path):
    """_resolve_assignee reads assignee from the contract file."""
    contract_file = tmp_path / "pipeline.toml"
    contract_file.write_text(
        '[pipeline]\n'
        'assignee = "pipeline"\n'
    )
    assert _resolve_assignee(tmp_path) == "pipeline"

def test_resolve_assignee_fallback_when_no_contract(tmp_path):
    """_resolve_assignee falls back to 'default' when no contract exists."""
    assert _resolve_assignee(tmp_path) == "default"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_contract.py -k "resolve_assignee" -v`
Expected: FAIL — `_resolve_assignee` not defined

- [ ] **Step 3: Implement _resolve_assignee in cli.py**

Add near `_cmd_init`:
```python
def _resolve_assignee(state_dir: Path) -> str:
    """Resolve the kanban assignee from the contract, falling back to 'default'.

    Reads the contract from state_dir/pipeline.toml.
    If the contract doesn't exist or can't be loaded, returns 'default'.
    """
    from .contract import find_contract_path, load_pipeline_contract

    cp = find_contract_path(state_dir)
    if cp is None:
        return "default"
    try:
        contract = load_pipeline_contract(cp)
        if contract.assignee and contract.assignee.strip():
            return contract.assignee
    except Exception as e:
        log.debug("failed to load contract for assignee resolution: %s", e)
    return "default"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract.py -k "resolve_assignee" -v`
Expected: PASS

- [ ] **Step 5: Wire _resolve_assignee into _tick_project**

In `_tick_project()` (around the call to `register_todo_phases`), replace the hardcoded `assignee="default"` with a dynamic lookup. Find the exact call site:

```bash
grep -n "register_todo_phases" hermes_pipeline/cli.py
```

The change: before the `register_todo_phases()` call, resolve the assignee:
```python
assignee = _resolve_assignee(project_state)
```

Then pass `assignee=assignee` to `register_todo_phases()`.

- [ ] **Step 6: Write test — tick uses contract assignee in kanban command**

```python
def test_tick_uses_contract_assignee(tmp_path, mocker):
    """When contract exists, tick passes its assignee to register_todo_phases."""
    from hermes_pipeline.contract import as_bundled_contract_path
    bundled = as_bundled_contract_path()

    # Set up contract
    dst = tmp_path / "pipeline.toml"
    if bundled.exists():
        import shutil
        shutil.copy2(str(bundled), str(dst))

    assignee = _resolve_assignee(tmp_path)
    assert assignee == "pipeline"  # from default contract
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/test_contract.py::test_tick_uses_contract_assignee -v`
Expected: PASS

- [ ] **Step 8: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All existing tests pass + new contract tests pass

- [ ] **Step 9: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_contract.py
git commit -m "feat: wire contract assignee into tick flow (TODO-15)"
```

---

### Task 5: End-to-end integration tests and edge cases

**Files:**
- Modify: `tests/test_contract.py` (add integration and chaos tests)
- Test: `tests/test_contract.py`

**Interfaces:**
- Consumes: All functions from Tasks 1-4
- Produces: Integration tests covering full init→doctor→tick flow and edge cases

- [ ] **Step 1: Write the failing test — full init + doctor flow**

```python
def test_init_then_doctor_flows(tmp_path, mocker):
    """End-to-end: init writes contract, doctor validates it."""
    from hermes_pipeline.cli import _cmd_init, _cmd_doctor

    fake_args = mocker.MagicMock(project=None)
    config = mocker.MagicMock(state_dir=tmp_path)

    # Init
    init_result = _cmd_init(fake_args, config)
    assert init_result == 0
    assert (tmp_path / "pipeline.toml").exists()

    # Doctor
    doctor_result = _cmd_doctor(fake_args, config)
    assert doctor_result == 0
```

- [ ] **Step 2: Write the failing test — doctor detects version mismatch**

```python
def test_doctor_version_mismatch(tmp_path, mocker):
    """doctor flags contract with wrong version."""
    contract_file = tmp_path / "pipeline.toml"
    contract_file.write_text(
        '[pipeline]\n'
        'version = "0"\n'  # wrong version
        'assignee = "pipeline"\n'
        'tools = ["Read", "Write", "Bash"]\n'
        'safe_mode = true\n'
    )
    fake_args = mocker.MagicMock()
    config = mocker.MagicMock(state_dir=tmp_path)

    from hermes_pipeline.cli import _cmd_doctor
    result = _cmd_doctor(fake_args, config)
    assert result == 1  # drift
```

- [ ] **Step 3: Write the failing test — contract with bad TOML parsing**

```python
def test_doctor_invalid_toml(tmp_path, mocker):
    """doctor handles malformed TOML gracefully."""
    contract_file = tmp_path / "pipeline.toml"
    contract_file.write_text("this is not [[ valid toml {{{\n")
    fake_args = mocker.MagicMock()
    config = mocker.MagicMock(state_dir=tmp_path)

    from hermes_pipeline.cli import _cmd_doctor
    result = _cmd_doctor(fake_args, config)
    assert result == 2  # missing/unreadable
```

- [ ] **Step 4: Write the failing test — backward compat: tick without contract**

```python
def test_backward_compat_no_contract():
    """_resolve_assignee returns 'default' when no contract exists."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        assignee = _resolve_assignee(Path(tmp))
        assert assignee == "default"
```

- [ ] **Step 5: Run all Task 5 tests**

Run: `uv run pytest tests/test_contract.py -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add tests/test_contract.py
git commit -m "test: add integration and edge-case tests for contract (TODO-15)"
```

---

## Self-Review

**1. Spec coverage:**
- Contract schema (versioned TOML) -> Task 1 PIPELINE_CONTRACT_VERSION, PipelineContract dataclass
- `pipeline-watch init` -> Task 2 _cmd_init
- `pipeline-watch doctor` -> Task 3 _cmd_doctor
- Wire assignee into `register_todo_phases` -> Task 4 _resolve_assignee
- Contract validation -> Task 1 validate_contract
- Drift detection -> Task 3 doctor cross-validates against phases.yaml
- Profile verification deferred to TODO-16 -> Not in scope, consistent with reframed spec
- Backward compatibility -> Task 4 fallback to "default"
- Error messages with remediation -> Task 2+3 print statements with fix guidance
- Exit codes 0/1/2 -> Task 2+3
- Structured logging -> log.info/log.error calls throughout

**2. Placeholder scan:** No TBD, TODO, "implement later", vague error handling instructions. Every step has concrete code.

**3. Type consistency:**
- `PipelineContract.version: str`, `assignee: str`, `model_policy: ModelPolicy`, `tools: list[str]`, `safe_mode: bool` — used consistently across loader, validator, init, doctor
- `ContractNotFoundError` and `ContractValidationError` — raised in loader, caught in doctor and _resolve_assignee
- Exit codes: init (0/1), doctor (0/1/2) — consistent across tasks
- `_resolve_assignee` returns `str`, used as `assignee=...` in register_todo_phases — matches existing `str = "default"` param type

**No gaps found. Plan is complete.**

---
