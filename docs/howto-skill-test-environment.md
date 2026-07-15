# How To: Skill Test Environment

Task-oriented guide for adding and maintaining tests in the skill test harness.

## Prerequisites

- Python 3.12+, managed via `uv`
- Familiarity with TODOS.md schema (see [CLAUDE.md](../CLAUDE.md) "TODOS.md management" section)
- `pytest` (included in project dependencies)

## Quick Start: Run Existing Tests

All unit tests are deterministic and run in <5 seconds with zero token cost.

```bash
# Run all unit tests
uv run pytest tests/skill-test-environment/unit/ -v

# Run a single test file (e.g., ID sequencing)
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -v

# Run a single test
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py::TestScanIds::test_scans_all_ids_from_fixture -v
```

All tests should pass on a clean checkout.

## Task 1: Add a New Unit Test

**Scenario:** The skill now supports a new field or validation rule, and you want to add a unit test.

### Step 1: Choose or Create a Test File

Test files are organized by feature area:
- `test_id_sequencing.py` — ID parsing and counter cache
- `test_entry_parsing.py` — Entry structure and field extraction
- `test_format_validation.py` — Schema validation (required fields, status markers)
- `test_archive_logic.py` — Entry archiving and completion
- `test_verify.py` — Golden file loading and assertion running

If your feature spans multiple files or doesn't fit existing files, create a new one:
```bash
touch tests/skill-test-environment/unit/test_my_feature.py
```

### Step 2: Write Your Test Class

Tests are organized into `TestClassName` classes, one per function/feature.

```python
"""Tests for your new feature."""

import pytest
from pathlib import Path
from tests.skill_test_environment.skill_logic import your_new_function


class TestYourNewFeature:
    """Describe what you're testing."""
    
    def test_happy_path(self, skill_demo_dir):
        """Test the common case."""
        # Use skill_demo_dir fixture to access demo-project files
        todos = (skill_demo_dir / "TODOS.md").read_text()
        result = your_new_function(todos)
        assert result is not None
    
    def test_edge_case(self):
        """Test an edge case with minimal input."""
        result = your_new_function("")
        assert result == expected_value
```

### Step 3: Use Available Fixtures

Four fixtures are available in `conftest.py`:

| Fixture | Type | Purpose |
|---------|------|---------|
| `skill_demo_dir` | `Path` | Directory path to `demo-project/` |
| `skill_golden_dir` | `Path` | Directory path to `golden/` |
| `skill_demo_todos` | `str` | Content of demo-project TODOS.md |
| `skill_demo_archive` | `str` | Content of demo-project TODOS-archive.md |

```python
def test_with_demo_project(skill_demo_todos):
    """Parse the demo-project TODOS.md."""
    from tests.skill_test_environment.skill_logic import parse_entries
    entries = parse_entries(skill_demo_todos)
    assert len(entries) == 6  # Demo has 6 entries
```

### Step 4: Run Your Test

```bash
uv run pytest tests/skill-test-environment/unit/test_my_feature.py -v
```

Expected output:
```
tests/skill-test-environment/unit/test_my_feature.py::TestYourNewFeature::test_happy_path PASSED
```

### Step 5: Commit

```bash
git add tests/skill-test-environment/unit/test_my_feature.py
git commit -m "test: add unit test for your new feature"
```

## Task 2: Add a New Golden File

**Scenario:** You've added a new skill subcommand or scenario (e.g., `--revise`) and want structural tests.

### Step 1: Create the Golden YAML

Golden files declare expected structural outcomes for a scenario. Create a new file in `golden/`:

```bash
touch tests/skill-test-environment/golden/revise_field.yaml
```

### Step 2: Write the Assertions

Each golden file has three sections:

```yaml
# Golden assertions for --revise subcommand
# Scenario: Revising an entry to add missing field

subcommand: revise
description: "Verify TODOS.md after revising TODO-1 to add missing Why field"

preconditions:
  - "Starts with demo-project TODOS.md"
  - "TODO-1 missing **Why:** field"

assertions:
  - file_exists: TODOS.md
  - total_entries: 6
  - regex_present: "TODO-1.*Why"
  - all_required_fields_present: true
  - no_broken_dependency_refs: true
```

**Supported assertions:** See the [Reference](reference-skill-test-harness.md#supported-assertions) section for the complete list.

### Step 3: Add a Unit Test

Create a test in `test_verify.py` or a dedicated test file that loads and runs your golden file:

```python
def test_revise_golden(skill_demo_dir, skill_golden_dir):
    """Verify --revise scenario."""
    from tests.skill_test_environment.verify import assert_golden
    
    # Simulate the revise operation on demo-project
    todos = (skill_demo_dir / "TODOS.md").read_text()
    archive = (skill_demo_dir / "TODOS-archive.md").read_text()
    
    # Apply your revise logic
    revised_todos = todos.replace("TODO-1: ", "TODO-1: ")  # Example
    
    # Assert against golden file
    assert_golden(
        skill_golden_dir / "revise_field.yaml",
        revised_todos,
        archive
    )
```

### Step 4: Run and Verify

```bash
uv run pytest tests/skill-test-environment/unit/test_verify.py::test_revise_golden -v
```

## Task 3: Update Demo Fixtures

**Scenario:** The TODOS.md schema has changed, and the demo-project fixtures are out of sync.

### Step 1: Update Demo Files

Demo fixtures are in `tests/skill-test-environment/demo-project/`:

```bash
# Edit the main fixture
vim tests/skill-test-environment/demo-project/TODOS.md

# Edit the archive (if needed)
vim tests/skill-test-environment/demo-project/TODOS-archive.md
```

### Step 2: Verify Tests Still Pass

```bash
uv run pytest tests/skill-test-environment/unit/ -v
```

If tests fail, update golden files to match your new fixture state (see Task 2).

### Step 3: Update Golden Files (if necessary)

If you added entries to the demo-project, update counts and max IDs in golden files:

```yaml
# Before: 6 entries, max ID 7
assertions:
  - total_entries: 6
  - max_id: 7

# After: 8 entries, max ID 9
assertions:
  - total_entries: 8
  - max_id: 9
```

### Step 4: Commit

```bash
git add tests/skill-test-environment/demo-project/
git add tests/skill-test-environment/golden/
git commit -m "test: update demo fixtures and golden files for new schema"
```

## Task 4: Debug a Failing Test

### Step 1: Run with Verbose Output

```bash
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py::TestScanIds::test_scans_all_ids_from_fixture -vv
```

The `-vv` flag shows full assertion details.

### Step 2: Inspect Fixtures

Print fixture content in your test:

```python
def test_something(skill_demo_todos):
    print("TODOS content:")
    print(skill_demo_todos)
    # Now run the test with -s flag to see prints
```

```bash
uv run pytest tests/skill-test-environment/unit/test_my_feature.py -s -v
```

### Step 3: Check Demo Files

All tests use the demo-project fixtures. If behavior is unexpected, inspect them:

```bash
cat tests/skill-test-environment/demo-project/TODOS.md
cat tests/skill-test-environment/demo-project/TODOS-archive.md
```

### Step 4: Test in Isolation

Create a minimal test file to isolate the problem:

```python
# tests/skill-test-environment/unit/test_debug.py
from pathlib import Path
from tests.skill_test_environment.skill_logic import your_function

def test_minimal():
    result = your_function("minimal input")
    print(f"Result: {result}")
    assert False  # Always fail to see output with -s
```

```bash
uv run pytest tests/skill-test-environment/unit/test_debug.py -s -v
```

Then remove `test_debug.py` when done.

## Task 5: Extend skill_logic.py

**Scenario:** The skill gains a new feature that requires a new pure-Python implementation function.

### Step 1: Add Your Function

Edit `tests/skill-test-environment/skill_logic.py`:

```python
def new_feature(todos_text: str) -> dict:
    """Implement the new feature logic.
    
    This mirrors skill behavior exactly — serves as test oracle.
    """
    result = {}
    # Implementation
    return result
```

### Step 2: Add Constants (if needed)

If your feature needs regex patterns or constants, add them at the module level:

```python
NEW_FEATURE_REGEX = re.compile(r"...")
NEW_FEATURE_DEFAULT = "value"
```

### Step 3: Test It

Create a unit test in the appropriate test file:

```python
def test_new_feature():
    from tests.skill_test_environment.skill_logic import new_feature
    result = new_feature("input")
    assert result is not None
```

### Step 4: Use in Golden Files

Once the function is tested, it can be used by `verify.py` for assertions. Add new assertion types to `verify.py` as needed (see the [Reference](reference-skill-test-harness.md#supported-assertions) for the pattern).

## Task 6: Add a New Assertion Type to verify.py

**Scenario:** Your golden file needs an assertion type that `verify.py` doesn't support yet.

### Step 1: Identify the Pattern

Write the assertion in your golden YAML:

```yaml
assertions:
  - my_new_check: true
  - my_new_check: {param1: value1, param2: value2}
```

### Step 2: Add Handling to run_structural()

Edit `tests/skill-test-environment/verify.py` in the `run_structural()` function:

```python
def run_structural(golden: dict, todos_text: str, archive_text: Optional[str] = None) -> dict:
    # ... existing code ...
    
    for assertion in assertions:
        result = {"assertion": assertion, "pass": True, "detail": ""}
        
        # ... existing elif branches ...
        
        elif "my_new_check" in assertion:
            # Implement your check
            check_value = assertion["my_new_check"]
            result["pass"] = perform_check(todos_text, check_value)
            result["detail"] = "check passed" if result["pass"] else "check failed"
        
        # ... rest of function ...
```

### Step 3: Test It

Write a unit test in `test_verify.py`:

```python
def test_my_new_check():
    from tests.skill_test_environment.verify import run_structural
    
    golden = {
        "assertions": [
            {"my_new_check": True}
        ]
    }
    
    result = run_structural(golden, "test content")
    assert result["failed"] == 0
```

### Step 4: Document It

Add your new assertion to [reference-skill-test-harness.md](reference-skill-test-harness.md#supported-assertions).

## Common Patterns

### Working with Entry Fields

```python
from tests.skill_test_environment.skill_logic import parse_entries

entries = parse_entries(todos_text)
for entry in entries:
    print(f"Entry {entry['id']}: {entry['title']}")
    for field_name, field_value in entry['fields'].items():
        print(f"  {field_name}: {field_value}")
```

### Simulating Skill Operations

The test harness provides simulators for skill operations:

```python
from tests.skill_test_environment.skill_logic import simulate_archive

todos = "- [ ] TODO-1: Open\n- [x] TODO-2: Done"
archive = ""

new_todos, new_archive = simulate_archive(todos, archive)
# Now new_todos has only TODO-1, new_archive has TODO-2
```

### Checking for Validation Issues

```python
from tests.skill_test_environment.skill_logic import validate_all_entries

validation = validate_all_entries(todos_text)
for entry_result in validation:
    if entry_result["issues"]:
        print(f"TODO-{entry_result['id']} has issues:")
        for issue in entry_result["issues"]:
            print(f"  - {issue}")
```

## Troubleshooting

### "ModuleNotFoundError: No module named 'tests.skill_test_environment'"

Make sure you're in the project root and using `uv run pytest`:

```bash
cd /path/to/todo-pipeline-orchestrator/.claude/worktrees/mock-integration-test-harness
uv run pytest tests/skill-test-environment/unit/ -v
```

### "FileNotFoundError: demo-project/TODOS.md"

Tests use relative paths from the project root. Run pytest from the project root, not from the tests directory.

### Golden File Assertion Fails

1. Check the actual vs. expected in the failure message
2. Verify your demo-project fixtures match the golden file's preconditions
3. Update golden file assertions if the fixture changed (see Task 3)

### Import Errors in Custom Tests

Imports must use full paths from project root:

```python
# Correct
from tests.skill_test_environment.skill_logic import parse_entries

# Wrong (will fail)
from skill_logic import parse_entries
```

## See Also

- [Reference: Skill Test Harness API](reference-skill-test-harness.md) — Complete function signatures and examples
- [Explanation: Skill Test Harness Design](explanation-skill-test-harness-design.md) — Why this architecture, Phase 2 plans
- [CLAUDE.md](../CLAUDE.md) — TODOS.md schema and skill requirements
