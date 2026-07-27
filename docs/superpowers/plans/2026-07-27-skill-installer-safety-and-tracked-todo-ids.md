# Skill Installer Safety And Tracked TODO IDs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make bundled skill installation fail on existing destinations unless `--reinstall` is explicit, add safe uninstall, and migrate TODO ID assignment to tracked `NEXT_TODO_ID` state in `TODOS.md`.

**Architecture:** Keep TODO-35 inside the existing `hermes_pipeline/cli.py` skills command surface with small preflight helpers for destructive operations. Keep TODO-38 runtime as bundled Markdown skill docs, with `tests/skill-test-environment/skill_logic.py` as the deterministic oracle that proves the docs describe a coherent contract.

**Tech Stack:** Python 3.12+, argparse, pathlib, shutil, importlib.resources, fcntl.flock on Unix, same-directory temp files plus os.replace, uv/pytest

## Global Constraints

- Python 3.12+ project managed by `uv`.
- Use `uv sync` / `uv run` / `uv add` for dependency and execution management.
- Current installer lives in `hermes_pipeline/cli.py` around the `skills` subparser and `_cmd_skills_install`.
- Existing installer tests live in `tests/test_skills_install.py`; one current test asserts overwrite-by-default and must flip.
- Bundled skill source lives in `hermes_pipeline/data/skills/todos-manager/`.
- The todos-manager deterministic test oracle lives under `tests/skill-test-environment/`.
- `Spec:` and `Reference:` fields remain `--revise`-only and must not be guessed.
- `.hermes/todo_id_counter` is explicitly out of scope for deletion in `TODO-38`.
- The real `todos-manager` runtime is the bundled skill documentation itself (`SKILL.md` plus `sections/*.md`).
- Exact tracked preamble line: `> - NEXT_TODO_ID: <n>`.
- `NEXT_TODO_ID` accepts only a positive base-10 integer.
- Missing or malformed `NEXT_TODO_ID` is repaired by `--audit` and by the pre-add reconciliation path.
- Locked and atomic means an exclusive lock spans read, reconciliation, and replacement.
- Use only `--reinstall`; do not add a `--force` alias.
- `tpo recover-counter` sets `.hermes/todo_id_counter` to `NEXT_TODO_ID - 1` when tracked state exists, and falls back to full scan only for legacy files.

---

## File Structure

- `hermes_pipeline/cli.py`: add `skills install --reinstall`, `skills uninstall`, `--yes`, destination preflight helpers, and installer/uninstaller user-visible errors.
- `tests/test_skills_install.py`: flip overwrite test, add parser tests, reinstall tests, uninstall confirmation tests, and multi-target preflight rollback tests.
- `tests/skill-test-environment/skill_logic.py`: add tracked metadata parsing, validation, reconciliation, locked atomic TODO updates, add/audit/init/convert helper behavior, and compatibility counter updates.
- `tests/skill-test-environment/unit/test_id_sequencing.py`: replace scan-only ID assumptions with tracked `NEXT_TODO_ID` contract tests.
- `tests/skill-test-environment/unit/test_format_validation.py`: validate preamble placement, malformed tracked state, duplicate tracked lines, and audit repair.
- `tests/skill-test-environment/unit/test_verify.py`: assert audit output reports tracked-state repairs.
- `tests/skill-test-environment/golden/*.yaml`: update golden assertions to require `NEXT_TODO_ID` preservation and increment behavior.
- `tests/skill-test-environment/demo-project/TODOS.md`: add the tracked preamble line with the next value for the fixture.
- `hermes_pipeline/data/skills/todos-manager/SKILL.md`: describe the tracked state contract and audit/add reconciliation.
- `hermes_pipeline/data/skills/todos-manager/sections/schema.md`: update preamble template and field validation text.
- `hermes_pipeline/data/skills/todos-manager/sections/id-assignment.md`: replace full-scan common path with tracked-state rules and conflict repair.
- `hermes_pipeline/data/skills/todos-manager/sections/error-messages.md`: add exact messages for malformed/missing/repaired tracked state.
- `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`: add legacy migration, stale low value, archive-only max, and failed write scenarios.
- `hermes_pipeline/data/skills/todos-manager/sections/convert-mode-b.md`: make convert insert `NEXT_TODO_ID`.
- `TODOS.md`: migrate the root project preamble to include `NEXT_TODO_ID`.
- `hermes_pipeline/counter.py`: make recover-counter prefer tracked state and use scan fallback only for legacy/malformed files.
- `tests/test_counter.py` and `tests/test_recover_counter_cli.py`: cover tracked-state recovery and legacy fallback.

## Dry Design Audit

Use these exact contract strings before implementation. If tests expose awkward wording, update docs and tests together in Task 1.

Installer existing-destination error:

```text
Problem ({name}): todos-manager is already installed at {dest}.
Cause: reinstalling without --reinstall would overwrite local changes.
Fix: rerun with `tpo skills install --target {target} --scope {scope} --reinstall` after reviewing the destination.
```

Installer destructive preflight error:

```text
Problem ({name}): cannot replace todos-manager at {dest}.
Cause: {reason}
Fix: make the destination removable, or uninstall it manually after reviewing local changes.
```

Uninstall confirmation error:

```text
Problem ({name}): uninstall requires confirmation.
Cause: deleting {dest} removes the installed todos-manager skill.
Fix: rerun with `tpo skills uninstall --target {target} --scope {scope} --yes` to confirm deletion.
```

Tracked TODO preamble line:

```markdown
> - NEXT_TODO_ID: <n>
```

### Task 1: Harden `tpo skills install` parser and fail-on-exists behavior

**Files:**
- Modify: `hermes_pipeline/cli.py`
- Modify: `tests/test_skills_install.py`

**Interfaces:**
- Consumes: `_skills_install_targets(target: str, scope: str) -> list[tuple[str, Path]]`
- Produces: `_cmd_skills_install(args, config: Config | None) -> int` using `args.reinstall: bool`
- Produces: `_preflight_skill_replacement(name: str, dest: Path) -> str | None`

- [ ] **Step 1: Write parser and fail-on-exists tests**

```python
def test_install_accepts_reinstall_flag():
    parser = build_parser()
    args = parser.parse_args(["skills", "install", "--reinstall"])
    assert args.reinstall is True

def test_install_does_not_accept_force_alias():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["skills", "install", "--force"])

def test_install_existing_destination_fails_without_reinstall(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = Config(projects_dir=tmp_path / "projects")
    target_dir = tmp_path / ".claude" / "skills" / "todos-manager"
    target_dir.mkdir(parents=True)
    (target_dir / "SKILL.md").write_text("local edit", encoding="utf-8")

    result = _cmd_skills_install(FakeArgs(target="claude", scope="user", reinstall=False), config)

    out = capsys.readouterr().out
    assert result == 1
    assert (target_dir / "SKILL.md").read_text(encoding="utf-8") == "local edit"
    assert "Problem (claude): todos-manager is already installed" in out
    assert "Cause: reinstalling without --reinstall would overwrite local changes." in out
    assert "Fix: rerun with `tpo skills install --target claude --scope user --reinstall`" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_skills_install.py::TestSkillsInstallParsing::test_install_accepts_reinstall_flag tests/test_skills_install.py::TestSkillsInstallParsing::test_install_does_not_accept_force_alias tests/test_skills_install.py::TestCmdSkillsInstall::test_install_existing_destination_fails_without_reinstall -v`

Expected: FAIL because `--reinstall` is not parsed and the installer overwrites the existing destination.

- [ ] **Step 3: Add `--reinstall` parser flag and fail-on-exists implementation**

```python
skills_install_parser.add_argument(
    "--reinstall",
    action="store_true",
    help="Replace an existing installed todos-manager skill after explicit review",
)
```

```python
def _preflight_skill_replacement(name: str, dest: Path) -> str | None:
    if dest.is_symlink():
        return "the destination is a symlink"
    if dest.exists() and not dest.is_dir():
        return "the destination exists but is not a directory"
    if dest.exists():
        try:
            probe = dest / ".tpo-delete-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            return f"the destination is not writable ({e})"
    parent = dest.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / ".tpo-install-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return f"the install directory is not writable ({e})"
    return None
```

Update `_cmd_skills_install` before copying:

```python
reinstall = bool(getattr(args, "reinstall", False))
for name, install_dir in targets:
    dest = install_dir / "todos-manager"
    if dest.exists() or dest.is_symlink():
        if not reinstall:
            any_failed = True
            print(f"Problem ({name}): todos-manager is already installed at {dest}.")
            print("Cause: reinstalling without --reinstall would overwrite local changes.")
            print(
                f"Fix: rerun with `tpo skills install --target {name} --scope {args.scope} --reinstall` "
                "after reviewing the destination."
            )
            continue
```

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_skills_install.py -k "reinstall or existing_destination or defaults or force_alias" -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_skills_install.py
git commit -m "feat(TODO-35): require explicit skill reinstall"
```

### Task 2: Add reinstall replacement and multi-target preflight rollback

**Files:**
- Modify: `hermes_pipeline/cli.py`
- Modify: `tests/test_skills_install.py`

**Interfaces:**
- Consumes: `_preflight_skill_replacement(name: str, dest: Path) -> str | None`
- Produces: install preflight that validates all selected targets before deleting any target when `args.reinstall` is true

- [ ] **Step 1: Write reinstall and rollback tests**

```python
def test_install_reinstall_replaces_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = Config(projects_dir=tmp_path / "projects")
    target_dir = tmp_path / ".claude" / "skills" / "todos-manager"
    target_dir.mkdir(parents=True)
    (target_dir / "SKILL.md").write_text("stale content", encoding="utf-8")

    result = _cmd_skills_install(FakeArgs(target="claude", scope="user", reinstall=True), config)

    assert result == 0
    assert (target_dir / "SKILL.md").read_text(encoding="utf-8") != "stale content"

def test_install_reinstall_target_all_preflights_before_removing_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = Config(projects_dir=tmp_path / "projects")
    claude_dest = tmp_path / ".claude" / "skills" / "todos-manager"
    codex_dest = tmp_path / ".agents" / "skills" / "todos-manager"
    claude_dest.mkdir(parents=True)
    codex_dest.parent.mkdir(parents=True)
    codex_dest.write_text("not a directory", encoding="utf-8")
    (claude_dest / "SKILL.md").write_text("keep me", encoding="utf-8")

    result = _cmd_skills_install(FakeArgs(target="all", scope="user", reinstall=True), config)

    out = capsys.readouterr().out
    assert result == 1
    assert (claude_dest / "SKILL.md").read_text(encoding="utf-8") == "keep me"
    assert "Problem (codex): cannot replace todos-manager" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_skills_install.py::TestCmdSkillsInstall::test_install_reinstall_replaces_existing_directory tests/test_skills_install.py::TestCmdSkillsInstall::test_install_reinstall_target_all_preflights_before_removing_first -v`

Expected: FAIL because reinstall replacement and all-target preflight are incomplete.

- [ ] **Step 3: Implement all-target destructive preflight and replacement**

```python
if reinstall:
    preflight_errors: list[tuple[str, Path, str]] = []
    for name, install_dir in targets:
        dest = install_dir / "todos-manager"
        if dest.exists() or dest.is_symlink():
            reason = _preflight_skill_replacement(name, dest)
            if reason is not None:
                preflight_errors.append((name, dest, reason))
    if preflight_errors:
        for name, dest, reason in preflight_errors:
            print(f"Problem ({name}): cannot replace todos-manager at {dest}.")
            print(f"Cause: {reason}.")
            print("Fix: make the destination removable, or uninstall it manually after reviewing local changes.")
        return 1
```

Then replace before copy:

```python
if reinstall and dest.exists():
    shutil.rmtree(dest)
shutil.copytree(source, dest)
```

- [ ] **Step 4: Run focused installer tests**

Run: `uv run pytest tests/test_skills_install.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_skills_install.py
git commit -m "feat(TODO-35): preflight skill reinstall targets"
```

### Task 3: Add `tpo skills uninstall` with confirmation and preflight

**Files:**
- Modify: `hermes_pipeline/cli.py`
- Modify: `tests/test_skills_install.py`

**Interfaces:**
- Consumes: `_skills_install_targets(target: str, scope: str) -> list[tuple[str, Path]]`
- Consumes: `_preflight_skill_replacement(name: str, dest: Path) -> str | None`
- Produces: `_cmd_skills_uninstall(args, config: Config | None) -> int` using `args.yes: bool`

- [ ] **Step 1: Write parser and confirmation tests**

```python
from hermes_pipeline.cli import _cmd_skills_uninstall

def test_uninstall_parser_accepts_scope_target_and_yes():
    parser = build_parser()
    args = parser.parse_args(["skills", "uninstall", "--target", "all", "--scope", "project", "--yes"])
    assert args.skills_command == "uninstall"
    assert args.target == "all"
    assert args.scope == "project"
    assert args.yes is True

def test_uninstall_refuses_without_yes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = tmp_path / ".claude" / "skills" / "todos-manager"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("installed", encoding="utf-8")

    result = _cmd_skills_uninstall(FakeArgs(target="claude", scope="user", yes=False), None)

    out = capsys.readouterr().out
    assert result == 1
    assert dest.exists()
    assert "Problem (claude): uninstall requires confirmation." in out
```

- [ ] **Step 2: Write uninstall success and all-target rollback tests**

```python
def test_uninstall_yes_removes_existing_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    dest = tmp_path / ".claude" / "skills" / "todos-manager"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("installed", encoding="utf-8")

    result = _cmd_skills_uninstall(FakeArgs(target="claude", scope="user", yes=True), None)

    assert result == 0
    assert not dest.exists()

def test_uninstall_all_preflights_before_removing_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dest = tmp_path / ".claude" / "skills" / "todos-manager"
    codex_dest = tmp_path / ".agents" / "skills" / "todos-manager"
    claude_dest.mkdir(parents=True)
    codex_dest.parent.mkdir(parents=True)
    codex_dest.write_text("not a directory", encoding="utf-8")
    (claude_dest / "SKILL.md").write_text("keep me", encoding="utf-8")

    result = _cmd_skills_uninstall(FakeArgs(target="all", scope="user", yes=True), None)

    out = capsys.readouterr().out
    assert result == 1
    assert claude_dest.exists()
    assert "Problem (codex): cannot replace todos-manager" in out
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_skills_install.py -k "uninstall" -v`

Expected: FAIL because the uninstall parser and command do not exist.

- [ ] **Step 4: Implement uninstall parser**

```python
skills_uninstall_parser = skills_subparsers.add_parser(
    "uninstall",
    help="Remove the bundled todos-manager skill from selected skill directories",
)
skills_uninstall_parser.add_argument("--target", choices=["codex", "claude", "all"], default="claude")
skills_uninstall_parser.add_argument("--scope", choices=["user", "project"], default="user")
skills_uninstall_parser.add_argument("-y", "--yes", action="store_true", help="Confirm deletion without prompting")
skills_uninstall_parser.set_defaults(func=_cmd_skills_uninstall)
```

- [ ] **Step 5: Implement uninstall command**

```python
def _cmd_skills_uninstall(args, config: Config | None) -> int:
    targets = _skills_install_targets(args.target, args.scope)
    if not bool(getattr(args, "yes", False)):
        for name, install_dir in targets:
            dest = install_dir / "todos-manager"
            print(f"Problem ({name}): uninstall requires confirmation.")
            print(f"Cause: deleting {dest} removes the installed todos-manager skill.")
            print(
                f"Fix: rerun with `tpo skills uninstall --target {name} --scope {args.scope} --yes` "
                "to confirm deletion."
            )
        return 1

    preflight_errors: list[tuple[str, Path, str]] = []
    for name, install_dir in targets:
        dest = install_dir / "todos-manager"
        if dest.exists() or dest.is_symlink():
            reason = _preflight_skill_replacement(name, dest)
            if reason is not None:
                preflight_errors.append((name, dest, reason))
    if preflight_errors:
        for name, dest, reason in preflight_errors:
            print(f"Problem ({name}): cannot replace todos-manager at {dest}.")
            print(f"Cause: {reason}.")
            print("Fix: make the destination removable, or uninstall it manually after reviewing local changes.")
        return 1

    for name, install_dir in targets:
        dest = install_dir / "todos-manager"
        if dest.exists():
            shutil.rmtree(dest)
            print(f"OK ({name}): removed todos-manager from {dest}")
        else:
            print(f"OK ({name}): todos-manager is not installed at {dest}")
    return 0
```

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest tests/test_skills_install.py -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/cli.py tests/test_skills_install.py
git commit -m "feat(TODO-35): add safe skill uninstall"
```

### Task 4: Add tracked TODO metadata oracle

**Files:**
- Modify: `tests/skill-test-environment/skill_logic.py`
- Modify: `tests/skill-test-environment/unit/test_id_sequencing.py`
- Modify: `tests/skill-test-environment/unit/test_format_validation.py`

**Interfaces:**
- Produces: `NEXT_TODO_ID_LINE_RE: Pattern[str]`
- Produces: `read_next_todo_id(text: str) -> tuple[int | None, list[str]]`
- Produces: `compute_scan_next_id(todos_path: Path, archive_path: Path) -> int`
- Produces: `reconcile_next_todo_id(project_dir: Path, mode: str) -> tuple[int, list[str]]`
- Produces: `replace_next_todo_id_line(text: str, next_id: int) -> str`

- [ ] **Step 1: Write metadata parser tests**

```python
from tests.skill_test_environment.skill_logic import (
    compute_scan_next_id,
    read_next_todo_id,
    reconcile_next_todo_id,
    replace_next_todo_id_line,
)

def test_read_next_todo_id_from_preamble():
    text = "# TODOS\n\n> **Format rules:**\n> - NEXT_TODO_ID: 8\n\n- [ ] TODO-1: A\n"
    value, issues = read_next_todo_id(text)
    assert value == 8
    assert issues == []

def test_read_next_todo_id_rejects_zero_negative_and_non_integer():
    for raw in ("0", "-1", "1.5", "abc"):
        text = f"# TODOS\n\n> - NEXT_TODO_ID: {raw}\n"
        value, issues = read_next_todo_id(text)
        assert value is None
        assert any("NEXT_TODO_ID" in issue for issue in issues)

def test_read_next_todo_id_rejects_duplicate_lines():
    text = "# TODOS\n\n> - NEXT_TODO_ID: 8\n> - NEXT_TODO_ID: 9\n"
    value, issues = read_next_todo_id(text)
    assert value is None
    assert any("duplicated" in issue.lower() for issue in issues)

def test_replace_next_todo_id_line_preserves_preamble():
    text = "# TODOS\n\n> **Format rules:**\n> - NEXT_TODO_ID: 8\n> - Completed entries: archived\n"
    updated = replace_next_todo_id_line(text, 9)
    assert "> - NEXT_TODO_ID: 9" in updated
    assert "> - Completed entries: archived" in updated
```

- [ ] **Step 2: Write reconciliation tests**

```python
def test_reconcile_repairs_stale_low_value_from_archive(tmp_path):
    (tmp_path / "TODOS.md").write_text("# TODOS\n\n> - NEXT_TODO_ID: 3\n\n- [ ] TODO-1: A\n", encoding="utf-8")
    (tmp_path / "TODOS-archive.md").write_text("- [x] TODO-7: Archived\n", encoding="utf-8")

    next_id, messages = reconcile_next_todo_id(tmp_path, mode="audit")

    assert next_id == 8
    assert "> - NEXT_TODO_ID: 8" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
    assert any("corrected NEXT_TODO_ID from 3 to 8" in message for message in messages)

def test_reconcile_missing_line_inserts_after_format_rules(tmp_path):
    (tmp_path / "TODOS.md").write_text(
        "# TODOS\n\n> **Format rules (enforced by `todos-manager` skill):**\n> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`\n\n- [ ] TODO-4: Existing\n",
        encoding="utf-8",
    )
    (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

    next_id, messages = reconcile_next_todo_id(tmp_path, mode="audit")

    assert next_id == 5
    assert "> - NEXT_TODO_ID: 5" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
    assert any("inserted NEXT_TODO_ID: 5" in message for message in messages)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py tests/skill-test-environment/unit/test_format_validation.py -k "next_todo_id or reconcile" -v`

Expected: FAIL because tracked metadata helpers are not implemented.

- [ ] **Step 4: Implement tracked metadata helpers**

```python
NEXT_TODO_ID_LINE_RE = re.compile(r"^>\s+-\s+NEXT_TODO_ID:\s+(.+?)\s*$", re.MULTILINE)

def read_next_todo_id(text: str) -> tuple[int | None, list[str]]:
    matches = list(NEXT_TODO_ID_LINE_RE.finditer(text))
    if not matches:
        return None, ["NEXT_TODO_ID is missing from the TODOS.md preamble"]
    if len(matches) > 1:
        return None, ["NEXT_TODO_ID is duplicated in the TODOS.md preamble"]
    raw = matches[0].group(1)
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        return None, [f"NEXT_TODO_ID must be a positive base-10 integer, got {raw!r}"]
    return int(raw), []

def compute_scan_next_id(todos_path: Path, archive_path: Path) -> int:
    return compute_next_id(todos_path, archive_path)

def replace_next_todo_id_line(text: str, next_id: int) -> str:
    replacement = f"> - NEXT_TODO_ID: {next_id}"
    if NEXT_TODO_ID_LINE_RE.search(text):
        return NEXT_TODO_ID_LINE_RE.sub(replacement, text, count=1)
    marker = "> **Format rules (enforced by `todos-manager` skill):**"
    if marker in text:
        return text.replace(marker, marker + "\n" + replacement, 1)
    return text.replace("# TODOS\n", "# TODOS\n\n" + replacement + "\n", 1)

def reconcile_next_todo_id(project_dir: Path, mode: str) -> tuple[int, list[str]]:
    todos_path = project_dir / "TODOS.md"
    archive_path = project_dir / "TODOS-archive.md"
    text = todos_path.read_text(encoding="utf-8")
    tracked, issues = read_next_todo_id(text)
    scanned_next = compute_scan_next_id(todos_path, archive_path)
    messages: list[str] = []
    if tracked == scanned_next and not issues:
        return tracked, messages
    updated = replace_next_todo_id_line(text, scanned_next)
    todos_path.write_text(updated, encoding="utf-8")
    if tracked is None:
        messages.append(f"{mode}: inserted NEXT_TODO_ID: {scanned_next}")
    else:
        messages.append(f"{mode}: corrected NEXT_TODO_ID from {tracked} to {scanned_next}")
    messages.extend(issues)
    return scanned_next, messages
```

- [ ] **Step 5: Run focused oracle tests**

Run: `uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py tests/skill-test-environment/unit/test_format_validation.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_id_sequencing.py tests/skill-test-environment/unit/test_format_validation.py
git commit -m "feat(TODO-38): add tracked TODO ID oracle"
```

### Task 5: Add locked atomic TODO updates and rollback tests in the oracle

**Files:**
- Modify: `tests/skill-test-environment/skill_logic.py`
- Modify: `tests/skill-test-environment/unit/test_id_sequencing.py`

**Interfaces:**
- Consumes: `replace_next_todo_id_line(text: str, next_id: int) -> str`
- Produces: `atomic_update_todos(todos_path: Path, transform: Callable[[str], str]) -> None`
- Produces: `assign_next_todo_id(project_dir: Path) -> tuple[int, list[str]]`

- [ ] **Step 1: Write rollback and stale-writer tests**

```python
def test_atomic_update_todos_rolls_back_when_replace_fails(tmp_path, monkeypatch):
    from tests.skill_test_environment import skill_logic
    todos = tmp_path / "TODOS.md"
    original = "# TODOS\n\n> - NEXT_TODO_ID: 8\n\n- [ ] TODO-7: Existing\n"
    todos.write_text(original, encoding="utf-8")

    def fail_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(skill_logic.os, "replace", fail_replace)

    with pytest.raises(OSError):
        skill_logic.atomic_update_todos(todos, lambda text: text.replace("8", "9", 1))

    assert todos.read_text(encoding="utf-8") == original

def test_assign_next_todo_id_repairs_conflict_before_returning(tmp_path):
    (tmp_path / "TODOS.md").write_text("# TODOS\n\n> - NEXT_TODO_ID: 7\n\n- [ ] TODO-7: Existing\n", encoding="utf-8")
    (tmp_path / "TODOS-archive.md").write_text("", encoding="utf-8")

    assigned, messages = assign_next_todo_id(tmp_path)

    assert assigned == 8
    assert "> - NEXT_TODO_ID: 9" in (tmp_path / "TODOS.md").read_text(encoding="utf-8")
    assert any("corrected NEXT_TODO_ID" in message for message in messages)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -k "atomic_update_todos or assign_next_todo_id" -v`

Expected: FAIL because locked atomic update helpers are not implemented.

- [ ] **Step 3: Implement locked atomic update helper**

```python
import fcntl
import os
import tempfile
from collections.abc import Callable

def atomic_update_todos(todos_path: Path, transform: Callable[[str], str]) -> None:
    lock_path = todos_path.with_suffix(todos_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        original = todos_path.read_text(encoding="utf-8")
        updated = transform(original)
        fd, tmp_name = tempfile.mkstemp(dir=todos_path.parent, prefix=".TODOS.", text=True)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(updated)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, todos_path)
        except BaseException:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
```

- [ ] **Step 4: Implement assign helper**

```python
def assign_next_todo_id(project_dir: Path) -> tuple[int, list[str]]:
    todos_path = project_dir / "TODOS.md"
    archive_path = project_dir / "TODOS-archive.md"
    messages: list[str] = []
    assigned = 1

    def transform(text: str) -> str:
        nonlocal assigned, messages
        tracked, issues = read_next_todo_id(text)
        scanned_next = compute_scan_next_id(todos_path, archive_path)
        used = scan_ids(text)
        if tracked is None or tracked in used or tracked < scanned_next:
            assigned = scanned_next
            messages.append(f"add: corrected NEXT_TODO_ID from {tracked} to {scanned_next}")
        else:
            assigned = tracked
        messages.extend(issues)
        return replace_next_todo_id_line(text, assigned + 1)

    atomic_update_todos(todos_path, transform)
    return assigned, messages
```

- [ ] **Step 5: Run focused oracle tests**

Run: `uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/skill-test-environment/skill_logic.py tests/skill-test-environment/unit/test_id_sequencing.py
git commit -m "feat(TODO-38): make TODO ID updates atomic"
```

### Task 6: Update bundled todos-manager docs, fixtures, and golden files

**Files:**
- Modify: `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/schema.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/id-assignment.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/error-messages.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/convert-mode-b.md`
- Modify: `tests/skill-test-environment/demo-project/TODOS.md`
- Modify: `tests/skill-test-environment/golden/add_happy_path.yaml`
- Modify: `tests/skill-test-environment/golden/audit_report.yaml`
- Modify: `tests/skill-test-environment/golden/init_output.yaml`
- Modify: `TODOS.md`

**Interfaces:**
- Consumes: exact preamble line `> - NEXT_TODO_ID: <n>`
- Produces: docs/fixtures contract matching `read_next_todo_id()` and `assign_next_todo_id()`

- [ ] **Step 1: Update preamble templates**

Replace the old ID line in `sections/schema.md`, `tests/skill-test-environment/demo-project/TODOS.md`, and root `TODOS.md`:

```markdown
> - NEXT_TODO_ID: 8
> - ID: sequential, immutable. Use `NEXT_TODO_ID` for the common path; reconcile by scanning `TODOS.md` plus `TODOS-archive.md` only when the tracked value is missing, malformed, stale, or conflicts.
```

For root `TODOS.md`, compute the actual value before editing:

Run: `rg -o "TODO-[0-9]+" TODOS.md TODOS-archive.md | sed 's/.*TODO-//' | sort -n | tail -1`

Expected: use max + 1 for the root `NEXT_TODO_ID` line.

- [ ] **Step 2: Update ID assignment docs**

Use this replacement text in `sections/id-assignment.md`:

```markdown
## Stable TODO-<n> ID Assignment

### ID sequencing rule

- IDs are assigned sequentially in insertion order, starting from 1.
- Once a TODO-<n> is committed, its ID is immutable.
- The common path reads `NEXT_TODO_ID` from the `TODOS.md` preamble and assigns that value.
- After a successful add, increment `NEXT_TODO_ID` by 1 in the same locked atomic write as the new entry.
- Archived entries count during reconciliation. Do not fill gaps.

### Tracked state rule

`TODOS.md` must contain this blockquote line inside the format-rules preamble:

```markdown
> - NEXT_TODO_ID: <n>
```

`<n>` must be a positive base-10 integer and means "the next ID to assign."

### Reconciliation algorithm

1. Read `NEXT_TODO_ID` from `TODOS.md`.
2. If the value is missing, duplicated, non-integer, zero, negative, stale, or already used by an active TODO, scan `TODOS.md` and `TODOS-archive.md`.
3. Compute `max(all IDs) + 1`, or `1` when no IDs exist.
4. Write the corrected `NEXT_TODO_ID` in place and report the correction.
5. For `--add`, continue by assigning the corrected ID and incrementing the tracked value.

### Counter cache

`.hermes/todo_id_counter` is compatibility/cache state only. It may be updated after a successful TODO write, but it no longer decides the next ID.
```

- [ ] **Step 3: Update golden assertions**

Add these assertions to `tests/skill-test-environment/golden/add_happy_path.yaml`:

```yaml
  - regex_present: "^> - NEXT_TODO_ID: 9$"
  - no_regex_present: "Next = max\\(all IDs in TODOS.md \\+ TODOS-archive.md\\) \\+ 1"
```

Add these assertions to `tests/skill-test-environment/golden/audit_report.yaml`:

```yaml
  - regex_present: "NEXT_TODO_ID"
  - regex_present: "corrected|inserted|valid"
```

Add this assertion to `tests/skill-test-environment/golden/init_output.yaml`:

```yaml
  - regex_present: "^> - NEXT_TODO_ID: 1$"
```

- [ ] **Step 4: Update acceptance scenarios**

Add scenario bullets to `sections/acceptance-scenarios.md`:

```markdown
- Legacy migration: a file without `NEXT_TODO_ID` is repaired by `--audit` and before first `--add`.
- Stale low value: `NEXT_TODO_ID: 3` with existing `TODO-7` is corrected to `8`.
- Archive-only max: archived `TODO-9` with active max `TODO-4` is corrected to `10`.
- Failed write: if replacement fails, `TODOS.md` remains byte-for-byte unchanged and `.hermes/todo_id_counter` is not advanced.
- Conflict: if `NEXT_TODO_ID` points to an active TODO, reconciliation scans active plus archive IDs, writes the corrected value, and continues.
```

- [ ] **Step 5: Run docs/fixture contract tests**

Run: `uv run pytest tests/skill-test-environment -v`

Expected: PASS after oracle tests and golden fixtures agree.

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/data/skills/todos-manager tests/skill-test-environment TODOS.md
git commit -m "docs(TODO-38): migrate todos-manager tracked ID contract"
```

### Task 7: Update recover-counter compatibility behavior

**Files:**
- Modify: `hermes_pipeline/counter.py`
- Modify: `tests/test_counter.py`
- Modify: `tests/test_recover_counter_cli.py`

**Interfaces:**
- Produces: `_read_tracked_next_todo_id(todos_text: str) -> int | None`
- Produces: `recover_counter(project_dir: Path) -> int`

- [ ] **Step 1: Write counter tests**

```python
def test_recover_counter_uses_tracked_next_todo_id_minus_one(tmp_path):
    project_dir = tmp_path
    (project_dir / "TODOS.md").write_text("# TODOS\n\n> - NEXT_TODO_ID: 8\n\n- [ ] TODO-3: Active\n", encoding="utf-8")

    result = recover_counter(project_dir)

    assert result == 7
    assert (project_dir / ".hermes" / "todo_id_counter").read_text(encoding="utf-8") == "7"

def test_recover_counter_falls_back_to_scan_when_tracked_state_missing(tmp_path):
    project_dir = tmp_path
    (project_dir / "TODOS.md").write_text("# TODOS\n\n- [ ] TODO-3: Active\n", encoding="utf-8")

    result = recover_counter(project_dir)

    assert result == 3

def test_recover_counter_falls_back_to_scan_when_tracked_state_malformed(tmp_path):
    project_dir = tmp_path
    (project_dir / "TODOS.md").write_text("# TODOS\n\n> - NEXT_TODO_ID: abc\n\n- [ ] TODO-4: Active\n", encoding="utf-8")

    result = recover_counter(project_dir)

    assert result == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_counter.py -k "tracked_next_todo_id or falls_back" -v`

Expected: FAIL because `recover_counter()` always uses scan/max preservation.

- [ ] **Step 3: Implement tracked-state preference**

```python
NEXT_TODO_ID_RE = re.compile(r"^>\s+-\s+NEXT_TODO_ID:\s+([1-9][0-9]*)\s*$", re.MULTILINE)

def _read_tracked_next_todo_id(todos_text: str) -> int | None:
    matches = NEXT_TODO_ID_RE.findall(todos_text)
    if len(matches) != 1:
        return None
    return int(matches[0])
```

Update `recover_counter()`:

```python
todos_content = todos_path.read_text()
tracked_next = _read_tracked_next_todo_id(todos_content)
if tracked_next is not None:
    result = tracked_next - 1
else:
    scanned_ids = [int(m) for m in TODO_ID_RE.findall(todos_content)]
    scanned_max = max(scanned_ids) if scanned_ids else 0
    existing_value = 0
    if counter_path.exists():
        try:
            existing_value = int(counter_path.read_text().strip())
        except (ValueError, OSError):
            existing_value = 0
    result = max(existing_value, scanned_max)
```

- [ ] **Step 4: Add CLI output test**

```python
def test_recover_counter_cli_reports_tracked_value(tmp_path, monkeypatch, capsys):
    projects = tmp_path / "projects"
    project = projects / "myproject"
    project.mkdir(parents=True)
    (project / "TODOS.md").write_text("# TODOS\n\n> - NEXT_TODO_ID: 8\n", encoding="utf-8")
    config = Config(projects_dir=projects)
    monkeypatch.setattr("hermes_pipeline.cli.Config.from_env", lambda: config)

    result = main(["recover-counter", "myproject"])

    out = capsys.readouterr().out
    assert result == 0
    assert "Counter set to 7 for project myproject" in out
```

- [ ] **Step 5: Run focused counter tests**

Run: `uv run pytest tests/test_counter.py tests/test_recover_counter_cli.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hermes_pipeline/counter.py tests/test_counter.py tests/test_recover_counter_cli.py
git commit -m "feat(TODO-38): recover counter from tracked TODO state"
```

### Task 8: Final contract audit and verification

**Files:**
- Modify: `docs/reference-counter.md`
- Modify: `docs/howto-todos-manager.md`
- Modify: `docs/tutorial-todos-manager.md`
- Modify: `docs/reference-skill-test-harness.md`

**Interfaces:**
- Consumes: final CLI behavior and tracked metadata contract from Tasks 1-7
- Produces: docs that no longer describe scan-only ID assignment as the common path

- [ ] **Step 1: Search for stale contract text**

Run: `rg -n "dirs_exist_ok|overwrite|--force|Next = max|todo_id_counter.*authoritative|max\\(all IDs|full scan|scan.*TODOS-archive" hermes_pipeline tests docs TODOS.md README.md`

Expected: any hits are either legacy-fallback explanations or text to update.

- [ ] **Step 2: Update stale docs**

Use this replacement language where docs describe the current ID source:

```markdown
`TODOS.md` stores the tracked `NEXT_TODO_ID` value in its format-rules preamble. `todos-manager --add` uses that value on the common path, increments it after a successful write, and reconciles by scanning `TODOS.md` plus `TODOS-archive.md` only when the tracked value is missing, malformed, stale, or conflicting.
```

Use this replacement language where docs describe installer replacement:

```markdown
`tpo skills install` fails when `todos-manager` is already installed. Use `tpo skills install --reinstall` after reviewing the destination to replace it intentionally. Use `tpo skills uninstall --yes` to remove installed copies.
```

- [ ] **Step 3: Run focused verification**

Run: `uv run pytest tests/test_skills_install.py tests/test_counter.py tests/test_recover_counter_cli.py tests/skill-test-environment -v`

Expected: PASS.

- [ ] **Step 4: Run full verification**

Run: `uv run pytest`

Expected: PASS.

- [ ] **Step 5: Run lint if available**

Run: `uv run ruff check .`

Expected: PASS.

- [ ] **Step 6: Verify package data still includes skill docs**

Run: `uv run pytest tests/test_skills_install.py::test_todos_manager_skill_is_packaged_data -v`

Expected: PASS.

- [ ] **Step 7: Commit final docs**

```bash
git add docs/reference-counter.md docs/howto-todos-manager.md docs/tutorial-todos-manager.md docs/reference-skill-test-harness.md
git commit -m "docs(TODO-38): update tracked TODO ID references"
```

## Self-Review

- Spec coverage: TODO-35 install fail-on-exists, explicit reinstall, uninstall confirmation, all-target preflight, and structured errors are covered by Tasks 1-3. TODO-38 tracked preamble, add/audit reconciliation, legacy migration, archive fallback, atomic writes, failed write rollback, docs/oracle lockstep, and recover-counter compatibility are covered by Tasks 4-8.
- Placeholder scan: this plan intentionally avoids `TBD`, `TODO`, "implement later", and "similar to" instructions. Code steps include concrete signatures and snippets.
- Type consistency: `_skills_install_targets()`, `_cmd_skills_install()`, `_cmd_skills_uninstall()`, `read_next_todo_id()`, `replace_next_todo_id_line()`, `reconcile_next_todo_id()`, `atomic_update_todos()`, `assign_next_todo_id()`, and `_read_tracked_next_todo_id()` are named consistently across producing and consuming tasks.
