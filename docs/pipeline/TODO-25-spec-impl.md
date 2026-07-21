# TODO-25 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional `**Spec:**` / `**Reference:**` fields to TODOS.md entries and thread the resolved, existence-checked, containment-checked paths into the first pipeline phase's prompt.

**Architecture:** A new standalone parser module (`hermes_pipeline/todos_md.py`) extracts `Spec:`/`Reference:` values for a given `todo_id` from TODOS.md via regex-anchored entry-boundary scanning. `phases.py`'s `_invoke_hermes` detects "first phase" positionally from the loaded phase list, resolves + validates the paths (existence + containment under `project_dir`), and passes survivors into `_render_phase_prompt`, which appends conditional header lines. All failure modes degrade silently (log + skip), never raising. Schema docs (`skills/todos-manager/sections/schema.md`, `SKILL.md`, `TODOS.md` preamble) are updated to document the new `--revise`-only fields.

**Tech Stack:** Python 3.12, `pathlib`, `pytest`, existing `hermes_pipeline` package conventions (frozen dataclasses, `from __future__ import annotations`, module-level `log = logging.getLogger(__name__)`).

## Global Constraints

- Path resolution: `(Path(project_dir) / path).resolve()` must fall under `Path(project_dir).resolve()` (containment check) — reject `../` traversal and symlink escapes.
- Every failure mode (missing file, missing todo_id, malformed entry, traversal) must **never raise** — log a warning and degrade to no-injection for that item.
- `Reference:` items are resolved **independently** — one invalid reference does not drop the others, and does not drop `Spec:`.
- No file content is ever injected into the prompt — only validated path strings.
- `_render_phase_prompt` output must be byte-identical to current behavior when no spec/reference survive (regression guard).
- `phases_list[0]` access must be guarded against an empty list — never raise `IndexError`.
- `--add` and `--revise`'s AI-prefill/auto-research must NOT suggest or scan for `Spec:`/`Reference:` values — user must type them verbatim.

---

### Task 1: `hermes_pipeline/todos_md.py` — TODOS.md field parser

**Files:**
- Create: `hermes_pipeline/todos_md.py`
- Test: `tests/test_todos_md.py`

**Interfaces:**
- Consumes: nothing from other tasks (standalone, `pathlib.Path` only).
- Produces: `find_todo_fields(todos_md_path: Path, todo_id: str) -> dict` with shape `{"spec": str | None, "references": list[str]}`. This is what Task 2 imports and calls.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for hermes_pipeline.todos_md.find_todo_fields."""
from __future__ import annotations
from pathlib import Path
from hermes_pipeline.todos_md import find_todo_fields


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "TODOS.md"
    p.write_text(content)
    return p


def test_entry_with_both_fields(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **What:** stuff
  - **Why:** reasons
  - **Spec:** docs/pipeline/TODO-25-spec.md
  - **Reference:** docs/notes/a.md, docs/notes/b.md
  - **Decisions:** Priority `P1`
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {
        "spec": "docs/pipeline/TODO-25-spec.md",
        "references": ["docs/notes/a.md", "docs/notes/b.md"],
    }


def test_entry_with_only_spec(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **What:** stuff
  - **Spec:** docs/pipeline/TODO-25-spec.md
  - **Decisions:** Priority `P1`
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": "docs/pipeline/TODO-25-spec.md", "references": []}


def test_entry_with_only_reference(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **What:** stuff
  - **Reference:** docs/notes/a.md
  - **Decisions:** Priority `P1`
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": ["docs/notes/a.md"]}


def test_entry_with_neither_field(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **What:** stuff
  - **Why:** reasons
  - **Decisions:** Priority `P1`
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": []}


def test_reference_whitespace_variance(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Reference:**   docs/notes/a.md ,docs/notes/b.md  ,  docs/notes/c.md
""")
    result = find_todo_fields(p, "TODO-25")
    assert result["references"] == [
        "docs/notes/a.md", "docs/notes/b.md", "docs/notes/c.md",
    ]


def test_malformed_todos_md_does_not_raise(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:**
  garbled nonsense with no closing structure
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": []}


def test_multiple_entries_anchors_to_todo_id(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-24: Other thing** — summary
  - **Spec:** docs/pipeline/TODO-24-spec.md
  - **Reference:** docs/notes/wrong.md
- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** docs/pipeline/TODO-25-spec.md
  - **Reference:** docs/notes/right.md
- [x] **TODO-26: Yet another** — summary
  - **Spec:** docs/pipeline/TODO-26-spec.md
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {
        "spec": "docs/pipeline/TODO-25-spec.md",
        "references": ["docs/notes/right.md"],
    }


def test_traversal_path_is_returned_verbatim_by_parser(tmp_path):
    """Containment rejection is phases.py's job (Task 3), not the parser's —
    the parser only extracts raw strings from TODOS.md text."""
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** ../../etc/passwd
""")
    result = find_todo_fields(p, "TODO-25")
    assert result["spec"] == "../../etc/passwd"


def test_todo_id_not_present(tmp_path):
    p = _write(tmp_path, """\
# TODOS

- [ ] **TODO-24: Other thing** — summary
  - **Spec:** docs/pipeline/TODO-24-spec.md
""")
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": []}


def test_todos_md_missing_entirely(tmp_path):
    p = tmp_path / "TODOS.md"
    result = find_todo_fields(p, "TODO-25")
    assert result == {"spec": None, "references": []}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_todos_md.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_pipeline.todos_md'`

- [ ] **Step 3: Write the implementation**

```python
"""Standalone TODOS.md Spec:/Reference: field extractor.

Not a full markdown parser, and not a reuse of the todos-manager skill's
prose-based logic (which isn't Python-importable, being an LLM-facing
skill). Scans only the sub-bullet block belonging to the requested
todo_id, anchored between its entry header and the next entry header
(or EOF), so a naive regex cannot bleed into a neighboring entry's
fields.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_ENTRY_HEADER_RE = re.compile(
    r"^- \[[ x→~]\] \*\*(TODO-\d+):", re.MULTILINE,
)
_SPEC_RE = re.compile(r"^\s*-\s*\*\*Spec:\*\*\s*(.+?)\s*$", re.MULTILINE)
_REFERENCE_RE = re.compile(r"^\s*-\s*\*\*Reference:\*\*\s*(.+?)\s*$", re.MULTILINE)

_EMPTY_RESULT = {"spec": None, "references": []}


def find_todo_fields(todos_md_path: Path, todo_id: str) -> dict:
    """Locate the TODO-<n> entry in todos_md_path and extract Spec:/Reference:.

    Returns {"spec": str | None, "references": list[str]}.
    Never raises for parsing problems — missing file, missing todo_id, or
    a malformed entry all degrade to the empty/partial result.
    """
    try:
        text = todos_md_path.read_text()
    except (FileNotFoundError, OSError) as e:
        log.warning("todos_md: could not read %s: %s", todos_md_path, e)
        return dict(_EMPTY_RESULT)

    try:
        return _extract(text, todo_id)
    except Exception as e:  # pragma: no cover - defense in depth
        log.warning("todos_md: failed to parse entry for %s: %s", todo_id, e)
        return dict(_EMPTY_RESULT)


def _extract(text: str, todo_id: str) -> dict:
    headers = list(_ENTRY_HEADER_RE.finditer(text))
    start = None
    end = len(text)
    for i, m in enumerate(headers):
        if m.group(1) == todo_id:
            start = m.end()
            if i + 1 < len(headers):
                end = headers[i + 1].start()
            break
    if start is None:
        return dict(_EMPTY_RESULT)

    block = text[start:end]

    spec_match = _SPEC_RE.search(block)
    spec = spec_match.group(1).strip() or None if spec_match else None

    ref_match = _REFERENCE_RE.search(block)
    references: list[str] = []
    if ref_match:
        raw = ref_match.group(1)
        references = [r.strip() for r in raw.split(",") if r.strip()]

    return {"spec": spec, "references": references}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_todos_md.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/todos_md.py tests/test_todos_md.py
git commit -m "feat: add todos_md.find_todo_fields parser for TODO-25"
```

---

### Task 2: `phases.py` — first-phase detection, path validation, prompt injection

**Files:**
- Modify: `hermes_pipeline/phases.py:167-190` (`_render_phase_prompt`)
- Modify: `hermes_pipeline/phases.py:244-253` (`_invoke_hermes`, phase lookup section)
- Test: `tests/test_phases.py`
- Test: `tests/test_phases_invoke.py`

**Interfaces:**
- Consumes: `find_todo_fields(todos_md_path: Path, todo_id: str) -> dict` from Task 1 (`hermes_pipeline.todos_md`).
- Produces: `_render_phase_prompt(template: str, *, todo_id: str, tick_id: str, project_slug: str, spec_path: str | None = None, reference_paths: list[str] | None = None) -> str`. `_invoke_hermes` behavior: when the resolved phase is positionally first in `phases_list`, resolves and injects `Spec:`/`Reference:` paths from TODOS.md at `Path(project_dir) / "TODOS.md"`.

- [ ] **Step 1: Write the failing test for `_render_phase_prompt`**

Append to `tests/test_phases.py`:

```python
def test_render_phase_prompt_no_spec_reference_unchanged():
    """Regression guard: omitting spec/reference kwargs must produce
    byte-identical output to pre-TODO-25 behavior."""
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-7", tick_id="01JT", project_slug="demo",
    )
    assert "Spec (authoritative):" not in out
    assert "Reference material:" not in out
    assert out == (
        "Pipeline context:\n"
        "- todo_id: TODO-7\n"
        "- tick_id: 01JT\n"
        "- project_slug: demo\n"
        "Work on TODO-7 ONLY. Do not pick a different TODO.\n\n"
        "do thing"
    )


def test_render_phase_prompt_both_spec_and_reference():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        spec_path="docs/pipeline/TODO-25-spec.md",
        reference_paths=["docs/notes/a.md", "docs/notes/b.md"],
    )
    assert "Spec (authoritative): docs/pipeline/TODO-25-spec.md\n" in out
    assert "Reference material: docs/notes/a.md, docs/notes/b.md\n" in out


def test_render_phase_prompt_only_spec():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        spec_path="docs/pipeline/TODO-25-spec.md",
    )
    assert "Spec (authoritative): docs/pipeline/TODO-25-spec.md\n" in out
    assert "Reference material:" not in out


def test_render_phase_prompt_only_reference():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        reference_paths=["docs/notes/a.md"],
    )
    assert "Spec (authoritative):" not in out
    assert "Reference material: docs/notes/a.md\n" in out


def test_render_phase_prompt_empty_reference_list_omitted():
    from hermes_pipeline import phases as phases_mod
    out = phases_mod._render_phase_prompt(
        "do thing", todo_id="TODO-25", tick_id="01JT", project_slug="demo",
        reference_paths=[],
    )
    assert "Reference material:" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phases.py -k render_phase_prompt -v`
Expected: FAIL — `test_render_phase_prompt_no_spec_reference_unchanged` passes (no kwarg change yet) but the other 3 new tests FAIL with `TypeError: _render_phase_prompt() got an unexpected keyword argument 'spec_path'`

- [ ] **Step 3: Update `_render_phase_prompt` implementation**

Replace `hermes_pipeline/phases.py:167-190`:

```python
def _render_phase_prompt(
    template: str, *, todo_id: str, tick_id: str, project_slug: str,
    spec_path: str | None = None, reference_paths: list[str] | None = None,
) -> str:
    """Inject the pipeline context the phase prompt needs.

    A picked TODO must be visible to the LLM — otherwise a TODO-7 pick can
    silently produce work for whatever TODO the LLM latches onto next. We
    prepend a non-templated context header and ALSO support `{todo_id}` /
    `{tick_id}` / `{project_slug}` substitution for phases that want to
    weave the values into prose. `.format()` with named-only fields is safe
    here because every prompt in configs/phases.yaml is repo-owned.

    `spec_path`/`reference_paths` are optional, pre-validated (existence +
    project_dir containment already checked by the caller) TODOS.md
    Spec:/Reference: values for the pipeline's first phase only. Omitted
    entirely when absent so prompt output for TODOs without these fields
    stays byte-identical to before this feature existed.
    """
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
        body = template.format(todo_id=todo_id, tick_id=tick_id, project_slug=project_slug)
    except (KeyError, IndexError):
        # Template uses a `{name}` we don't supply — fall back to verbatim
        # body. The header still scopes the run to this TODO.
        body = template
    return header + body
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phases.py -k render_phase_prompt -v`
Expected: PASS (5 tests). Verify the unchanged-output test still matches byte-for-byte (no extra blank line vs. before — the original had `\n\n` after the header, meaning `header + body` where body starts fresh; confirm assertion string matches exactly).

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/phases.py tests/test_phases.py
git commit -m "feat: add optional spec/reference lines to _render_phase_prompt"
```

- [ ] **Step 6: Write the failing tests for first-phase detection + injection**

Append to `tests/test_phases_invoke.py`:

```python
def test_first_phase_injects_spec_and_reference(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
        _fake_phase(phase_key="phase_3_other", terminal=False, prompt="do other"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "docs" / "pipeline").mkdir(parents=True)
    spec_file = project_dir / "docs" / "pipeline" / "TODO-25-spec.md"
    spec_file.write_text("spec content")
    ref_file = project_dir / "docs" / "notes" / "a.md"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("ref content")
    (project_dir / "TODOS.md").write_text(f"""\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** docs/pipeline/TODO-25-spec.md
  - **Reference:** docs/notes/a.md
""")
    seen = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": ""}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)
    phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert "Spec (authoritative): docs/pipeline/TODO-25-spec.md" in seen["prompt"]
    assert "Reference material: docs/notes/a.md" in seen["prompt"]


def test_non_first_phase_does_not_inject(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
        _fake_phase(phase_key="phase_3_other", terminal=False, prompt="do other"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "docs" / "pipeline").mkdir(parents=True)
    (project_dir / "docs" / "pipeline" / "TODO-25-spec.md").write_text("spec content")
    (project_dir / "TODOS.md").write_text("""\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** docs/pipeline/TODO-25-spec.md
""")
    seen = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": ""}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)
    phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_3_other",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert "Spec (authoritative):" not in seen["prompt"]


def test_missing_spec_file_dropped_reference_kept(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    ref_file = project_dir / "docs" / "notes" / "a.md"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("ref content")
    (project_dir / "TODOS.md").write_text("""\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** docs/pipeline/nonexistent-spec.md
  - **Reference:** docs/notes/a.md
""")
    seen = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": ""}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)
    phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert "Spec (authoritative):" not in seen["prompt"]
    assert "Reference material: docs/notes/a.md" in seen["prompt"]


def test_traversal_path_dropped_independently_of_existence(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # File exists on disk (outside project_dir) but must still be rejected
    # by the containment check, independent of the existence check.
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("outside content")
    ref_file = project_dir / "docs" / "notes" / "a.md"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("ref content")
    (project_dir / "TODOS.md").write_text("""\
# TODOS

- [ ] **TODO-25: Do the thing** — summary
  - **Spec:** ../outside.md
  - **Reference:** docs/notes/a.md
""")
    seen = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": ""}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)
    phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert "Spec (authoritative):" not in seen["prompt"]
    assert "Reference material: docs/notes/a.md" in seen["prompt"]


def test_no_todos_md_no_injection_phase_runs_normally(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [
        _fake_phase(phase_key="phase_2_autoplan", terminal=False, prompt="do thing"),
    ])
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    seen = {}
    def _capture(**kw):
        seen["prompt"] = kw["prompt"]
        return {"returncode": 0, "stdout": ""}
    monkeypatch.setattr(phases_mod, "_run_hermes_subprocess", _capture)
    out = phases_mod._invoke_hermes(
        todo_id="TODO-25", phase_key="phase_2_autoplan",
        tick_id="01JT", state_dir=state_dir, project_slug="demo",
        project_dir=str(project_dir),
    )
    assert out["status"] == "success"
    assert "Spec (authoritative):" not in seen["prompt"]


def test_empty_phases_list_does_not_raise_indexerror(state_dir, monkeypatch, tmp_path):
    """load_phases() returning [] must not crash phases_list[0] lookup —
    but phase lookup itself already raises UnknownPhaseError first, which
    is the correct existing behavior; this confirms no IndexError leaks
    through first-phase detection before that point."""
    monkeypatch.setattr(phases_mod, "load_phases", lambda *a, **k: [])
    with pytest.raises(phases_mod.UnknownPhaseError):
        phases_mod._invoke_hermes(
            todo_id="TODO-25", phase_key="phase_2_autoplan",
            tick_id="01JT", state_dir=state_dir, project_slug="demo",
            project_dir=str(tmp_path),
        )
```

- [ ] **Step 7: Run tests to verify they fail**

Run: `pytest tests/test_phases_invoke.py -k "first_phase or non_first_phase or missing_spec or traversal_path or no_todos_md or empty_phases_list" -v`
Expected: FAIL — injection tests fail because no `Spec (authoritative):` ever appears (feature not yet wired); `test_empty_phases_list_does_not_raise_indexerror` currently passes already (existing `UnknownPhaseError` path), which is fine — Step 8 must not break it.

- [ ] **Step 8: Implement first-phase detection + path validation in `_invoke_hermes`**

Replace `hermes_pipeline/phases.py:244-253`:

```python
def _resolve_spec_reference_paths(*, project_dir: str | None, todo_id: str) -> tuple[str | None, list[str]]:
    """Resolve and validate Spec:/Reference: paths for injection into the
    first phase's prompt. Fail-soft throughout: any TODOS.md read/parse
    problem, missing file, or containment violation drops that item only
    and never raises.
    """
    if project_dir is None:
        return None, []

    from .todos_md import find_todo_fields

    project_root = Path(project_dir).resolve()
    todos_md_path = project_root / "TODOS.md"
    fields = find_todo_fields(todos_md_path, todo_id)

    def _validate(rel_path: str) -> str | None:
        try:
            resolved = (project_root / rel_path).resolve()
        except (OSError, ValueError):
            return None
        try:
            resolved.relative_to(project_root)
        except ValueError:
            log.warning(
                "todos_md: %s path %r for %s resolves outside project_dir, dropping",
                rel_path, rel_path, todo_id,
            )
            return None
        if not resolved.is_file():
            log.warning("todos_md: %s path %r for %s does not exist, dropping",
                        rel_path, rel_path, todo_id)
            return None
        return rel_path

    spec_path = fields["spec"]
    if spec_path is not None:
        spec_path = _validate(spec_path)

    reference_paths = [p for p in (_validate(r) for r in fields["references"]) if p is not None]

    return spec_path, reference_paths


def _invoke_hermes(*, todo_id: str, phase_key: str, tick_id: str, state_dir, project_slug: str, **kw) -> dict:
    """Execute a single phase via hermes subprocess and write ready_for_review on terminal success."""
    profile = _resolve_execution_profile(state_dir)
    phases_list = load_phases(resolve_profile_phases_path(profile))
    phases_cfg = {p.phase_key: p for p in phases_list}
    phase = phases_cfg.get(phase_key)
    if phase is None:
        raise UnknownPhaseError(
            f"phase_key {phase_key!r} not found in phases.yaml; "
            f"known keys: {sorted(phases_cfg)}"
        )
    is_first_phase = bool(phases_list) and phase.phase_key == phases_list[0].phase_key
```

Then, further down in the same function, replace the existing prompt-rendering call:

```python
    prompt = _render_phase_prompt(
        phase.prompt, todo_id=todo_id, tick_id=tick_id, project_slug=project_slug,
    )
```

with:

```python
    spec_path, reference_paths = (
        _resolve_spec_reference_paths(project_dir=kw.get("project_dir"), todo_id=todo_id)
        if is_first_phase else (None, [])
    )
    prompt = _render_phase_prompt(
        phase.prompt, todo_id=todo_id, tick_id=tick_id, project_slug=project_slug,
        spec_path=spec_path, reference_paths=reference_paths,
    )
```

Note: leave the `_invoke_review_phase` early-return branch (for `phase.phase_key == _rp.REVIEW_PHASE_KEY`) and the `phase.gate` early-return branch untouched — they return before reaching the prompt-rendering line, and review/gate phases are never index 0 in `phases.yaml`, so no injection logic is needed there.

- [ ] **Step 9: Run tests to verify they pass**

Run: `pytest tests/test_phases_invoke.py tests/test_phases.py -v`
Expected: PASS (all existing + all new tests, including the full `test_phases_invoke.py` and `test_phases.py` suites — confirm no regressions in the pre-existing tests in those files).

- [ ] **Step 10: Run the full pipeline test suite**

Run: `pytest tests/ -x -q`
Expected: PASS (no regressions elsewhere — `phases.py` changes are additive/backward-compatible via default `None`/`[]` kwargs).

- [ ] **Step 11: Commit**

```bash
git add hermes_pipeline/phases.py tests/test_phases_invoke.py
git commit -m "feat: inject validated Spec/Reference paths into first phase prompt"
```

---

### Task 3: Schema documentation updates (`todos-manager` skill)

**Files:**
- Modify: `skills/todos-manager/sections/schema.md`
- Modify: `skills/todos-manager/SKILL.md`
- Modify: `TODOS.md` (preamble blockquote only)

**Interfaces:**
- Consumes: nothing (pure documentation; no code dependency on Tasks 1/2).
- Produces: nothing consumed by other tasks — this is the terminal task.

- [ ] **Step 1: Update `skills/todos-manager/sections/schema.md` optional-fields table**

In `skills/todos-manager/sections/schema.md`, replace the table at lines 30-40:

```markdown
## Optional fields

| Field | Description |
|-------|-------------|
| **Pros:** | Benefits |
| **Cons:** | Risks/drawbacks |
| **Context:** | References, design doc pointers, file locations |
| **Depends on:** | Other TODO-<n> references |
| **Assumptions:** | Preconditions |
| **Completed:** | Version + date (set when done) |
| **Resolved design:** | Design decisions (zero or more) |
| **Spec:** | Single path to the authoritative deliverable (e.g. from office-hours / grill-with-docs / spec skills) — drives the pipeline's first phase. `--revise`-only: never AI-suggested, never part of `--add` auto-research; always user-typed verbatim. |
| **Reference:** | Comma-separated list of supplementary/background paths, threaded into the pipeline's first phase prompt. Same `--revise`-only, never-auto-suggested rule as `Spec:`. Not a synonym for `Context:`, which stays free-text prose. |
```

- [ ] **Step 2: Update the preamble template text in the same file**

Replace lines 55-69 (`## Preamble Template` block) so the format-rules list includes the two new fields:

```markdown
## Preamble Template

When creating or converting TODOS.md, insert this blockquote as the file header:

```markdown
# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Spec:**, **Reference:**
> - **Spec:**/**Reference:** are `--revise`-only (never suggested by `--add` or auto-research); always typed verbatim
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`
```
```

- [ ] **Step 3: Update `skills/todos-manager/SKILL.md`**

Add a note near the `--revise` section header (around line 178, `### \`--revise\`: Revise an existing TODO entry with AI-pre-filled suggestions`), inserting a line directly after that header:

```markdown
### `--revise`: Revise an existing TODO entry with AI-pre-filled suggestions

**Exception:** `**Spec:**` and `**Reference:**` are never AI-pre-filled or auto-detected (e.g. no scanning `docs/pipeline/TODO-<n>-*.md` and offering it as a suggestion) — the user must type these values verbatim. A wrong guessed path is worse than an empty field. These two fields also never appear in `--add`'s auto-research (see step 4.5).

Read `sections/revise.md` and follow its steps in full.
```

- [ ] **Step 4: Update `TODOS.md` preamble blockquote**

Read the current preamble in `TODOS.md` (top of file) and apply the same optional-fields list edit as Step 2 — add `**Spec:**, **Reference:**` to the enumerated optional-fields line, and add the `--revise`-only caveat line. Do not touch any existing entry content below the preamble.

- [ ] **Step 5: Verify no test suite covers these docs as code**

Run: `pytest tests/ -k todos_manager -v` (or the closest matching test file, e.g. `test_kanban_tasks.py`/`test_kanban.py` if they parse schema.md) — confirm nothing asserts on the exact optional-fields table text in a way that would now fail. If a test does hardcode the field list, update its expected string to include `Spec:`/`Reference:`.

- [ ] **Step 6: Commit**

```bash
git add skills/todos-manager/sections/schema.md skills/todos-manager/SKILL.md TODOS.md
git commit -m "docs: add Spec/Reference optional fields to todos-manager schema"
```

---

## Self-Review

**Spec coverage:**
- Schema changes (2 new optional fields, `--add`/`--revise` behavior) → Task 3.
- `hermes_pipeline/todos_md.py` module + `find_todo_fields` → Task 1.
- Dynamic first-phase detection (`phases_list[0]`, not hardcoded) → Task 2, Step 8.
- Lookup + injection flow (resolve → containment check → existence check → independent per-item drop → inject into `_render_phase_prompt`) → Task 2, Step 8 (`_resolve_spec_reference_paths`).
- `_render_phase_prompt` conditional header lines, omitted-block regression guard → Task 2, Steps 1-5.
- Fail-soft table (all 7 rows: missing TODOS.md, missing todo_id, malformed entry, missing Spec file, missing Reference files, traversal) → covered by Task 1 tests (parser-level) + Task 2 tests (phases.py-level: missing file, traversal, no-TODOS.md).
- Files touched list → matches Tasks 1-3 file lists exactly.
- Test coverage list (all bullet points under "Test coverage (required)") → each has a corresponding test in Task 1 Step 1 or Task 2 Steps 6/8.

**Placeholder scan:** No TBD/TODO/"add appropriate"/"similar to Task N" patterns present — every step has complete code.

**Type consistency:** `find_todo_fields(todos_md_path: Path, todo_id: str) -> dict` (Task 1) matches the call site in `_resolve_spec_reference_paths` (Task 2, Step 8): `find_todo_fields(todos_md_path, todo_id)` positional call, `fields["spec"]` / `fields["references"]` key access — consistent with the `{"spec": ..., "references": [...]}` return shape used throughout. `_render_phase_prompt`'s new kwargs (`spec_path: str | None = None, reference_paths: list[str] | None = None`) match both the Task 2 Step 3 signature and the Step 8 call site.
