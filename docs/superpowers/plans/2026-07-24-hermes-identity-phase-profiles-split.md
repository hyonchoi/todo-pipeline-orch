# Split data/profiles into hermes-identity and phase-profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `hermes_pipeline/data/profiles/` into two directories — `data/hermes-identity/pipeline/SOUL.md` (identity data) and `data/phase-profiles/{gstack,agent-skills}/phases.yaml` (phase-orchestration configs) — with call sites, tests, and docs updated to match.

**Architecture:** Pure path-rename refactor. `git mv` the directories, update the two `.joinpath(...)` call sites in `hermes_pipeline/phases.py` and `hermes_pipeline/contract.py` that build these paths via `importlib.resources`, add one regression test locking in the new resolved paths and the exclusion guarantee (`"pipeline"`/`"hermes-identity"` never appear in the unknown-profile error's "Available profiles" listing), and update two public docs. No packaging manifest change needed — `pyproject.toml` uses `packages = ["hermes_pipeline"]`, which picks up any directory structure inside the package.

**Tech Stack:** Python 3.12+, uv, pytest, `importlib.resources`.

## Global Constraints

- `pipeline/` subdirectory nesting under the identity path must be preserved: `data/hermes-identity/pipeline/SOUL.md`, not flattened to `data/hermes-identity/SOUL.md`.
- Doc scope is limited to `docs/howto-pipeline-contract.md` and `docs/howto-agent-skills-profile.md`. Do NOT touch CHANGELOG.md, SPEC.md, `tasks/todo.md`, `tasks/plan.md`, `TODOS-archive.md`, or any file under `docs/superpowers/plans/` — those are historical records of past state.
- No packaging/manifest changes — `pyproject.toml`'s `packages = ["hermes_pipeline"]` (hatchling) already covers this.
- Existing test `tests/test_phases_package_resolution.py` must pass unmodified (it asserts behavior via `load_phases()`, not literal paths).

---

## Current State (for reference)

```
hermes_pipeline/data/profiles/
├── pipeline/SOUL.md
├── gstack/phases.yaml
└── agent-skills/phases.yaml
```

- `hermes_pipeline/phases.py:32` — inside `resolve_profile_phases_path()`:
  ```python
  profiles_root = files("hermes_pipeline").joinpath("data", "profiles")
  ```
- `hermes_pipeline/contract.py:182` — inside `bundled_profile_dir()`:
  ```python
  traversable = files("hermes_pipeline").joinpath("data", "profiles", "pipeline")
  ```

## Target State

```
hermes_pipeline/data/hermes-identity/pipeline/SOUL.md
hermes_pipeline/data/phase-profiles/gstack/phases.yaml
hermes_pipeline/data/phase-profiles/agent-skills/phases.yaml
```

---

### Task 1: Move directories and update call sites

**Files:**
- Move: `hermes_pipeline/data/profiles/pipeline/` → `hermes_pipeline/data/hermes-identity/pipeline/`
- Move: `hermes_pipeline/data/profiles/` → `hermes_pipeline/data/phase-profiles/`
- Modify: `hermes_pipeline/contract.py:182`
- Modify: `hermes_pipeline/phases.py:32`
- Test: `tests/test_phases_package_resolution.py` (existing, must pass unmodified)

**Interfaces:**
- Consumes: nothing new — reuses existing `resolve_profile_phases_path(profile: str) -> Path` and `bundled_profile_dir() -> Path` signatures unchanged.
- Produces: `resolve_profile_phases_path("gstack")` now resolves under `hermes_pipeline/data/phase-profiles/gstack/phases.yaml`. `bundled_profile_dir()` now resolves under `hermes_pipeline/data/hermes-identity/pipeline/`. Task 2's regression test depends on these exact resolved shapes.

- [ ] **Step 1: Create the hermes-identity directory and move the pipeline/ subtree into it**

```bash
mkdir -p hermes_pipeline/data/hermes-identity
git mv hermes_pipeline/data/profiles/pipeline hermes_pipeline/data/hermes-identity/pipeline
```

- [ ] **Step 2: Rename the now-emptied profiles/ directory to phase-profiles/**

After step 1, `hermes_pipeline/data/profiles/` contains only `gstack/` and `agent-skills/`.

```bash
git mv hermes_pipeline/data/profiles hermes_pipeline/data/phase-profiles
```

- [ ] **Step 3: Verify the resulting layout**

Run: `find hermes_pipeline/data -type f`

Expected output (order may vary):
```
hermes_pipeline/data/hermes-identity/pipeline/SOUL.md
hermes_pipeline/data/phase-profiles/gstack/phases.yaml
hermes_pipeline/data/phase-profiles/agent-skills/phases.yaml
```

- [ ] **Step 4: Update the call site in contract.py**

In `hermes_pipeline/contract.py`, inside `bundled_profile_dir()`, change:

```python
    traversable = files("hermes_pipeline").joinpath("data", "profiles", "pipeline")
```

to:

```python
    traversable = files("hermes_pipeline").joinpath("data", "hermes-identity", "pipeline")
```

- [ ] **Step 5: Update the call site in phases.py**

In `hermes_pipeline/phases.py`, inside `resolve_profile_phases_path()`, change:

```python
    profiles_root = files("hermes_pipeline").joinpath("data", "profiles")
```

to:

```python
    profiles_root = files("hermes_pipeline").joinpath("data", "phase-profiles")
```

- [ ] **Step 6: Run the existing regression test to confirm no behavior change**

Run: `uv run pytest tests/test_phases_package_resolution.py -v`
Expected: PASS (unmodified — it asserts `load_phases()` behavior, not literal paths)

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/data hermes_pipeline/contract.py hermes_pipeline/phases.py
git commit -m "refactor: split data/profiles into hermes-identity and phase-profiles"
```

---

### Task 2: Add regression test locking in the new layout and the exclusion guarantee

**Files:**
- Test: `tests/test_profile_layout_split.py` (new)

**Interfaces:**
- Consumes: `hermes_pipeline.contract.bundled_profile_dir() -> Path`, `hermes_pipeline.phases.resolve_profile_phases_path(profile: str) -> Path`, `hermes_pipeline.contract.ContractSchemaError` (raised by `resolve_profile_phases_path` on unknown profile, with message format `f"unknown profile '{profile}'. Available profiles: {', '.join(available)}. ..."`).
- Produces: nothing consumed by later tasks — this is the terminal verification task for the path-shape guarantee.

- [ ] **Step 1: Write the failing test**

Create `tests/test_profile_layout_split.py`:

```python
"""Locks in the hermes-identity / phase-profiles directory split (TODO-32).

Regression coverage for the risk this refactor exists to close: identity
data and phase-orchestration configs must never re-mix under one namespace,
and the unknown-profile error's "Available profiles" listing must never
list identity-only directory names as if they were phase profiles.
"""
from __future__ import annotations

import pytest

from hermes_pipeline.contract import ContractSchemaError, bundled_profile_dir
from hermes_pipeline.phases import resolve_profile_phases_path


def test_bundled_profile_dir_resolves_under_hermes_identity():
    path = bundled_profile_dir()
    assert path.parts[-2:] == ("hermes-identity", "pipeline")
    assert (path / "SOUL.md").is_file()


def test_resolve_profile_phases_path_resolves_under_phase_profiles():
    path = resolve_profile_phases_path("gstack")
    assert "phase-profiles" in path.parts
    assert "gstack" in path.parts
    assert path.name == "phases.yaml"
    assert path.is_file()


def test_unknown_profile_error_excludes_identity_directory_names():
    with pytest.raises(ContractSchemaError) as exc_info:
        resolve_profile_phases_path("does-not-exist")
    message = str(exc_info.value)
    assert "pipeline" not in message
    assert "hermes-identity" not in message
    assert "gstack" in message
    assert "agent-skills" in message
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run pytest tests/test_profile_layout_split.py -v`
Expected: PASS (Task 1 already made the underlying path changes — this test verifies them, it isn't TDD-first since the refactor precedes it)

- [ ] **Step 3: Run the full test suite to confirm no regressions**

Run: `uv run pytest -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_profile_layout_split.py
git commit -m "test: lock in hermes-identity/phase-profiles path split"
```

---

### Task 3: Update public docs to reference the new paths

**Files:**
- Modify: `docs/howto-pipeline-contract.md`
- Modify: `docs/howto-agent-skills-profile.md`

**Interfaces:**
- Consumes: nothing (docs only).
- Produces: nothing consumed by later tasks — terminal documentation task.

- [ ] **Step 1: Update docs/howto-agent-skills-profile.md**

Three occurrences of `hermes_pipeline/data/profiles/` need updating. Run:

```bash
grep -n "hermes_pipeline/data/profiles" docs/howto-agent-skills-profile.md
```

Expected output (line numbers as of this plan):
```
13:A profile is a directory under `hermes_pipeline/data/profiles/<name>/` containing a `phases.yaml`...
80:1. Create `hermes_pipeline/data/profiles/<name>/phases.yaml` following the same...
87:...flag names a profile that doesn't exist under `hermes_pipeline/data/profiles/`.
```

Replace all three occurrences of `hermes_pipeline/data/profiles/` with `hermes_pipeline/data/phase-profiles/` (these all describe phase-config profiles, not identity data — the correct new namespace):

```bash
sed -i '' 's#hermes_pipeline/data/profiles/#hermes_pipeline/data/phase-profiles/#g' docs/howto-agent-skills-profile.md
```

- [ ] **Step 2: Verify the replacement in docs/howto-agent-skills-profile.md**

Run: `grep -n "hermes_pipeline/data/" docs/howto-agent-skills-profile.md`
Expected: all matches show `hermes_pipeline/data/phase-profiles/`, none show `hermes_pipeline/data/profiles/`

- [ ] **Step 3: Update docs/howto-pipeline-contract.md**

One occurrence needs updating. Run:

```bash
grep -n "hermes_pipeline/data/profiles" docs/howto-pipeline-contract.md
```

Expected output:
```
125:...field names a profile that doesn't exist under `hermes_pipeline/data/profiles/`.
```

Replace it:

```bash
sed -i '' 's#hermes_pipeline/data/profiles/#hermes_pipeline/data/phase-profiles/#g' docs/howto-pipeline-contract.md
```

- [ ] **Step 4: Verify the replacement in docs/howto-pipeline-contract.md**

Run: `grep -n "hermes_pipeline/data/" docs/howto-pipeline-contract.md`
Expected: match shows `hermes_pipeline/data/phase-profiles/`, none show `hermes_pipeline/data/profiles/`

- [ ] **Step 5: Confirm no other in-scope doc references the old path**

Run: `grep -rn "data/profiles" docs/howto-pipeline-contract.md docs/howto-agent-skills-profile.md`
Expected: no output (empty)

- [ ] **Step 6: Commit**

```bash
git add docs/howto-pipeline-contract.md docs/howto-agent-skills-profile.md
git commit -m "docs: update profile paths to hermes-identity/phase-profiles split"
```

---

### Task 4: Full verification pass

**Files:** none (verification only)

**Interfaces:**
- Consumes: all prior tasks' changes.
- Produces: nothing — final gate before calling this done.

- [ ] **Step 1: Confirm the old profiles/ directory no longer exists**

Run: `test -d hermes_pipeline/data/profiles && echo "STILL EXISTS" || echo "removed"`
Expected: `removed`

- [ ] **Step 2: Run the full test suite one more time**

Run: `uv run pytest -v`
Expected: All tests PASS, including `tests/test_phases_package_resolution.py` and `tests/test_profile_layout_split.py`

- [ ] **Step 3: Grep the whole repo for stray old-path references outside the excluded historical files**

Run:
```bash
grep -rln "data/profiles\b" --include='*.py' --include='*.md' . \
  | grep -v -E 'CHANGELOG.md|SPEC.md|tasks/todo.md|tasks/plan.md|TODOS-archive.md|docs/superpowers/plans/'
```
Expected: no output (empty) — any remaining match must be one of the excluded historical files above

- [ ] **Step 4: Confirm git status is clean and all three commits are present**

Run: `git log --oneline -3`
Expected: three commits — docs update, regression test, and directory-split/call-site-update — in that reverse-chronological order
