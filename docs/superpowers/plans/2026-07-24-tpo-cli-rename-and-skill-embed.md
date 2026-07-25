# tpo CLI — Rename + Skill Embed (TODO-33 + TODO-34) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the CLI entry point from `pipeline-watch`/`hermes-pipeline` to `tpo`, keep one-release deprecation shims for the old names, and embed the `todos-manager` skill as package data with a new `tpo skills install` subcommand.

**Architecture:** Single `[project.scripts]` entry point `tpo = "hermes_pipeline.cli:main"`, with `pipeline-watch`/`hermes-pipeline` as thin wrapper entry points that print a deprecation warning then delegate to the same `main()`. The `todos-manager` skill moves from `skills/todos-manager/` to `hermes_pipeline/data/skills/todos-manager/` so it ships inside the wheel. A new `skills` argparse subparser adds `tpo skills install` which copies the bundled directory to `~/.claude/skills/`, `~/.agents/skills/`, or both. Path resolution for bundled data (skill files and the existing Hermes SOUL.md profile) is unified behind one `_resolve_bundled_dir()` helper in `contract.py`, with a real fallback to a temp directory when `importlib.resources` returns a non-filesystem `Traversable`.

**Tech Stack:** Python 3.12, argparse, `importlib.resources`, hatchling, uv, pytest, `pytest-mock`.

## Global Constraints

- Strict rename only for TODO-33 — no API surface changes, no new subcommands beyond `skills`.
- TODO-34 must package non-Python files (SKILL.md + sections/) as package data via hatchling.
- `uv tool install` distribution model must work end-to-end after the change.
- The internal Python package name `hermes_pipeline` stays unchanged (only the console script changes).
- Python 3.12+, hatchling backend, uv-managed.
- Historical `CHANGELOG.md` entries and past gstack design docs are NOT rewritten — only forward-looking docs (README, current `CLAUDE.md`, help text, current docs/*.md) get the new name.
- `scripts/install-todos-manager.sh` is deleted; docs pointing to it redirect to `tpo skills install`.
- Version bump (VERSION, pyproject.toml, uv.lock, CHANGELOG.md) happens LAST, after `uv sync` reflects all other changes — see project `CLAUDE.md`.

---

## File Structure

**New files:**
- `hermes_pipeline/data/skills/todos-manager/SKILL.md` — moved from `skills/todos-manager/SKILL.md`
- `hermes_pipeline/data/skills/todos-manager/sections/*.md` — moved from `skills/todos-manager/sections/*.md` (9 files: acceptance-scenarios, auto-research, convert-mode-b, entry-boundary, error-messages, id-assignment, list, revise, schema)
- `hermes_pipeline/data/__init__.py` — empty, makes `data/` a traversable package anchor
- `hermes_pipeline/deprecated_entry.py` — shared deprecation-shim entry points for `pipeline-watch` and `hermes-pipeline`
- `tests/test_skills_install.py` — new tests for `tpo skills install`
- `tests/test_deprecation_shims.py` — new tests for the deprecation shims

**Modified files:**
- `pyproject.toml` — `[project.scripts]` section
- `hermes_pipeline/contract.py` — add `_resolve_bundled_dir()`, retrofit `bundled_profile_dir()`
- `hermes_pipeline/cli.py` — `prog="tpo"`, new `skills` subparser + `_cmd_skills_install`, string replacements, bootstrap config bypass
- `tests/test_harness.py`, `tests/test_contract.py`, `tests/test_recover_counter_cli.py`, `tests/test_cli_entrypoint.py`, `tests/test_tick_subcommand.py`, `tests/test_cli.py`, `tests/test_cli_contract.py`, `tests/test_ship.py` — string replacements
- `CLAUDE.md`, `README.md`, `docs/*.md` (non-gstack, non-historical) — string replacements + path updates
- `scripts/install-todos-manager.sh` — deleted
- `tests/skill-test-environment/README.md`, `tests/skill-test-environment/conftest.py` — path updates if they reference `skills/todos-manager/`

**Why `data/` and not `skills/` at the package root:** the skill files are markdown data, not Python submodules. Placing them under `data/` avoids needing `__init__.py` in every subdirectory and preserves natural directory names (hyphens). One `__init__.py` at the data root is the only Python package requirement.

---

### Task 1: Move todos-manager skill into package data

**Files:**
- Create: `hermes_pipeline/data/__init__.py`
- Move: `skills/todos-manager/SKILL.md` → `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- Move: `skills/todos-manager/sections/*.md` → `hermes_pipeline/data/skills/todos-manager/sections/*.md`
- Test: `tests/test_skills_install.py` (new — package-data discoverability check only in this task)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `hermes_pipeline/data/skills/todos-manager/` on disk, importable as `importlib.resources.files("hermes_pipeline.data")`. Later tasks (`_resolve_bundled_dir`, `_cmd_skills_install`) depend on this exact path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skills_install.py
"""Tests for the tpo skills install subcommand and bundled skill data."""
from __future__ import annotations

from importlib.resources import files


def test_todos_manager_skill_is_packaged_data():
    """SKILL.md and sections/ are importable via importlib.resources."""
    data_root = files("hermes_pipeline.data")
    skill_md = data_root.joinpath("skills", "todos-manager", "SKILL.md")
    assert skill_md.is_file()
    sections_dir = data_root.joinpath("skills", "todos-manager", "sections")
    section_names = {p.name for p in sections_dir.iterdir()}
    assert "schema.md" in section_names
    assert "id-assignment.md" in section_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_skills_install.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_pipeline.data'` (the directory doesn't exist yet).

- [ ] **Step 3: Move the files and create the package anchor**

```bash
mkdir -p hermes_pipeline/data/skills
git mv skills/todos-manager hermes_pipeline/data/skills/todos-manager
touch hermes_pipeline/data/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_skills_install.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/data tests/test_skills_install.py
git commit -m "feat: move todos-manager skill into hermes_pipeline package data"
```

---

### Task 2: Add `_resolve_bundled_dir()` helper and retrofit `bundled_profile_dir()`

**Files:**
- Modify: `hermes_pipeline/contract.py:170-183` (the existing `bundled_profile_dir()` function)
- Test: `tests/test_contract.py` (new test class alongside existing tests)

**Interfaces:**
- Consumes: `hermes_pipeline.data` package from Task 1.
- Produces: `_resolve_bundled_dir(*parts: str) -> Path` in `hermes_pipeline/contract.py`. Task 3 (`_cmd_skills_install`) imports and calls this as `_resolve_bundled_dir("skills", "todos-manager")`.

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_contract.py

from hermes_pipeline.contract import _resolve_bundled_dir


class TestResolveBundledDir:
    def test_resolves_real_filesystem_path(self):
        """Normal (non-zip) install: returns a real, existing directory."""
        result = _resolve_bundled_dir("skills", "todos-manager")
        assert result.is_dir()
        assert (result / "SKILL.md").is_file()

    def test_falls_back_to_tempdir_for_non_filesystem_traversable(self, mocker):
        """Zip-wheel install: Path(traversable) raises, falls back to a real temp copy."""
        import hermes_pipeline.contract as contract_mod

        class FakeTraversable:
            def __fspath__(self):
                raise NotImplementedError("not a real filesystem path")

            def iterdir(self):
                return iter([])

            def is_dir(self):
                return True

            name = "todos-manager"

        mocker.patch.object(
            contract_mod, "_bundled_data_root",
            return_value=FakeTraversable(),
        )
        result = _resolve_bundled_dir("skills", "todos-manager")
        assert result.is_dir()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract.py::TestResolveBundledDir -v`
Expected: FAIL with `ImportError: cannot import name '_resolve_bundled_dir'`

- [ ] **Step 3: Write the implementation**

Replace the existing `bundled_profile_dir()` function (`hermes_pipeline/contract.py:170-183`) with:

```python
def _bundled_data_root():
    """Return the importlib.resources Traversable for hermes_pipeline.data.

    Isolated as its own function so tests can mock it to simulate a
    non-filesystem (zip-wheel) install without patching importlib itself.
    """
    from importlib.resources import files
    return files("hermes_pipeline.data")


def _copy_traversable_to_tempdir(traversable) -> Path:
    """Recursively copy a non-filesystem Traversable into a real tempdir.

    Used when importlib.resources yields a Traversable that isn't backed by
    a plain filesystem path (e.g. a zip-wheel install) — shutil.copytree and
    Path() operations need a real directory to work against.
    """
    import shutil
    import tempfile

    dest_root = Path(tempfile.mkdtemp(prefix="hermes_pipeline_bundled_"))

    def _copy_node(node, dest: Path) -> None:
        if node.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            for child in node.iterdir():
                _copy_node(child, dest / child.name)
        else:
            dest.write_bytes(node.read_bytes())

    _copy_node(traversable, dest_root)
    return dest_root


def _resolve_bundled_dir(*parts: str) -> Path:
    """Resolve a directory under hermes_pipeline/data/ to a real filesystem Path.

    Resolves package-relative so it works whether running from a checkout
    or from an installed wheel. For zip-wheel installs, importlib.resources
    returns a Traversable that isn't a real filesystem path — in that case
    the directory is copied to a temp directory so callers can rely on a
    plain Path (shutil.copytree, Path.exists, etc.) unconditionally.
    """
    traversable = _bundled_data_root().joinpath(*parts)
    try:
        return Path(traversable)
    except (TypeError, NotImplementedError):
        return _copy_traversable_to_tempdir(traversable)


def bundled_profile_dir() -> Path:
    """Return the path to the directory containing the bundled pipeline SOUL.md.

    Resolves package-relative so it works whether running from a checkout
    or from an installed wheel.
    """
    return _resolve_bundled_dir("hermes-identity", "pipeline")
```

Note: this changes the path passed to `_resolve_bundled_dir` from `("data", "hermes-identity", "pipeline")` to `("hermes-identity", "pipeline")` because `_bundled_data_root()` now anchors at `hermes_pipeline.data` directly (the same package Task 1 created), not `hermes_pipeline`. Verify `hermes_pipeline/data/hermes-identity/pipeline/SOUL.md` already exists at that relative location under `data/`; if the existing bundled profile currently lives at `hermes_pipeline/data/hermes-identity/pipeline/SOUL.md` (per the pre-existing docstring's `"data", "hermes-identity", "pipeline"` joinpath against `files("hermes_pipeline")`), no file move is needed — only the anchor package changes from `"hermes_pipeline"` to `"hermes_pipeline.data"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_contract.py::TestResolveBundledDir -v`
Expected: PASS

- [ ] **Step 5: Run full contract test suite to check for regressions**

Run: `uv run pytest tests/test_contract.py -v`
Expected: All PASS (existing `bundled_profile_dir()` callers unaffected — same return contract, real `Path` pointing at an existing directory)

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/contract.py tests/test_contract.py
git commit -m "feat: add _resolve_bundled_dir helper with non-filesystem fallback"
```

---

### Task 3: Add `tpo skills install` subcommand

**Files:**
- Modify: `hermes_pipeline/cli.py` — add `skills` subparser in `build_parser()` (after the `test` subparser, `hermes_pipeline/cli.py:296-326`), add `_cmd_skills_install`, add bootstrap bypass in `main()` (`hermes_pipeline/cli.py:1209-1245`)
- Test: `tests/test_skills_install.py` (append to file from Task 1)

**Interfaces:**
- Consumes: `_resolve_bundled_dir` from `hermes_pipeline.contract` (Task 2).
- Produces: `_cmd_skills_install(args, config) -> int` in `cli.py`, registered on the `skills install` subparser. `args.target` (`"codex" | "claude" | "all"`, default `"claude"`), `args.scope` (`"user" | "project"`, default `"user"`), `args.force` (bool flag, `store_true`).

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_skills_install.py

from pathlib import Path

import pytest

from hermes_pipeline.cli import _cmd_skills_install, build_parser
from hermes_pipeline.config import Config


class FakeArgs:
    def __init__(self, **kwargs):
        kwargs.setdefault("target", "claude")
        kwargs.setdefault("scope", "user")
        kwargs.setdefault("force", False)
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestSkillsInstallParsing:
    def test_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["skills", "install"])
        assert args.target == "claude"
        assert args.scope == "user"
        assert args.force is False
        assert hasattr(args, "func")

    def test_target_all_scope_project(self):
        parser = build_parser()
        args = parser.parse_args(["skills", "install", "--target", "all", "--scope", "project"])
        assert args.target == "all"
        assert args.scope == "project"

    def test_invalid_target_rejected(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["skills", "install", "--target", "bogus"])


class TestCmdSkillsInstall:
    def test_installs_to_claude_user_target(self, tmp_path, mocker, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        result = _cmd_skills_install(FakeArgs(target="claude", scope="user"), config)
        assert result == 0
        installed = tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md"
        assert installed.is_file()

    def test_reinstall_overwrites_without_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        target_dir = tmp_path / ".claude" / "skills" / "todos-manager"
        target_dir.mkdir(parents=True)
        (target_dir / "SKILL.md").write_text("stale content")

        result = _cmd_skills_install(FakeArgs(target="claude", scope="user"), config)

        assert result == 0
        content = (target_dir / "SKILL.md").read_text()
        assert content != "stale content"

    def test_creates_target_directory_if_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")
        assert not (tmp_path / ".claude").exists()

        result = _cmd_skills_install(FakeArgs(target="claude", scope="user"), config)

        assert result == 0
        assert (tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_target_codex_installs_to_agents_skills(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(FakeArgs(target="codex", scope="user"), config)

        assert result == 0
        assert (tmp_path / ".agents" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_target_all_installs_to_both(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(FakeArgs(target="all", scope="user"), config)

        assert result == 0
        assert (tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md").is_file()
        assert (tmp_path / ".agents" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_scope_project_uses_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        result = _cmd_skills_install(FakeArgs(target="claude", scope="project"), config)

        assert result == 0
        assert (tmp_path / ".claude" / "skills" / "todos-manager" / "SKILL.md").is_file()

    def test_permission_denied_produces_structured_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        def _raise_permission_error(*a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr("shutil.copytree", _raise_permission_error)

        result = _cmd_skills_install(FakeArgs(target="claude", scope="user"), config)

        out = capsys.readouterr().out
        assert result == 1
        assert "Problem:" in out
        assert "Cause:" in out
        assert "Fix:" in out

    def test_target_all_partial_failure_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = Config(projects_dir=tmp_path / "projects")

        real_copytree = __import__("shutil").copytree
        call_count = {"n": 0}

        def _flaky_copytree(src, dst, **kw):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise PermissionError("denied on second target")
            return real_copytree(src, dst, **kw)

        monkeypatch.setattr("shutil.copytree", _flaky_copytree)

        result = _cmd_skills_install(FakeArgs(target="all", scope="user"), config)

        assert result == 1
        out = capsys.readouterr().out
        assert "claude" in out.lower()
        assert "codex" in out.lower() or "agents" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_skills_install.py -v`
Expected: FAIL with `ImportError: cannot import name '_cmd_skills_install'` and `SystemExit` failures on unrecognized `skills` subcommand.

- [ ] **Step 3: Write the implementation**

Add to `hermes_pipeline/cli.py`, in `build_parser()` right after the `test` subparser block (after line 326, before `return parser`):

```python
    # skills: bootstrap bundled skills into user/project skill directories
    skills_parser = subparsers.add_parser(
        "skills",
        help="Manage bundled agent skills (e.g. todos-manager)",
    )
    skills_subparsers = skills_parser.add_subparsers(dest="skills_command", required=True)

    skills_install_parser = skills_subparsers.add_parser(
        "install",
        help="Install the bundled todos-manager skill",
    )
    skills_install_parser.add_argument(
        "--target", choices=["codex", "claude", "all"], default="claude",
        help="Which skill directory convention to install into (default: claude)",
    )
    skills_install_parser.add_argument(
        "--scope", choices=["user", "project"], default="user",
        help="Install under the user's home directory or the current project (default: user)",
    )
    skills_install_parser.add_argument(
        "--force", action="store_true",
        help="Scripting/non-interactive marker; install always overwrites regardless of this flag",
    )
    skills_install_parser.set_defaults(func=_cmd_skills_install)
```

Add the command implementation, near `_cmd_install_profile` (after its closing, before `_cmd_test`):

```python
_SKILLS_INSTALL_TARGET_DIRNAMES = {
    "claude": ".claude/skills",
    "codex": ".agents/skills",
}


def _skills_install_targets(target: str, scope: str) -> list[tuple[str, Path]]:
    """Resolve (target_name, install_dir) pairs for --target/--scope."""
    base = Path.home() if scope == "user" else Path.cwd()
    names = ["claude", "codex"] if target == "all" else [target]
    return [(name, base / _SKILLS_INSTALL_TARGET_DIRNAMES[name]) for name in names]


def _cmd_skills_install(args, config: Config) -> int:
    """Handle 'skills install' subcommand — copy the bundled todos-manager skill.

    Copies hermes_pipeline/data/skills/todos-manager/ to one or both of
    ~/.claude/skills/todos-manager/ and ~/.agents/skills/todos-manager/
    (or their project-scoped equivalents), always overwriting existing
    files (idempotent reinstall).

    Exit codes: 0 all targets installed, 1 source missing / any target failed.
    """
    from .contract import _resolve_bundled_dir

    source = _resolve_bundled_dir("skills", "todos-manager")
    if not source.is_dir():
        print(f"Problem: bundled todos-manager skill not found at {source}.")
        print("Cause: the installed package is missing its bundled skill data.")
        print("Fix: reinstall with `uv tool install hermes-pipeline` (or `uv sync` in a checkout).")
        return 1

    targets = _skills_install_targets(args.target, args.scope)
    any_failed = False
    for name, install_dir in targets:
        dest = install_dir / "todos-manager"
        try:
            install_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, dest, dirs_exist_ok=True)
            print(f"OK ({name}): installed todos-manager to {dest}")
        except PermissionError as e:
            any_failed = True
            print(f"Problem ({name}): permission denied writing to {dest}.")
            print(f"Details: {e}")
            print(f"Cause: the current user lacks write access to {install_dir}.")
            print(f"Fix: check permissions on {install_dir}, or rerun with --scope project.")
        except OSError as e:
            any_failed = True
            print(f"Problem ({name}): failed to install todos-manager to {dest}.")
            print(f"Details: {e}")
            print("Cause: an OS-level error occurred during copy.")
            print(f"Fix: inspect {install_dir} and retry.")

    return 1 if any_failed else 0
```

Add the bootstrap bypass in `main()` (`hermes_pipeline/cli.py:1209-1245`) — `skills install` doesn't need pipeline runtime `Config` (state dir, lock dir), so skip `Config.from_env()` for it:

```python
def main(argv: list[str] | None = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        argv: Command-line arguments (default: sys.argv[1:]).

    Returns:
        Exit code (0 on success, 2 on error).
    """
    verbose, debug, remaining = _strip_global_flags(argv)

    parser = build_parser()
    args = parser.parse_args(remaining)

    # Bootstrap subcommands (file-copy only) don't need pipeline runtime
    # config (state dir, lock dir, projects dir) — skip Config.from_env()
    # so they work even when that env isn't configured yet.
    if getattr(args, "command", None) == "skills":
        if hasattr(args, "func"):
            return args.func(args, None)
        parser.parse_args(remaining + ["--help"])
        return 0

    config = Config.from_env()

    log_path = config.state_dir / config.log_file_subpath
    if debug:
        configure_logging(log_path, config.log_retention_days, level=logging.DEBUG)
        vlog.setLevel(logging.INFO)
    elif verbose:
        configure_logging(log_path, config.log_retention_days, level=logging.INFO)
        vlog.setLevel(logging.INFO)
    else:
        configure_logging(log_path, config.log_retention_days)

    if hasattr(args, "func"):
        return args.func(args, config)
    else:
        parser.print_help()
        return 0
```

Since `_cmd_skills_install(args, config)` is called with `config=None` from this bypass path, and its body never touches `config` (it only uses `args.target`/`args.scope`/`args.force`), the signature is kept as `(args, config)` to match every other subcommand handler's calling convention — no behavior depends on `config` being non-`None` for this command. Update the type-check-facing signature comment only if the project runs mypy/pyright in strict mode; this project doesn't (ruff only), so `config: Config` stays as the declared type for consistency with sibling handlers.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_skills_install.py -v`
Expected: PASS

- [ ] **Step 5: Run full CLI test suite to check for regressions**

Run: `uv run pytest tests/test_cli.py tests/test_cli_contract.py tests/test_tick_subcommand.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_skills_install.py
git commit -m "feat: add tpo skills install subcommand"
```

---

### Task 4: Rename console script to `tpo` + add deprecation shims

**Files:**
- Modify: `pyproject.toml` — `[project.scripts]`
- Create: `hermes_pipeline/deprecated_entry.py`
- Modify: `hermes_pipeline/cli.py:216` (`prog="pipeline-watch"` → `prog="tpo"`)
- Test: `tests/test_deprecation_shims.py` (new)

**Interfaces:**
- Consumes: `hermes_pipeline.cli.main` (unchanged signature: `main(argv: list[str] | None = None) -> int`).
- Produces: `pipeline_watch_deprecated() -> int` and `hermes_pipeline_deprecated() -> int` in `hermes_pipeline/deprecated_entry.py`, wired as console-script targets.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deprecation_shims.py
"""Tests for the pipeline-watch/hermes-pipeline deprecation shims (TODO-33)."""
from __future__ import annotations

import sys

import pytest

from hermes_pipeline.deprecated_entry import (
    hermes_pipeline_deprecated,
    pipeline_watch_deprecated,
)


class TestDeprecationShims:
    def test_pipeline_watch_prints_warning_and_dispatches(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pipeline-watch", "--version"])
        result = pipeline_watch_deprecated()
        captured = capsys.readouterr()
        assert "pipeline-watch" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "tpo" in captured.err
        assert result == 0

    def test_hermes_pipeline_prints_warning_and_dispatches(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["hermes-pipeline", "--version"])
        result = hermes_pipeline_deprecated()
        captured = capsys.readouterr()
        assert "hermes-pipeline" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "tpo" in captured.err
        assert result == 0

    def test_pipeline_watch_forwards_args_unchanged(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pipeline-watch", "recover-counter", "myproject"])
        # No config env set up -> _resolve_project_dir logs "project not found" and
        # returns exit code 2, proving the real subcommand ran (not a no-op).
        result = pipeline_watch_deprecated()
        assert result == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_deprecation_shims.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_pipeline.deprecated_entry'`

- [ ] **Step 3: Write the implementation**

```python
# hermes_pipeline/deprecated_entry.py
"""Deprecated console-script entry points for the pre-tpo CLI names.

Kept for one release after the TODO-33 rename so existing installs of
pipeline-watch/hermes-pipeline keep working while users migrate to `tpo`.
Remove both this module and their [project.scripts] entries in the
following version bump.
"""
from __future__ import annotations

import sys

from .cli import main


def _deprecated_main(old_name: str) -> int:
    print(
        f"`{old_name}` is deprecated, use `tpo` instead. "
        f"This alias will be removed in a future release.",
        file=sys.stderr,
    )
    return main()


def pipeline_watch_deprecated() -> int:
    return _deprecated_main("pipeline-watch")


def hermes_pipeline_deprecated() -> int:
    return _deprecated_main("hermes-pipeline")
```

Update `pyproject.toml` `[project.scripts]`:

```toml
[project.scripts]
tpo = "hermes_pipeline.cli:main"
pipeline-watch = "hermes_pipeline.deprecated_entry:pipeline_watch_deprecated"
hermes-pipeline = "hermes_pipeline.deprecated_entry:hermes_pipeline_deprecated"
```

Update `hermes_pipeline/cli.py:216`:

```python
        prog="tpo",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_deprecation_shims.py -v`
Expected: PASS

- [ ] **Step 5: Sync uv and reinstall console scripts**

Run: `uv sync`
Expected: completes without error; `.venv/bin/tpo`, `.venv/bin/pipeline-watch`, `.venv/bin/hermes-pipeline` all exist.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml hermes_pipeline/deprecated_entry.py hermes_pipeline/cli.py tests/test_deprecation_shims.py uv.lock
git commit -m "feat: rename console script to tpo, add deprecation shims for old names"
```

---

### Task 5: Update source string references (`pipeline-watch` → `tpo`)

**Files:**
- Modify: `hermes_pipeline/cli.py:175,694,703,1063,1182,1184`
- Modify: `hermes_pipeline/contract.py:76,94,128,144`
- Modify: `hermes_pipeline/harness.py:43,284`
- Modify: `hermes_pipeline/ship.py:494`
- Test: existing test files updated in Task 6 assert on these strings — do this task first so those updates land against real output.

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new — pure string replacement, no signature changes.

- [ ] **Step 1: Replace all in-source references**

```bash
grep -rn "pipeline-watch" hermes_pipeline/*.py
```

Replace every occurrence of the literal string `pipeline-watch` with `tpo` in:
- `hermes_pipeline/cli.py` (help text, error messages, print statements — lines 175, 694, 703, 1063, 1182, 1184)
- `hermes_pipeline/contract.py` (error messages — lines 76, 94, 128, 144)
- `hermes_pipeline/harness.py` (comment + fixture text — lines 43, 284)
- `hermes_pipeline/ship.py` (error message — line 494)

Example (`hermes_pipeline/cli.py:175`):
```python
        raise argparse.ArgumentTypeError(
            f"todo_id must be a number (you provided '{value}'). "
            f"Example: tpo merge myproject 123"
        )
```

Example (`hermes_pipeline/cli.py:1182,1184`):
```python
    print("  tpo init <project> --assignee pipeline")
    print("Then verify with:")
    print("  tpo doctor <project>")
```

Do the same substitution for every remaining match. Do not touch `docs/old-code/pipeline_watcher.py` (historical) or any file under `docs/gstack/` or `docs/superpowers/`.

- [ ] **Step 2: Verify no source references remain**

Run: `grep -rn "pipeline-watch" hermes_pipeline/*.py`
Expected: no output (all replaced).

- [ ] **Step 3: Run ruff and the full test suite to catch breakage early**

Run: `uv run ruff check hermes_pipeline/`
Expected: no new errors.

Run: `uv run pytest -x`
Expected: failures only in tests that assert the literal old string (fixed in Task 6) — everything else passes.

- [ ] **Step 4: Commit**

```bash
git add hermes_pipeline/cli.py hermes_pipeline/contract.py hermes_pipeline/harness.py hermes_pipeline/ship.py
git commit -m "refactor: replace pipeline-watch references with tpo in source"
```

---

### Task 6: Update test references

**Files:**
- Modify: `tests/test_harness.py:246`
- Modify: `tests/test_contract.py:54`
- Modify: `tests/test_recover_counter_cli.py:11`
- Modify: `tests/test_cli_entrypoint.py:9` (docstring + rename test function)
- Modify: `tests/test_tick_subcommand.py:40`
- Modify: `tests/test_cli.py:14,38`
- Modify: `tests/test_cli_contract.py:258`
- Modify: `tests/test_ship.py:128,167` (these assert on `pyproject.toml`'s `[project] name`, which stays `hermes-pipeline` — verify before changing, see Step 1)

**Interfaces:**
- Consumes: the renamed `prog="tpo"` from Task 4, the string replacements from Task 5.
- Produces: nothing new — assertions updated to match new behavior.

- [ ] **Step 1: Inspect test_ship.py assertions before touching them**

`tests/test_ship.py:128,167` assert `'[project]\nname = "hermes-pipeline"\nversion = "0.3.3"\n'` — this is the **package name** field (`hermes-pipeline`), which the plan's constraints say stays unchanged (only the console script/entry-point name changes to `tpo`). Confirm these two lines are testing the package-name field, not a console-script name, and leave them untouched.

Run: `grep -n -B3 "hermes-pipeline" tests/test_ship.py`
Expected: confirms these are `pyproject.toml` fixture content for the `[project] name` field, unrelated to the console script rename — no change needed here.

- [ ] **Step 2: Update `tests/test_cli.py`**

```python
        assert parser.prog == "tpo"
```
and
```python
        sys.argv = ['tpo']
```

- [ ] **Step 3: Update `tests/test_cli_contract.py:258`**

```python
        assert "tpo init" in capsys.readouterr().out
```

- [ ] **Step 4: Update `tests/test_contract.py:54`**

```python
    with pytest.raises(ContractMissingError, match="tpo init"):
```

- [ ] **Step 5: Update `tests/test_recover_counter_cli.py:11`**

```python
    """Tests for tpo recover-counter <project>."""
```

- [ ] **Step 6: Update `tests/test_cli_entrypoint.py`**

```python
"""Tests for CLI entrypoints."""

import re
import subprocess
import sys


def test_cli_entrypoint_module_runs():
    """Verify the hermes_pipeline.cli module is runnable as a CLI entry point."""
    result = subprocess.run(
        [sys.executable, "-m", "hermes_pipeline.cli", "--version"],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert re.search(r"\d+\.\d+\.\d+", result.stdout), f"stdout: {result.stdout}"
```

- [ ] **Step 7: Update `tests/test_tick_subcommand.py:40`**

```python
    """Tests for tpo tick (scan loop)."""
```

- [ ] **Step 8: Update `tests/test_harness.py:246`**

```python
written to events.jsonl, so `tpo test` is no longer silent mid-run."""
```

- [ ] **Step 9: Run the full test suite**

Run: `uv run pytest -v`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add tests/test_harness.py tests/test_contract.py tests/test_recover_counter_cli.py tests/test_cli_entrypoint.py tests/test_tick_subcommand.py tests/test_cli.py tests/test_cli_contract.py
git commit -m "test: update test references from pipeline-watch to tpo"
```

---

### Task 7: Delete install script, update its references, update skill-test-environment path references

**Files:**
- Delete: `scripts/install-todos-manager.sh`
- Modify: `README.md:21,63,94,97` — redirect to `tpo skills install`, update path references from `skills/todos-manager/` to the packaged location
- Modify: `CLAUDE.md:48,50` — update skill source path
- Modify: `tests/skill-test-environment/README.md` — update path references if present
- Modify: `tests/skill-test-environment/conftest.py` — update path references if present

**Interfaces:**
- Consumes: `tpo skills install` from Task 3 (must exist before docs reference it).
- Produces: nothing new — documentation/reference updates only.

- [ ] **Step 1: Delete the install script**

```bash
git rm scripts/install-todos-manager.sh
```

- [ ] **Step 2: Update `README.md`**

Line 21 — change `Install via `scripts/install-todos-manager.sh`.` to `Install via `tpo skills install`.`

Line 63 — remove the `[Install TODOS Manager](scripts/install-todos-manager.sh)` docs-table row entirely (the script no longer exists); replace its content cell with a pointer to `tpo skills install --help`.

Lines 94-97:
```markdown
tpo skills install --target all
```
```markdown
This installs `todos-manager` to `~/.claude/skills/todos-manager/` and `~/.agents/skills/todos-manager/`.
```

Line 60 — update the SKILL.md link path: `[TODOS Manager skill](hermes_pipeline/data/skills/todos-manager/SKILL.md)`.

- [ ] **Step 3: Update `CLAUDE.md`**

```markdown
- Skill source: `hermes_pipeline/data/skills/todos-manager/SKILL.md`. Install via `tpo skills install --target all` to copy to `~/.claude/skills/todos-manager/` and/or `~/.agents/skills/todos-manager/`.
```

- [ ] **Step 4: Check and update skill-test-environment references**

Run: `grep -rn "skills/todos-manager\|install-todos-manager" tests/skill-test-environment/`

If any references to the old `skills/todos-manager/` path exist, update them to `hermes_pipeline/data/skills/todos-manager/`. If `tests/skill-test-environment/` tests the skill logic independently of the package location (via its own `demo-project/` fixtures, not the moved SKILL.md), no change is needed there — verify by reading `tests/skill-test-environment/conftest.py` for any hardcoded path before editing.

- [ ] **Step 5: Verify no remaining references to the deleted script or old skill path in forward-looking docs**

Run: `grep -rln "install-todos-manager.sh\|skills/todos-manager" README.md CLAUDE.md docs/*.md tests/skill-test-environment/ 2>/dev/null | grep -v docs/gstack`
Expected: no output, or only files intentionally left alone with a documented reason.

- [ ] **Step 6: Commit**

```bash
git add -A README.md CLAUDE.md tests/skill-test-environment/
git rm -f scripts/install-todos-manager.sh 2>/dev/null || true
git commit -m "docs: redirect install-todos-manager.sh references to tpo skills install"
```

---

### Task 8: Update remaining docs (README + docs/*.md) — find-and-replace `pipeline-watch`/`hermes-pipeline` → `tpo`

**Files:**
- Modify: `README.md` (remaining occurrences beyond Task 7's edits)
- Modify: `docs/checklist-harness-production-coverage.md`, `docs/explanation-multi-project-scan.md`, `docs/explanation-pipeline-contract.md`, `docs/howto-agent-skills-profile.md`, `docs/howto-approve-and-ship.md`, `docs/howto-config-toml.md`, `docs/howto-debugging-and-recovery.md`, `docs/howto-mock-integration-test-harness.md`, `docs/howto-multi-project-setup.md`, `docs/howto-pipeline-contract.md`, `docs/howto-pipeline-profile.md`, `docs/howto-pipeline-tick.md`, `docs/howto-prompt-sha-mismatch.md`, `docs/howto-troubleshoot-state-migration.md`, `docs/reference-cli.md`, `docs/reference-counter.md`, `docs/reference-kanban-as-scheduler.md`, `docs/tutorial-getting-started.md`, `docs/tutorial-multi-project-scan.md`

**Interfaces:** none — documentation-only edits.

**Explicitly out of scope (do not edit):** `docs/pipeline-modularization-plan.md` (historical design doc predating this rename), anything under `docs/gstack/` or `docs/superpowers/`, and `CHANGELOG.md` history prior to the new entry added in Task 9.

- [ ] **Step 1: List remaining occurrences**

```bash
grep -rln "pipeline-watch\|hermes-pipeline" README.md docs/*.md \
  | grep -v "docs/pipeline-modularization-plan.md"
```

- [ ] **Step 2: Replace in each listed file**

For each file, replace:
- `pipeline-watch` → `tpo` (command name in prose and code blocks, e.g. `uv run pipeline-watch tick` → `uv run tpo tick`)
- `hermes-pipeline test` → `tpo test` (the mock-harness subcommand — it's the same binary now, not a separate script)
- Two-command framing that referred to `pipeline-watch` and `hermes-pipeline` as separate entry points (e.g. `docs/reference-cli.md:3,5,6,8` "Complete reference for `pipeline-watch` and `hermes-pipeline` subcommands") collapses to a single `tpo` reference, since both now dispatch through one binary:

```markdown
Complete reference for `tpo` subcommands.

- `uv run tpo <command> [args]` — Production pipeline orchestration (tick, merge, approve, ...)
- `uv run tpo test [args]` — Mock integration test harness

## tpo Global Flags
```

and further down:
```markdown
## tpo test
```

- Sample version-output text (`docs/tutorial-getting-started.md:29`):
```
tpo 0.5.10
```
(match whatever `VERSION` is at the time this task runs — check `cat VERSION` before editing, since Task 9 hasn't bumped it yet in this task's scope).

- README section headers/prose referencing "pipeline-watched project" stay grammatically — e.g. `docs/explanation-multi-project-scan.md:67` "a directory is a pipeline-watched project" describes the *concept*, not the command; leave descriptive prose like this alone and only replace literal command invocations (`pipeline-watch tick`, `pipeline-watch install-profile`, etc.). Use judgment file-by-file: if `pipeline-watch` appears as a backticked command or `uv run pipeline-watch ...` invocation, replace it; if it's used as an adjective describing the system in flowing prose, leave it.

- [ ] **Step 3: Verify no command-invocation references remain**

```bash
grep -rn '`pipeline-watch \|`hermes-pipeline \|uv run pipeline-watch\|uv run hermes-pipeline' README.md docs/*.md \
  | grep -v "docs/pipeline-modularization-plan.md"
```
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/*.md
git commit -m "docs: replace pipeline-watch/hermes-pipeline command references with tpo"
```

---

### Task 9: README + CHANGELOG breaking-change note, then version bump (ALL 4 files, last)

**Files:**
- Modify: `README.md` (breaking-change callout, if not already covered by Task 8's edits)
- Modify: `CHANGELOG.md` — new entry
- Modify: `VERSION`
- Modify: `pyproject.toml` — `version` field
- Modify: `uv.lock` — regenerated via `uv sync`, not hand-edited

**Interfaces:** none — release metadata only. Per project `CLAUDE.md`, this must be the last task, after `uv sync` reflects every other change, so only one `uv sync` run is needed here.

- [ ] **Step 1: Confirm current version**

Run: `cat VERSION && grep '^version' pyproject.toml`
Expected: both show `0.5.10`.

- [ ] **Step 2: Bump VERSION**

```bash
echo "0.6.0" > VERSION
```

(Minor bump: this is a breaking CLI rename, matching semver's "backward incompatible" signal even under 1.0 pre-release conventions the repo already treats minor bumps as feature/breaking-boundary — consistent with the existing `0.5.9 → 0.5.10` pattern being a patch-level change and this being larger in scope.)

- [ ] **Step 3: Update `pyproject.toml`**

```toml
version = "0.6.0"
```

- [ ] **Step 4: Regenerate uv.lock**

Run: `uv sync`
Expected: completes; `uv.lock`'s `hermes-pipeline` entry now shows `version = "0.6.0"`.

- [ ] **Step 5: Add CHANGELOG entry**

Insert at the top of `CHANGELOG.md`, right after the format-description preamble:

```markdown
## [0.6.0] - 2026-07-24

### Changed
- **BREAKING:** CLI renamed from `pipeline-watch`/`hermes-pipeline` to `tpo`. Reinstall with `uv tool install hermes-pipeline` to get the new name. `pipeline-watch` and `hermes-pipeline` still work for one release — each prints a deprecation warning to stderr before dispatching, and will be removed in the next version bump.
- The `todos-manager` skill is now bundled as package data (`hermes_pipeline/data/skills/todos-manager/`) instead of living outside the package at `skills/todos-manager/`. `uv tool install` now works end-to-end without a manual clone step.

### Added
- `tpo skills install [--target {codex|claude|all}] [--scope {user|project}] [--force]` — installs the bundled `todos-manager` skill to `~/.claude/skills/`, `~/.agents/skills/`, or both.

### Removed
- `scripts/install-todos-manager.sh` — superseded by `tpo skills install`.
```

- [ ] **Step 6: Verify all 4 files are in sync**

```bash
cat VERSION
grep '^version' pyproject.toml
grep -A1 'name = "hermes-pipeline"' uv.lock
head -10 CHANGELOG.md
```
Expected: `VERSION`, `pyproject.toml`, and `uv.lock`'s `hermes-pipeline` entry all show `0.6.0`; `CHANGELOG.md`'s top entry is `## [0.6.0]`.

- [ ] **Step 7: Commit**

```bash
git add VERSION pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore: bump version 0.5.10 → 0.6.0 for tpo CLI rename"
```

---

### Task 10: Full verification pass

**Files:** none modified — verification only.

**Interfaces:** none.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: all tests PASS.

- [ ] **Step 2: Run ruff**

Run: `uv run ruff check .`
Expected: no errors (respecting the existing `exclude = ["docs/old-code"]`).

- [ ] **Step 3: Verify `tpo --help`**

Run: `uv run tpo --help`
Expected: shows `usage: tpo [-h] ...` with all subcommands including `skills`.

- [ ] **Step 4: Verify `tpo skills install --help`**

Run: `uv run tpo skills install --help`
Expected: shows `--target {codex,claude,all}` and `--scope {user,project}` and `--force` options.

- [ ] **Step 5: Verify deprecation shims**

Run: `uv run pipeline-watch --help 2>&1 | head -5`
Expected: first stderr line is the deprecation warning naming `pipeline-watch` and `tpo`; the rest is normal `tpo` help output.

Run: `uv run hermes-pipeline --help 2>&1 | head -5`
Expected: same, naming `hermes-pipeline` and `tpo`.

- [ ] **Step 6: Build the wheel and verify package data is included**

Run: `uv build && unzip -l dist/*.whl | grep skills`
Expected: lists `hermes_pipeline/data/skills/todos-manager/SKILL.md` and all 9 files under `hermes_pipeline/data/skills/todos-manager/sections/`.

- [ ] **Step 7: Verify `tpo skills install` end-to-end in a scratch HOME**

```bash
mkdir -p /tmp/tpo-verify-home
HOME=/tmp/tpo-verify-home uv run tpo skills install --target all
ls /tmp/tpo-verify-home/.claude/skills/todos-manager/SKILL.md
ls /tmp/tpo-verify-home/.agents/skills/todos-manager/SKILL.md
rm -rf /tmp/tpo-verify-home
```
Expected: both `SKILL.md` files exist; command prints `OK (claude): ...` and `OK (codex): ...`.

- [ ] **Step 8: Report completion**

No commit needed for this task — it's verification only. If any step fails, return to the relevant earlier task and fix before proceeding.

## Success Criteria

- `uv tool install hermes-pipeline` → `tpo` is available on PATH
- `tpo --help` shows the command as `tpo` with all existing subcommands
- `pipeline-watch`/`hermes-pipeline` still work for one release, printing a deprecation warning
- `tpo skills install --target all` copies todos-manager to both `~/.claude/skills/todos-manager/` and `~/.agents/skills/todos-manager/`
- All existing tests pass with `tpo` in place of `pipeline-watch`
- `uv build` produces a wheel that includes `hermes_pipeline/data/skills/todos-manager/` as package data
- New tests for `tpo skills install` cover: mocked copy verifies files written per target; reinstall overwrites existing files without error; target directory created if missing; package data discoverable via `importlib.resources`; `--target`/`--scope` argument parsing produces correct path combinations; `_resolve_bundled_dir()` fallback path (mocked non-filesystem Traversable); permission-denied on target produces Problem/Cause/Fix error, exit 1; `--target all` partial failure reports per-target status, exit 1; deprecated `pipeline-watch`/`hermes-pipeline` shims print warning and still dispatch correctly
