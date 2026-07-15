# Explanation: Skill Test Harness Design

Design rationale for the pure-Python golden-file test architecture.

## Problem Statement

The `todos-manager` skill enforces a complex, multi-rule schema for TODOS.md entries:

- **ID sequencing** — Must compute `max(all IDs) + 1` atomically across two files
- **Field validation** — Required fields (What, Why, Decisions), optional fields (Pros, Cons, Context, etc.)
- **Status markers** — Only `[ ]`, `[→]`, `[x]`, `[~]` allowed
- **Dependency resolution** — References to non-existent TODO-N IDs must be detected
- **Archive logic** — Moving completed entries preserves IDs, headers, order

Verifying this behavior requires either:
1. Running the actual Anthropic skill (requires API tokens, Hermes setup, slow, non-deterministic)
2. Mocking the skill (requires maintaining a mock implementation, duplicates logic, error-prone)
3. **Pure-Python implementations with golden-file assertions** (our choice)

## Architecture Decision: Pure-Python + Golden Files

### Why Not Mock the Skill?

**Rejected:** Skill mocking would require duplicating skill logic in test code. This creates two sources of truth:
- The real skill (in `skills/todos-manager/SKILL.md`)
- The mock implementation (in test code)

When the skill is updated, both must be synced. Single source of truth is better.

### Why Not Just Unit Test the Skill Directly?

**Risk:** Invoking the skill during tests:
- Requires Hermes setup and authentication
- Uses API tokens (not free)
- Non-deterministic (depends on model output, latency, transient errors)
- Cannot run offline
- Slow (seconds per test vs. milliseconds)

### Why Pure-Python + Golden Files?

**Benefits:**
- **Zero token cost** — No skill invocation
- **Deterministic** — Same input always produces same output
- **Fast** — Entire test suite runs in <5 seconds
- **Offline** — Works without network or auth
- **Single source of truth** — `skill_logic.py` is the oracle; golden files declare expected outcomes
- **Explicit contracts** — Golden YAML files document what each operation should achieve

**Tradeoff:** We maintain Python implementations of skill rules alongside the skill itself. This is acceptable because:
1. Skill rules are deterministic, not AI-driven
2. Rules rarely change (stable contract)
3. Python code is simpler than the skill's AI reasoning
4. Benefit (instant feedback, zero cost) outweighs the maintenance burden

## Golden File Design

Golden files are YAML assertions about file structure, not AI-judged semantic correctness.

### Example: `add_happy_path.yaml`

```yaml
subcommand: add
description: "Verify TODOS.md after adding one new entry via --add"
preconditions:
  - "Starts with demo-project TODOS.md (6 entries, max ID 7)"
  - "TODOS-archive.md exists with TODO-5"
assertions:
  - file_exists: TODOS.md
  - regex_count:
      pattern: "^- \\[.\\] TODO-\\d+:"
      count: 7
  - regex_present: "^- \\[ \\] TODO-8:"
  - preamble_present: true
  - max_id: 8
  - no_duplicate_ids: true
```

**Why YAML, not hardcoded Python assertions?**

- **Declarative** — Assertions are readable without running code
- **Reusable** — Multiple tests can use the same golden file
- **Extensible** — Add new assertion types to `verify.py` without touching test code
- **Git-friendly** — Diffs show exactly what changed in expected behavior

**Why assertions are structural, not semantic:**

We can verify structure deterministically:
- ✅ "Entry count is 7"
- ✅ "Max ID is 8"
- ✅ "No missing required fields"
- ✅ "Dependency TODO-5 exists"

We cannot verify semantics at zero cost:
- ❌ "The title accurately describes the work"
- ❌ "The Why field is convincing"
- ❌ "The Decisions are sound"

(Semantic validation is deferred to Phase 2 — agent-driven integration tests with AI judgment.)

## Phase 1 vs. Phase 2

This harness implements **Phase 1** — structural unit tests. Phase 2 (deferred) would add agent-driven integration testing.

### Phase 1 (Current): Structural Unit Tests

- **Scope:** Deterministic skill logic (ID, parsing, validation, archive)
- **Execution:** Pure Python, golden files, pytest
- **Cost:** Zero tokens
- **Speed:** <5 seconds for full suite
- **What it catches:**
  - ID sequencing bugs
  - Schema validation failures
  - Archive corruption
  - Regex parsing errors
  - Field requirement violations

- **What it doesn't catch:**
  - AI reasoning quality
  - Semantic correctness of entries
  - Skill integration (e.g., Hermes CLI invocation)
  - Side effects (file I/O, state mutations in skill)

### Phase 2 (Deferred): Agent-Driven Integration Tests

Planned but not yet implemented. Would add:

```
tests/skill-test-environment/integration/
├── conftest.py              # Agent-specific fixtures (Hermes CLI, temp projects)
├── test_add_via_hermes.py   # Invoke actual skill via hermes CLI
├── test_archive_via_hermes.py
├── test_audit_via_hermes.py
└── fixtures/
    └── scenarios/           # Multi-step interaction scenarios
```

**Phase 2 would:**
- Invoke the real skill via Hermes CLI
- Test full end-to-end behavior (CLI parsing, file I/O, state mutations)
- Use AI to judge semantic correctness ("Is the entry quality good?")
- Verify the skill's subcommand orchestration

**Why Phase 2 is deferred:**
- Phase 1 catches 80% of bugs at zero cost
- Phase 2 is slow and expensive
- Hermes integration is still evolving; wait for stability
- AI-judged tests are flaky; need better evaluation patterns

**Trigger for Phase 2:** When skill integration is stable and we're confident in golden files (likely v2.2+).

## Fixture Architecture

### Demo-Project Fixture

`tests/skill-test-environment/demo-project/` contains reference TODOS.md and TODOS-archive.md files.

**Design:**
- One fixture serves all tests (DRY principle)
- Fixture is diverse (multiple entry types, fields, IDs, status markers)
- Fixture is stable (only changed when schema changes)
- Pytest fixtures load fixture content on-demand (efficient)

**Why not separate fixture per test?**
- Slows test discovery and execution
- Makes comparison between tests harder (different baselines)
- Violates DRY; fixture diversity needs to be in one place

### Fixture Prefixing Strategy

All fixtures prefixed with `skill_` (e.g., `skill_demo_dir`, `skill_golden_dir`).

**Why?**
- Parent `tests/conftest.py` may also define demo fixtures
- Prefix avoids collision and makes origin clear
- Pytest auto-discovers conftest.py at all levels, so namespace isolation is necessary

**If collision occurs:**
- Pytest will raise an error (duplicate fixture name)
- Rename the fixture with the `skill_` prefix to disambiguate
- Update all test imports to use new name

## Extension Points

### Adding a New Assertion Type

If golden files need to check something `verify.py` doesn't support:

1. **Add to `run_structural()` in `verify.py`:**
   ```python
   elif "my_new_check" in assertion:
       result["pass"] = perform_check(todos_text, assertion["my_new_check"])
   ```

2. **Write a unit test in `test_verify.py`:**
   ```python
   def test_my_new_check():
       golden = {"assertions": [{"my_new_check": True}]}
       result = run_structural(golden, "test content")
       assert result["failed"] == 0
   ```

3. **Document it in the Reference guide**

### Adding a New skill_logic.py Function

If a new skill subcommand needs deterministic logic:

1. **Add the function to `skill_logic.py`**
2. **Unit test it immediately (in appropriate test file)**
3. **Use it in golden assertions via `verify.py`**

### Adding a New Golden File

For a new skill scenario or subcommand variant:

1. **Create `golden/new_scenario.yaml`** with assertions
2. **Write a test in `test_verify.py`** that loads and verifies it
3. **Run test to ensure assertions are achievable**

## Testing Philosophy

### Determinism Over Coverage

We optimize for determinism and speed over comprehensive coverage.

**Example:** We test ID sequencing with edge cases (gaps, archive-only IDs, empty files) rather than property-based testing with 1000 random inputs. Each test is fast and explicit.

### Structural, Not Semantic

We verify the skill enforces its schema, not that entries are "good."

**Acceptable assertions:**
- "Has 3 required fields"
- "No IDs appear twice"
- "Dependency TODO-5 exists"

**Out of scope:**
- "The title is descriptive"
- "The Why is convincing"
- "Decisions are sound"

### Fixtures as Reference Implementations

`skill_logic.py` functions are not just tests — they're the reference implementation of skill rules. The skill itself should match these rules exactly.

**Implication:** When the skill is updated, update `skill_logic.py` first, then verify the skill matches.

## Maintenance Burden

### What Needs Updating When?

| Change | Action |
|--------|--------|
| Skill adds new rule | Update `skill_logic.py` + write unit tests + add golden file assertion |
| Skill changes rule | Update `skill_logic.py` + update unit tests + update golden files |
| Schema field changes | Update demo-project fixture + update golden files + update `REQUIRED_FIELDS` |
| New subcommand added | Add new `skill_logic.py` function + unit tests + golden file |

### Typical Effort

- **Add new function** — 2 hours (implementation, tests, golden files, documentation)
- **Fix a bug caught by test** — 30 minutes (fix in `skill_logic.py`, run tests, commit)
- **Update for schema change** — 1 hour (fixture, golden files, constants, tests)

### Cost-Benefit

**Cost:** Maintaining two code paths (skill + `skill_logic.py`), syncing when skill changes

**Benefit:** 
- Instant feedback loop (no Hermes wait)
- Zero token usage
- Offline testing
- Catches bugs before skill invocation

**Verdict:** Cost is worth it for early development. Revisit in Phase 2 if maintenance becomes burdensome.

## See Also

- [Reference: Skill Test Harness API](reference-skill-test-harness.md) — Complete function signatures
- [How To: Skill Test Environment](howto-skill-test-environment.md) — Task-oriented guide for adding tests
- [ARCHITECTURE.md](ARCHITECTURE.md#todos-manager-skill-v21) — Skill requirements and design
