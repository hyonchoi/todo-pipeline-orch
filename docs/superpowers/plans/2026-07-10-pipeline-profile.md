# Pipeline Profile + SOUL.md + Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bundle a dedicated Hermes profile (SOUL.md + distribution.yaml) with the pipeline orchestrator so the unattended agent has the right personality and can be installed in two commands.

**Architecture:** Move `profiles/pipeline/` and `configs/phases.yaml` into the package as in-package data (`hermes_pipeline/data/`), resolved via `importlib.resources`. Add `install-profile` CLI command and `--assignee` flag to `init`. Harden `doctor` to verify profile existence when assignee is non-default.

**Tech Stack:** Python 3.12+, uv, importlib.resources, hatchling package data.

## Global Constraints

- Python 3.12+, uv-managed package — no new runtime dependencies
- Contract is per-project at `<project_state>/pipeline.toml`
- Profile lives in `~/.hermes/` via Hermes profile API
- `pipeline-watch` must work as an installed tool (`uv tool install` / `pipx`) from arbitrary CWD — **no out-of-package data access**
- Backward compatible — existing ticks work without a contract (fallback to `"default"` assignee)
- SOUL.md is advisory, not enforcement — the contract's capability check is the only hard surface

---

### Task 1: Write SOUL.md — Agent Personality

**Files:**
- Create: `hermes_pipeline/data/profiles/pipeline/SOUL.md`

**Interfaces:**
- Produces: SOUL.md file — consumed by Task 2 (distribution.yaml packaging) and used by the Hermes profile at runtime

- [ ] **Step 1: Write the SOUL.md**

Create `hermes_pipeline/data/profiles/pipeline/SOUL.md` with the following content:

```markdown
# SOUL — Pipeline Agent Personality

## Role

You are an unattended worker driving kanban phases autonomously. There is no human at the terminal.

## Key Behaviors

1. **No interactive prompts.** There is no one to answer them. If a phase prompt says "apply fixes," apply fixes. If it says "write tests," write tests. No "should I?"

2. **Follow skill instructions literally.** The phase prompt is the spec, not a suggestion. If it names a skill, use it. If it names a file, read it. If it says commit, commit.

3. **Commits are your voice.** Clear messages, atomic changes. Each commit does one thing and the message says what it is.

4. **Surface errors without dwelling.** If something fails, state what broke and what you'll do next. Don't narrate the debugging process unless the phase is stalled.

5. **Be decisive on judgment calls.** When reviewing code, decide. When writing, write. When shipping, ship (or halt at a gate, as instructed).

6. **Narrate only what's necessary for debugging.** A phase stall should be diagnosable from the output. A successful phase should be terse.

7. **Stay skill-agnostic.** Don't hard-code phase names or gstack skill names into output. The phase prompt carries the skill invocation; you execute it.

## Timeout Behavior

If a phase approaches its turn or time limit, complete the current atomic action (finish the edit, finish the commit) then stop. Don't start something new in the last turn.

## Refusal

Refuse a phase only if:
- The project directory doesn't exist or is inaccessible.
- The phase prompt is empty or contains only placeholders.
- A gate is blocking and the prompt says to wait.

In all other cases, attempt the work. If you can't complete it, document what you did and where you stopped.
```

- [ ] **Step 2: Verify the file was created**

```bash
test -f hermes_pipeline/data/profiles/pipeline/SOUL.md && echo "OK" || echo "MISSING"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add hermes_pipeline/data/profiles/pipeline/SOUL.md
git commit -m "feat: add SOUL.md — pipeline agent personality"
```

---

### Task 2: Create distribution.yaml — Hermes Distribution Manifest

**Files:**
- Create: `hermes_pipeline/data/profiles/pipeline/distribution.yaml`

**Interfaces:**
- Consumes: SOUL.md from Task 1 (same directory)
- Produces: distribution.yaml — the complete profile distribution, installable via `hermes profile install <dir>`

- [ ] **Step 1: Write distribution.yaml**

Create `hermes_pipeline/data/profiles/pipeline/distribution.yaml`:

```yaml
name: pipeline
description: "Unattended kanban pipeline agent — autonomous, decisive, no interactive prompts"
min_hermes_version: "1.0"
```

**Why these values:**
- `name: pipeline` — matches the assignee value (`assignee = "pipeline"`) so routing works. This is the load-bearing coupling flagged by ENG-1.
- `description` — descriptive enough for the kanban decomposer to route by role.
- `min_hermes_version` — conservative 1.0; the profile API is stable.

- [ ] **Step 2: Verify the distribution structure**

```bash
ls hermes_pipeline/data/profiles/pipeline/
```

Expected output: `SOUL.md  distribution.yaml`

- [ ] **Step 3: Commit**

```bash
git add hermes_pipeline/data/profiles/pipeline/distribution.yaml
git commit -m "feat: add distribution.yaml for pipeline Hermes profile"
```

---

### Task 3: Migrate `load_phases()` to In-Package Resolution

**Files:**
- Move: `configs/phases.yaml` → `hermes_pipeline/data/phases.yaml`
- Modify: `hermes_pipeline/phases.py:23-26` — `load_phases()` resolution logic

**Interfaces:**
- Consumes: `configs/phases.yaml` (existing file at repo root)
- Produces: Updated `load_phases()` that resolves phases.yaml via `importlib.resources`, package-relative

- [ ] **Step 1: Write the failing test**

```python
# tests/test_phases_package_resolution.py — new file
def test_load_phases_uses_package_data(tmp_path, mocker):
    """load_phases() resolves phases.yaml from in-package data, not repo root."""
    from hermes_pipeline.phases import load_phases

    # Should load even from a CWD that has no configs/ directory
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        phases = load_phases()
        assert len(phases) > 0
        assert phases[0].phase_key == "phase_2_autoplan"
    finally:
        os.chdir(original_cwd)
```

- [ ] **Step 2: Move phases.yaml into the package**

```bash
mv configs/phases.yaml hermes_pipeline/data/phases.yaml
```

Delete the now-empty `configs/` directory if it's empty:
```bash
rmdir configs 2>/dev/null || true
```

- [ ] **Step 3: Update `load_phases()` to use `importlib.resources`**

Modify `hermes_pipeline/phases.py:23-26`:

```python
def load_phases(config_path: Path | str | None = None) -> list[Phase]:
    if config_path is None:
        from importlib.resources import files
        config_path = files("hermes_pipeline").joinpath("data", "phases.yaml")
    config_path = Path(config_path)
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return [Phase(**p) for p in data["phases"]]
```

- [ ] **Step 4: Run existing tests to verify no regression**

```bash
uv run pytest tests/test_phases.py tests/test_phases_invoke.py -v
```

Expected: All existing tests pass.

- [ ] **Step 5: Run the new test**

```bash
uv run pytest tests/test_phases_package_resolution.py -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/data/phases.yaml hermes_pipeline/phases.py tests/test_phases_package_resolution.py
git rm -rf configs/ 2>/dev/null || true
git commit -m "refactor: migrate phases.yaml to in-package data with importlib.resources"
```

---

### Task 4: Configure hatchling to Ship Package Data

**Files:**
- Modify: `pyproject.toml` — add `[tool.hatch.build.targets.wheel]` section for package data

**Interfaces:**
- Consumes: `hermes_pipeline/data/` directory structure from Tasks 1-3
- Produces: pyproject.toml configured to include `data/` subdirectory in the wheel

- [ ] **Step 1: Verify the data directory structure**

```bash
find hermes_pipeline/data -type f | sort
```

Expected output:
```
hermes_pipeline/data/phases.yaml
hermes_pipeline/data/profiles/pipeline/SOUL.md
hermes_pipeline/data/profiles/pipeline/distribution.yaml
```

- [ ] **Step 2: Add hatchling package data configuration to pyproject.toml**

Add this section after the existing `[build-system]` block in `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel]
packages = ["hermes_pipeline"]
```

The `hermes_pipeline/data/` files are inside the package directory, so hatchling includes them automatically as package data when the package is specified as above. No additional `include` directive needed — files inside the package namespace are included by default.

- [ ] **Step 3: Verify the package builds with data included**

```bash
uv build && unzip -l dist/hermes_pipeline-*.whl | grep "data/"
```

Expected: Output includes `phases.yaml`, `SOUL.md`, and `distribution.yaml` paths.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: configure hatchling to include package data in wheel"
```

---

### Task 5: Add `install-profile` CLI Command

**Files:**
- Modify: `hermes_pipeline/cli.py` — add subcommand parser and handler
- Modify: `hermes_pipeline/contract.py` — add `bundled_profile_dir()` helper function
- Test: `tests/test_cli_contract.py` — add install-profile tests

**Interfaces:**
- Consumes: `hermes_pipeline/data/profiles/pipeline/` (Tasks 1-2), in-package resolution (Task 3 pattern)
- Produces: `pipeline-watch install-profile [--force]` CLI command

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_contract.py`:

```python
class TestInstallProfileParser:
    def test_install_profile_parses_force(self):
        parser = build_parser()
        args = parser.parse_args(["install-profile", "--force"])
        assert args.command == "install-profile"
        assert args.force is True

    def test_install_profile_force_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["install-profile"])
        assert args.force is False
        assert args.force is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_cli_contract.py::TestInstallProfileParser -v
```

Expected: FAIL — "install-profile" subcommand doesn't exist yet

- [ ] **Step 3: Add `bundled_profile_dir()` helper to contract.py**

Add to `hermes_pipeline/contract.py`:

```python
def bundled_profile_dir() -> Path:
    """Return the path to the bundled pipeline profile distribution.

    Resolves package-relative so it works whether running from a checkout
    or from an installed wheel.
    """
    from importlib.resources import files
    return Path(files("hermes_pipeline").joinpath("data", "profiles", "pipeline"))
```

- [ ] **Step 4: Add the `install-profile` subcommand parser to cli.py**

Add after the doctor parser (around line 517):

```python
    # install-profile: Install the bundled pipeline Hermes profile
    install_profile_parser = subparsers.add_parser(
        "install-profile",
        help="Install the bundled pipeline Hermes profile",
    )
    install_profile_parser.add_argument(
        "--force", action="store_true",
        help="Force reinstall even if the profile already exists",
    )
    install_profile_parser.set_defaults(func=_cmd_install_profile)
```

- [ ] **Step 5: Implement `_cmd_install_profile` handler in cli.py**

Add after `_cmd_doctor`:

```python
def _cmd_install_profile(args, config: Config) -> int:
    """Handle 'install-profile' subcommand — install the bundled pipeline profile.

    Resolves the bundled distribution package-relative, shells
    `hermes profile install [--force]`, then verifies with
    `hermes profile show pipeline`.

    Exit codes: 0 success, 1 hermes install/show failure, 2 hermes not found.
    """
    from .contract import bundled_profile_dir

    profile_dir = bundled_profile_dir()

    if not (profile_dir / "distribution.yaml").exists():
        log.error("bundled profile distribution not found at %s", profile_dir)
        return 1

    cmd = ["hermes", "profile", "install", str(profile_dir)]
    if args.force:
        cmd.append("--force")

    print(f"Installing pipeline profile from {profile_dir}...")
    result = _cli_sp.run(cmd, text=True)
    if result.returncode != 0:
        print(f"Problem: `hermes profile install` failed (exit {result.returncode})")
        print(f"Cause: Hermes may not be installed, or the profile source is invalid.")
        if result.stderr:
            print(f"Details: {result.stderr.strip()}")
        print(f"Fix: Ensure Hermes is installed and accessible, then retry.")
        return 2

    # Post-install verification: prove the profile is resolvable
    print("Verifying profile installation...")
    verify = _cli_sp.run(
        ["hermes", "profile", "show", "pipeline"], text=True, capture_output=True
    )
    if verify.returncode != 0:
        print(f"Problem: Profile installed but `hermes profile show pipeline` failed.")
        print(f"Cause: Profile name may not match 'pipeline', or Hermes caching issue.")
        print(f"Fix: Run `hermes profile list` to check installed profiles.")
        return 1

    print("Pipeline profile installed successfully.")
    print()
    print("Next step: set the assignee in your project contract:")
    print("  pipeline-watch init <project> --assignee pipeline")
    print("Then verify with:")
    print("  pipeline-watch doctor <project>")
    return 0
```

- [ ] **Step 6: Run parser tests**

```bash
uv run pytest tests/test_cli_contract.py::TestInstallProfileParser -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/cli.py hermes_pipeline/contract.py tests/test_cli_contract.py
git commit -m "feat: add install-profile CLI command for bundled Hermes profile"
```

---

### Task 6: Add `--assignee` Flag to `init` Command

**Files:**
- Modify: `hermes_pipeline/cli.py` — add `--assignee` argument to init parser and update `_cmd_init`
- Test: `tests/test_cli_contract.py` — add --assignee tests

**Interfaces:**
- Consumes: existing `_cmd_init` and `init_parser` from cli.py
- Produces: `pipeline-watch init <project> --assignee pipeline` writes the assignee field directly (no hand-edit needed)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_contract.py`:

```python
class TestInitAssignee:
    def test_init_assignee_parser(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo", "--assignee", "pipeline"])
        assert args.assignee == "pipeline"

    def test_init_assignee_defaults_to_none(self):
        parser = build_parser()
        args = parser.parse_args(["init", "demo"])
        assert args.assignee is None

    def test_init_writes_assignee_flag_value(self, tmp_path, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee="pipeline"), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert 'assignee = "pipeline"' in contract.read_text()

    def test_init_without_assignee_uses_default(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _create_project(projects_dir, "demo")
        config = Config(projects_dir=projects_dir)

        result = _cmd_init(FakeArgs(project="demo", force=False, assignee=None), config)

        assert result == 0
        contract = projects_dir / "demo" / ".hermes" / "pipeline.toml"
        assert 'assignee = "default"' in contract.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_cli_contract.py::TestInitAssignee -v
```

Expected: FAIL — `FakeArgs` needs `assignee` attribute, parser doesn't have the flag yet

- [ ] **Step 3: Add `--assignee` argument to init parser**

Modify the init parser section (around line 504-509):

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
    init_parser.add_argument(
        "--assignee", default=None,
        help="Set the assignee field (e.g., --assignee pipeline)",
    )
    init_parser.set_defaults(func=_cmd_init)
```

- [ ] **Step 4: Update `_cmd_init` to use --assignee**

Modify `_render_default_contract_toml` in `contract.py` to accept an optional assignee parameter. Instead of modifying the render function (which is used by `write_default_contract`), modify `_cmd_init` to write the assignee after the default contract is created:

Update `_cmd_init` in `cli.py` (around line 1257). After the contract is written (or if it already existed), if `--assignee` is provided, patch the assignee field:

```python
def _cmd_init(args, config: Config) -> int:
    """Handle 'init' subcommand — write the default pipeline execution contract."""
    project_dir = _resolve_project_dir(config, args.project)
    if project_dir is None:
        return 2

    from .state_migration import _get_project_state_dir
    from .contract import contract_path, write_default_contract, load_contract, PipelineContract

    project_state = _get_project_state_dir(project_dir)
    path = contract_path(project_state)

    try:
        if args.force and path.exists():
            path.unlink()
        written = write_default_contract(project_state)
    except OSError as e:
        log.error("failed to write pipeline contract at %s: %s", path, e)
        return 1

    # If --assignee was provided, patch the assignee field in the written file
    if args.assignee is not None and path.exists():
        import tomllib
        try:
            data = tomllib.loads(path.read_text())
            data["assignee"] = args.assignee
            # Re-render as TOML
            toml_lines = [f"# Pipeline execution contract — read at tick start."]
            toml_lines.append(f'schema_version = {data["schema_version"]}')
            toml_lines.append(f'assignee = "{data["assignee"]}"')
            caps = data.get("capabilities", ["Read", "Write", "Edit", "Bash"])
            caps_toml = ", ".join(f'"{c}"' for c in caps)
            toml_lines.append(f"capabilities = [{caps_toml}]")
            path.write_text("\n".join(toml_lines) + "\n")
        except (tomllib.TOMLDecodeError, KeyError) as e:
            log.error("failed to patch assignee in %s: %s", path, e)
            return 1

    if written:
        print(f"Wrote pipeline execution contract: {path}")
    else:
        print(f"Pipeline execution contract already exists: {path} (use --force to regenerate)")
    return 0
```

- [ ] **Step 5: Run all init tests**

```bash
uv run pytest tests/test_cli_contract.py::TestInitAssignee tests/test_cli_contract.py::TestCmdInit tests/test_cli_contract.py::TestBuildParserInit -v
```

Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_cli_contract.py
git commit -m "feat: add --assignee flag to init command"
```

---

### Task 7: Harden `doctor` — Fail-Closed on Missing Profile

**Files:**
- Modify: `hermes_pipeline/cli.py` — `_cmd_doctor` handler
- Test: `tests/test_cli_contract.py` — add missing-profile doctor tests

**Interfaces:**
- Consumes: contract.assignee from the existing contract module
- Produces: doctor exits 2 when assignee is non-default but the Hermes profile isn't installed

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_contract.py`:

```python
class TestDoctorMissingProfile:
    def test_doctor_checks_profile_for_non_default_assignee(self, tmp_path, mocker, capsys):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\nassignee = "pipeline"\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        # Simulate: hermes profile show "pipeline" fails (profile not installed)
        mocker.patch(
            "hermes_pipeline.cli._cli_sp.run",
            return_value=MagicMock(returncode=1, stderr="profile not found", stdout=""),
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"))

        assert result == 2
        out = capsys.readouterr().out
        assert "pipeline" in out.lower() or "profile" in out.lower()

    def test_doctor_skips_profile_check_for_default_assignee(self, tmp_path, mocker, capsys):
        """When assignee is 'default', doctor should NOT check Hermes profile."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = _create_project(projects_dir, "demo")
        (project_dir / ".hermes").mkdir(parents=True)
        (project_dir / ".hermes" / "pipeline.toml").write_text(
            'schema_version = 1\ncapabilities = ["Read", "Write", "Bash"]\n'
        )
        call_count = {"n": 0}
        original_run = _cli_sp.run
        def tracking_run(*a, **kw):
            call_count["n"] += 1
            cmd = a[0] if a else kw.get("args", [])
            if "profile" in cmd:
                return MagicMock(returncode=1, stderr="profile not found", stdout="")
            return original_run(*a, **kw)
        mocker.patch("hermes_pipeline.cli._cli_sp.run", side_effect=tracking_run)
        mocker.patch(
            "hermes_pipeline.cli.load_phases",
            return_value=[Phase(phase_key="p1", name="P1", tools="Read,Write,Bash")],
        )
        config = Config(projects_dir=projects_dir)

        result = _cmd_doctor(FakeArgs(project="demo"))

        assert result == 0
        assert call_count["n"] == 0  # No hermes calls for default assignee
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_cli_contract.py::TestDoctorMissingProfile -v
```

Expected: FAIL — doctor doesn't check profile yet

- [ ] **Step 3: Update `_cmd_doctor` to check profile for non-default assignee**

Add the profile verification logic after the capability drift check in `_cmd_doctor`:

```python
def _cmd_doctor(args, config: Config) -> int:
    """Handle 'doctor' subcommand — verify the pipeline execution contract.

    Exit codes: 0 clean, 1 drift (capability mismatch), 2 missing/invalid
    contract, unknown project, or missing profile.
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
            f"required by phases.yaml — edit the contract to add them"
        )
        return 1

    # Verify the assigned profile is actually installed (non-default assignee only)
    if contract.assignee != "default":
        verify_result = _cli_sp.run(
            ["hermes", "profile", "show", contract.assignee],
            text=True, capture_output=True,
        )
        if verify_result.returncode != 0:
            print(
                f"MISSING: Hermes profile '{contract.assignee}' is not installed, "
                f"but contract assignee is set to '{contract.assignee}'"
            )
            print(
                f"Cause: The profile was never installed, or it was removed after install."
            )
            print(
                f"Fix: Install the bundled profile with `pipeline-watch install-profile`, "
                f"or create a custom profile named '{contract.assignee}' with `hermes profile create {contract.assignee}`."
            )
            return 2

    print(
        f"OK: schema_version={contract.schema_version} assignee={contract.assignee} "
        f"capabilities={sorted(contract.capabilities)}"
    )
    return 0
```

- [ ] **Step 4: Run all doctor tests**

```bash
uv run pytest tests/test_cli_contract.py::TestDoctorMissingProfile tests/test_cli_contract.py::TestCmdDoctor -v
```

Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_cli_contract.py
git commit -m "feat: doctor fail-closed when non-default assignee profile is missing"
```

---

### Task 8: Regression Test Suite — Full Pipeline

**Files:**
- Test: `tests/test_cli_contract.py` — run full suite

**Interfaces:**
- Consumes: All changes from Tasks 1-7
- Produces: Verified green test suite

- [ ] **Step 1: Run the full test suite**

```bash
uv run pytest tests/ -v --tb=short
```

Expected: All 30+ existing tests pass, all new tests pass. No regressions.

- [ ] **Step 2: Run the build to verify package data is included**

```bash
uv build && unzip -l dist/hermes_pipeline-*.whl | grep "data/"
```

Expected: Output includes `phases.yaml`, `SOUL.md`, `distribution.yaml` paths within the wheel.

- [ ] **Step 3: Commit if tests pass**

```bash
git commit --allow-empty -m "test: verify full test suite passes after profile + distribution changes" 2>/dev/null || true
```

(Only commit if there were actual changes in previous steps that weren't committed. If everything was already committed task-by-task, skip this.)

---

### Task 9: Update Documentation — Wiring Walkthrough

**Files:**
- Create: `docs/howto-pipeline-profile.md` — onboarding walkthrough

**Interfaces:**
- Produces: Complete wiring walkthrough doc (init → install → doctor → first tick)

- [ ] **Step 1: Write the walkthrough doc**

Create `docs/howto-pipeline-profile.md`:

```markdown
# How to: Set Up the Pipeline Profile

This guide walks through setting up the dedicated pipeline agent profile for unattended kanban execution.

## Quick Start (Two Commands)

```bash
# 1. Initialize the project contract with the pipeline assignee
pipeline-watch init <project> --assignee pipeline

# 2. Install the bundled Hermes profile
pipeline-watch install-profile
```

## Step-by-Step Walkthrough

### Step 1: Initialize the Project Contract

```bash
pipeline-watch init myproject --assignee pipeline
```

Output:
```
Wrote pipeline execution contract: /path/to/myproject/.hermes/pipeline.toml
```

This creates a `pipeline.toml` with `assignee = "pipeline"` and capabilities derived from the current phases.yaml.

### Step 2: Install the Pipeline Profile

```bash
pipeline-watch install-profile
```

Output:
```
Installing pipeline profile from /path/to/data/profiles/pipeline/...
Verifying profile installation...
Pipeline profile installed successfully.

Next step: set the assignee in your project contract:
  pipeline-watch init <project> --assignee pipeline
Then verify with:
  pipeline-watch doctor <project>
```

### Step 3: Verify Everything is Wired

```bash
pipeline-watch doctor myproject
```

Output (success):
```
OK: schema_version=1 assignee=pipeline capabilities=['Bash', 'Edit', 'Read', 'Write']
```

Output (missing profile):
```
MISSING: Hermes profile 'pipeline' is not installed, but contract assignee is set to 'pipeline'
Cause: The profile was never installed, or it was removed after install.
Fix: Install the bundled profile with `pipeline-watch install-profile`, or create a custom profile named 'pipeline' with `hermes profile create pipeline`.
```

### Step 4: Run a Tick

```bash
pipeline-watch tick myproject
```

The pipeline tick registers kanban phases with `--assignee pipeline`. Hermes routes the tasks to the installed profile, which runs with SOUL.md in context.

## Reinstalling After SOUL.md Changes

If you've edited the bundled SOUL.md and want to reinstall:

```bash
pipeline-watch install-profile --force
```

## Custom Profiles (Escape Hatch)

The bundled profile is a default. To create a custom profile:

```bash
hermes profile create my-custom-profile --description "My custom pipeline agent"
# Edit SOUL.md at ~/.hermes/profiles/my-custom-profile/SOUL.md
pipeline-watch init myproject --assignee my-custom-profile
```

**Important:** SOUL.md is advisory — it shapes agent behavior through instructions, not enforcement. The pipeline execution contract's `capabilities` field is the only hard boundary (enforced by `doctor` and the tick flow).

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `doctor` reports MISSING profile | Profile not installed | `pipeline-watch install-profile` |
| `doctor` reports DRIFT | phases.yaml added a tool | Edit `pipeline.toml` capabilities, or `pipeline-watch init --force` |
| Tasks not being picked up | Assignee doesn't match profile name | Ensure `assignee` in `pipeline.toml` matches `hermes profile list` name exactly |
| Profile installed but agent doesn't behave correctly | SOUL.md is advisory; model may not follow all instructions | Edit SOUL.md and reinstall with `--force` |
```

- [ ] **Step 2: Commit**

```bash
git add docs/howto-pipeline-profile.md
git commit -m "docs: add pipeline profile wiring walkthrough"
```

---

### Task 10: Regenerate Implementation Plan, Mark TODO-15 Done

**Files:**
- Create: `docs/superpowers/plans/2026-07-10-pipeline-profile.md` (this plan)
- Modify: `TODOS.md` — mark TODO-15 as done

**Interfaces:**
- Consumes: Everything from Tasks 1-9
- Produces: Finalized plan, TODO-15 marked done

- [ ] **Step 1: This IS the regenerated implementation plan**

This document serves as the regenerated plan (superseding `docs/superpowers/plans/2026-07-09-pipeline-execution-contract.md`). The old plan assumed global contract, bundled `pipeline.toml` in configs/, and had `tools` instead of `capabilities`. This plan reflects what actually shipped in the contract module plus the new profile work.

- [ ] **Step 2: Mark TODO-15 done in TODOS.md**

Use the `todos-manager` skill or manually update TODOS.md to mark TODO-15 as `[x]` done with a **Completed:** line referencing this plan.

- [ ] **Step 3: Commit the finalized design doc**

Per CLAUDE.md: "Commit the md files when finalized."

```bash
git add docs/gstack/hyonchoi-main-design-20260710-115644.md
git commit -m "docs: finalize design doc for pipeline profile + SOUL.md (APPROVED)"
```

- [ ] **Step 4: Final commit**

```bash
git add docs/superpowers/plans/2026-07-10-pipeline-profile.md TODOS.md
git commit -m "docs: regenerate impl plan, mark TODO-15 done"
```

---

## Self-Review

### 1. Spec Coverage

- SOUL.md written verbatim before packaging (CEO-1) — **Task 1**
- SOUL.md is skill-agnostic (CEO-4) — verified in Task 1 content (no phase names, no skill names)
- SOUL.md + distribution packaged as installable (Approach B) — **Tasks 1-2**
- `hermes profile install` works on the distribution — **Task 2** (distribution.yaml + SOUL.md)
- `install-profile` command (Amendment A1) — **Task 5**
- `--assignee` flag for init (T1) — **Task 6**
- doctor fail-closed on missing profile (ENG-2) — **Task 7**
- Routing contract: distribution.yaml `name: pipeline` matches assignee value — **Task 2**
- In-package data resolution (Amendment A1 revised) — **Tasks 3-4** (phases.yaml + profiles move into package)
- `load_phases()` migrated off `parent.parent` — **Task 3**
- `install-profile --force` for SOUL.md iteration loop — **Task 5**
- Docs wiring walkthrough (DX-12) — **Task 9**
- Escape hatch documented (DX-13) — **Task 9** (Custom Profiles section)
- Problem/Cause/Fix diagnostics (DX-5) — **Tasks 5-7** (all error messages follow this format)
- "One command" framing softened (CEO-5, DX-1) — **Task 9** (doc says "two commands")
- Regenerate impl plan, supersede old (ENG-5) — **Task 10**
- TODO-15 marked done — **Task 10**
- Success criterion 3 eval — **NOT implemented** (deferred — requires live pipeline run infrastructure not in scope)

### 2. Placeholder Scan

No "TBD", "TODO", "implement later", "add appropriate error handling" found. Every step has concrete code, commands, or content.

### 3. Type Consistency

- `bundled_profile_dir()` returns `Path` — used by `_cmd_install_profile` which passes `str(profile_dir)` to subprocess. Consistent.
- `contract.assignee` is `str` — compared to `"default"` string in doctor. Consistent.
- `PipelineContract` dataclass unchanged — no field changes. Consistent.
- `load_phases()` signature unchanged — `config_path: Path | str | None`. Consistent.
- `init --assignee` value flows from argparse `str | None` through `_cmd_init` to the TOML file. Consistent.

### Gap: Success Criterion 3 Eval

The design calls for a transcript-scan eval to validate "no interactive prompts" (CEO-2, ENG-3). This is out of scope for this iteration — it requires a live pipeline run or a fake-executor harness that captures agent transcripts. The plan delivers the infrastructure (profile, SOUL.md, wiring) that makes the eval possible. The eval itself is a follow-up task.
