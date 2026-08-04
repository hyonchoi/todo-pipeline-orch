# TODO-40 Document Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach `todos-manager --add` and `--revise` to discover, classify, validate, and user-confirm `Plan:`, `Spec:`, and `Reference:` document attachments.

**Architecture:** Keep document-attachment policy in one new reusable skill section consumed by both workflows. The bundled Markdown skill remains authoritative; schema copies and the structural test harness are updated together, while runtime selection and prompt consumption remain explicitly outside TODO-40.

**Tech Stack:** Markdown skill contracts, Python 3.12, pytest, uv, importlib.resources

## Global Constraints

- `Plan:` is the execution authority and the sole actionability gate; `Spec:` is the outcome contract; `Reference:` is supplementary context.
- Discovery is repository-bounded, relevance-gated, capped at 20 total reads and 10 searches, and limited to five qualified attachment candidates.
- Detected and manual paths must resolve to existing regular files inside the repository and be stored as normalized repository-relative POSIX paths.
- No attachment is written or replaced without the existing synthesis and preview confirmations.
- Existing TODOs without `Plan:` remain valid; TODO-39 owns actionability enforcement and pipeline consumption.
- Use `rtk`-prefixed search, read, test, lint, and compilation commands.

---

### Task 1: Define the canonical attachment schema

**Files:**
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/schema.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/id-assignment.md`
- Modify: `hermes_pipeline/harness.py`
- Modify: `tests/skill-test-environment/demo-project/TODOS.md`
- Test: `tests/test_todos_manager_skill_contract.py`

**Interfaces:**
- Consumes: the Plan/Spec/Reference meanings in `CONTEXT.md` and ADR 0001.
- Produces: one canonical field order and wording used by generated preambles, fixtures, and later workflow tasks.

- [ ] **Step 1: Write failing schema-copy tests**

Create `tests/test_todos_manager_skill_contract.py` with helpers that load packaged skill resources and assert that every canonical preamble names the three fields and no longer contains the old revise-only rule:

```python
from importlib.resources import files
from pathlib import Path

import hermes_pipeline.harness as harness


DATA = files("hermes_pipeline").joinpath("data", "skills", "todos-manager")


def skill_text(relative: str) -> str:
    return DATA.joinpath(*relative.split("/")).read_text(encoding="utf-8")


def test_schema_defines_document_attachment_roles():
    schema = skill_text("sections/schema.md")
    assert "| **Plan:** |" in schema
    assert "execution authority" in schema
    assert "outcome contract" in schema
    assert "supplementary" in schema


def test_schema_copies_do_not_claim_spec_reference_are_revise_only():
    copies = [
        skill_text("sections/schema.md"),
        skill_text("sections/id-assignment.md"),
        Path("tests/skill-test-environment/demo-project/TODOS.md").read_text(),
        Path(harness.__file__).read_text(),
    ]
    for text in copies:
        assert "Spec:**/**Reference:** are `--revise`-only" not in text
        assert "**Plan:**" in text
```

- [ ] **Step 2: Run the new tests and confirm the old contract fails**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py -q`

Expected: FAIL because `Plan:` is absent and the revise-only wording remains.

- [ ] **Step 3: Update the canonical schema and every preamble copy**

Add `Plan:` before `Spec:` in optional-field lists. Define exact cardinality and roles:

```markdown
| **Plan:** | Single repository-relative path to executable implementation instructions. The sole actionability gate. |
| **Spec:** | Single repository-relative path to the authoritative outcome contract. |
| **Reference:** | Comma-separated repository-relative paths to supplementary context; literal commas are not allowed in a path. |
```

Replace all revise-only wording with: attachments may be proposed by `--add` or `--revise`, but require explicit user confirmation. Preserve the existing canonical section boundaries and `NEXT_TODO_ID` rules byte-for-byte outside these field descriptions.

- [ ] **Step 4: Run schema and preamble regression tests**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/skill-test-environment/unit/test_format_validation.py tests/test_todos_md.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the schema contract**

```bash
git add hermes_pipeline/data/skills/todos-manager/sections/schema.md hermes_pipeline/data/skills/todos-manager/sections/id-assignment.md hermes_pipeline/harness.py tests/skill-test-environment/demo-project/TODOS.md tests/test_todos_manager_skill_contract.py
git commit -m "Define TODO document attachment schema"
```

### Task 2: Add reusable document discovery and validation rules

**Files:**
- Create: `hermes_pipeline/data/skills/todos-manager/sections/document-attachments.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/auto-research.md`
- Test: `tests/test_todos_manager_skill_contract.py`

**Interfaces:**
- Consumes: title, summary, TODO ID when available, explicit context paths, Git status, remaining research read/search budget, and existing attachment values.
- Produces: up to five records shaped as `{path, roles, relevance_reason, source, validation}` plus unresolved/none state for each role.

- [ ] **Step 1: Add failing assertions for the shared policy section**

Extend `tests/test_todos_manager_skill_contract.py`:

```python
def test_document_attachment_policy_is_shared_and_bounded():
    policy = skill_text("sections/document-attachments.md")
    assert "explicit" in policy
    assert "changed or untracked" in policy
    assert "five qualified candidates" in policy
    assert "20 file reads" in policy
    assert "10 searches" in policy
    assert "symlink" in policy
    assert "repository-relative POSIX" in policy


def test_both_workflows_route_to_shared_attachment_policy():
    skill = skill_text("SKILL.md")
    assert "sections/document-attachments.md" in skill
    assert "--add" in skill and "--revise" in skill
```

- [ ] **Step 2: Run the policy tests and confirm failure**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py -q`

Expected: FAIL because the shared section does not exist.

- [ ] **Step 3: Write the complete shared attachment policy**

Create `sections/document-attachments.md` with explicit subsections for discovery order, excluded trees, first-class and semantic Plan qualification, strong relevance signals, role classification, combined Plan+Spec handling, normalized path validation, Reference comma rejection, candidate records, ambiguity states, existing-value preservation, and exact validation errors. Include this normative algorithm:

```text
resolve candidate against repository root
reject unless the lexical input is relative
reject unless the resolved target is inside the resolved repository root
reject unless the target exists and is a regular file
store target.relative_to(repository_root).as_posix()
```

State that explicit paths consume the shared read budget first, up to five reads are reserved for attachments, discovery ends at five qualified candidates, and incomplete discovery is disclosed rather than extending the 20-read/10-search cap.

- [ ] **Step 4: Route both workflows through the new section**

Add the section to `SKILL.md`'s routing table for both `--add` and `--revise`. Remove the old exception forbidding Spec/Reference suggestions. Update `auto-research.md` so attachment discovery shares its counters instead of starting a second budget.

- [ ] **Step 5: Run the contract tests**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the shared policy**

```bash
git add hermes_pipeline/data/skills/todos-manager/SKILL.md hermes_pipeline/data/skills/todos-manager/sections/auto-research.md hermes_pipeline/data/skills/todos-manager/sections/document-attachments.md tests/test_todos_manager_skill_contract.py
git commit -m "Add bounded TODO document discovery policy"
```

### Task 3: Integrate Plan selection into `--add`

**Files:**
- Modify: `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/auto-research.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`
- Test: `tests/test_todos_manager_skill_contract.py`

**Interfaces:**
- Consumes: qualified attachment records from `sections/document-attachments.md`.
- Produces: a `Plan:` synthesis value in one of `none detected`, one suggested path, or unresolved multiple-choice state; the preview emits `Plan:` only after user resolution.

- [ ] **Step 1: Add failing `--add` interaction contract tests**

```python
def test_add_contract_covers_zero_one_and_multiple_plan_candidates():
    skill = skill_text("SKILL.md")
    scenarios = skill_text("sections/acceptance-scenarios.md")
    for phrase in ("Plan: none detected", "one candidate", "multiple candidates"):
        assert phrase in skill or phrase in scenarios
    assert "confirm" in scenarios
    assert "unresolved" in scenarios
    assert "none" in scenarios
```

- [ ] **Step 2: Run the test and confirm failure**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py -q`

Expected: FAIL because `--add` has no Plan state machine.

- [ ] **Step 3: Extend the synthesis and confirmation state machine**

Add `Plan:` to the synthesis block after `Context:`. Specify:

```text
zero qualified Plans -> display "Plan: none detected"; omit Plan from entry
one qualified Plan -> display suggested path plus relevance reason; require confirm or edit
multiple qualified Plans -> display numbered paths plus reasons; mark unresolved
unresolved + confirm -> reject only Plan and request select/path/none
manual path -> run shared validation without rerunning research
none -> omit Plan and permit a non-actionable TODO
```

Keep the existing batch edit and final preview gates. Do not introduce a separate yes/no prompt or write before `y`.

- [ ] **Step 4: Add executable acceptance scenarios**

Add scenarios for zero, one, and multiple candidates; an explicit current-context path; manual valid untracked path; invalid path correction; budget exhaustion; and cancellation proving `TODOS.md` remains byte-for-byte unchanged.

- [ ] **Step 5: Run focused tests**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/skill-test-environment/unit -q`

Expected: PASS.

- [ ] **Step 6: Commit the add workflow**

```bash
git add hermes_pipeline/data/skills/todos-manager/SKILL.md hermes_pipeline/data/skills/todos-manager/sections/auto-research.md hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md tests/test_todos_manager_skill_contract.py
git commit -m "Add confirmed Plan selection to todos add"
```

### Task 4: Add post-planning document attachment to `--revise`

**Files:**
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/revise.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`
- Test: `tests/test_todos_manager_skill_contract.py`

**Interfaces:**
- Consumes: existing attachment values and qualified Plan, Spec, and Reference candidates from the shared policy.
- Produces: an explicitly confirmed replacement/preservation/removal for singleton roles and append/deduplicate behavior for References.

- [ ] **Step 1: Add failing revise contract tests**

```python
def test_revise_contract_preserves_and_explicitly_mutates_attachments():
    revise = skill_text("sections/revise.md")
    required = [
        "Plan", "Spec", "Reference", "preserve", "replace", "remove",
        "append", "deduplicate", "combined Plan", "invalid existing",
    ]
    for phrase in required:
        assert phrase in revise
```

- [ ] **Step 2: Run the test and confirm failure**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py -q`

Expected: FAIL because revise currently excludes document suggestions and exits when ordinary fields have no gaps.

- [ ] **Step 3: Make attachments independently revisable**

Change the no-gaps exit so `--revise` still offers document discovery when invoked to attach planning outputs. Add Plan, Spec, and Reference rows to the revision synthesis. Preserve existing singleton values by default; require `Plan: replace <path>`, `Spec: replace <path>`, or an explicit remove action to mutate them. Append new References in existing order, normalize before deduplication, and require explicit removal. Warn about invalid existing paths without blocking unrelated edits.

- [ ] **Step 4: Define combined-role and ambiguity handling**

Offer `attach as Plan and Spec` only when the same validated document strongly qualifies for both roles. Reject confirmation while candidate roles remain ambiguous. Never add a Plan or Spec path to Reference, and never rerun discovery after an edit-path validation failure.

- [ ] **Step 5: Add revise acceptance scenarios**

Cover the three-session flow (add TODO, create/finalize planning docs, revise TODO), explicit invoking-skill paths, Git-changed fallback, bounded conventional search, combined role, preservation, replacement, removal, Reference append/deduplication, invalid existing warnings, and cancel/no-write behavior.

- [ ] **Step 6: Run focused tests**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/skill-test-environment/unit -q`

Expected: PASS.

- [ ] **Step 7: Commit the revise workflow**

```bash
git add hermes_pipeline/data/skills/todos-manager/sections/revise.md hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md tests/test_todos_manager_skill_contract.py
git commit -m "Attach planning documents during TODO revision"
```

### Task 5: Validate audit behavior and packaged installation parity

**Files:**
- Modify: `hermes_pipeline/data/skills/todos-manager/SKILL.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/error-messages.md`
- Modify: `hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md`
- Modify: `tests/test_skills_install.py`
- Test: `tests/test_todos_manager_skill_contract.py`

**Interfaces:**
- Consumes: canonical attachment validation from `sections/document-attachments.md`.
- Produces: non-mutating audit findings for invalid attachments and proof that installation copies every required policy file unchanged.

- [ ] **Step 1: Add failing audit and package parity tests**

Extend the contract tests to require audit wording for missing files, containment escape, directory targets, symlink escape, and Reference comma rejection. Extend `test_todos_manager_skill_is_packaged_data` to include `document-attachments.md`, then add an install test that compares the installed `SKILL.md` and every installed `sections/*.md` byte-for-byte with `importlib.resources`.

```python
def test_todos_manager_skill_is_packaged_data():
    section_names = {p.name for p in DATA.joinpath("sections").iterdir()}
    assert "document-attachments.md" in section_names
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/test_skills_install.py -q`

Expected: FAIL because audit does not validate attachments and the new section is absent.

- [ ] **Step 3: Add non-mutating audit reporting**

Teach the audit contract to validate present attachment values using the shared path rules, report one path-specific finding per defect, and never require, remove, replace, or repair attachments. Add exact path-plus-remediation examples to `error-messages.md`.

- [ ] **Step 4: Complete acceptance coverage**

Add a coverage table mapping every requirement in the authoritative spec to an acceptance scenario or contract test. Explicitly mark runtime selection/worktree behavior as TODO-39 and therefore outside this suite.

- [ ] **Step 5: Run the complete relevant suite**

Run: `rtk uv run pytest tests/test_todos_manager_skill_contract.py tests/test_skills_install.py tests/skill-test-environment/unit tests/test_todos_md.py -q`

Expected: PASS.

- [ ] **Step 6: Verify a fresh installed copy**

Run: `rtk uv run tpo skills install --target codex --scope project --reinstall`

Then compare the project-installed copy against the packaged source using the byte-for-byte test added in Step 1. Do not edit an installed mirror directly.

- [ ] **Step 7: Run repository verification**

Run: `rtk uv run pytest -q`

Expected: the full suite passes with no failures.

Run: `rtk git diff --check`

Expected: no output and exit status 0.

- [ ] **Step 8: Commit audit and distribution coverage**

```bash
git add hermes_pipeline/data/skills/todos-manager/SKILL.md hermes_pipeline/data/skills/todos-manager/sections/error-messages.md hermes_pipeline/data/skills/todos-manager/sections/acceptance-scenarios.md tests/test_skills_install.py tests/test_todos_manager_skill_contract.py
git commit -m "Verify TODO attachment audit and installation"
```
