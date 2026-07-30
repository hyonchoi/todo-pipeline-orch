# Executable Harness Fixture Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the harness happy-path TODO concrete enough for Autoplan to execute without clarification.

**Architecture:** Keep pipeline prompts and agent ambiguity checks unchanged. Strengthen the source fixture in `hermes_pipeline/harness.py`, with a regression test that reads the generated project's committed `TODOS.md`.

**Tech Stack:** Python 3.12+, pytest, uv, Git

## Global Constraints

- The fixture defines a named Python module and function.
- The fixture defines concrete input and return types.
- The fixture defines deterministic normalization and empty-input behavior.
- The fixture uses no external dependencies.
- The fixture includes executable acceptance criteria.
- Do not duplicate fixture requirements in phase YAML or prompt wrappers.
- Do not weaken Autoplan's ambiguity handling.

---

### Task 1: Make the happy-path fixture executable

**Files:**
- Modify: `tests/test_harness.py:34-42`
- Modify: `hermes_pipeline/harness.py:92-95`

**Interfaces:**
- Consumes: `create_mock_project(path: Path, fixture_name: str) -> dict[str, Any]`
- Produces: A generated `TODOS.md` whose TODO-1 entry fully specifies `mock_transform.normalize_names(names: list[str]) -> list[str]`

- [ ] **Step 1: Write the failing regression test**

Add this test beside `test_create_mock_project_happy_path`:

```python
def test_create_mock_project_happy_path_has_executable_todo(self, tmp_path: Path):
    create_mock_project(tmp_path, "happy-path")

    todos = (tmp_path / "TODOS.md").read_text()
    required_contract = (
        "mock_transform.py",
        "normalize_names(names: list[str]) -> list[str]",
        "strip surrounding whitespace",
        "discard empty strings",
        "preserve input order",
        "return an empty list",
        "standard library only",
        "**Acceptance criteria:**",
        "uv run pytest",
    )

    for requirement in required_contract:
        assert requirement in todos
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
rtk uv run pytest tests/test_harness.py::TestCreateMockProject::test_create_mock_project_happy_path_has_executable_todo -q
```

Expected: FAIL because `mock_transform.py` and the remaining contract text are absent from the current fixture.

- [ ] **Step 3: Enrich the mock TODO with the minimal executable contract**

Replace the current three-line TODO entry returned by `_get_todos_for_fixture()` with:

```python
"- [ ] **TODO-1: Implement mock name normalization** — adds a deterministic Python data transformation\n"
"  - **What:** Create `mock_transform.py` with `normalize_names(names: list[str]) -> list[str]`. For each input string, strip surrounding whitespace, discard empty strings after stripping, lowercase the remaining value, and preserve input order. Return an empty list for empty input.\n"
"  - **Why:** Provide a small, executable feature that exercises the complete harness pipeline without external services.\n"
"  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `feat/mock-happy-path`, Language `Python 3.12+`, Dependencies `standard library only`, Test Coverage `required`, Security Review `not-required`\n"
"  - **Acceptance criteria:** `normalize_names([\" Alice \", \"\", \"BOB\"])` returns `[\"alice\", \"bob\"]`; `normalize_names([])` returns `[]`; tests run with `uv run pytest`.\n"
```

- [ ] **Step 4: Run the focused test and harness fixture tests**

Run:

```bash
rtk uv run pytest tests/test_harness.py::TestCreateMockProject -q
```

Expected: all `TestCreateMockProject` tests PASS.

- [ ] **Step 5: Run the full locked test suite**

Run:

```bash
rtk uv run --locked pytest -q
```

Expected: the full suite PASSes with no new warnings or errors.

- [ ] **Step 6: Commit the verified fix**

Propose and create one atomic commit containing only:

```bash
git add tests/test_harness.py hermes_pipeline/harness.py
git commit -m "Fix executable harness fixture contract"
```

- [ ] **Step 7: Report retained-task semantics**

Re-inspect task `t_662d6f33` with:

```bash
rtk hermes kanban show t_662d6f33 --json
```

Report that it remains blocked because it belongs to the retained pre-fix harness workspace. Do not mutate it. The corrected fixture is exercised by a fresh harness run.
