# Skill Test Environment

Phase 1: Structural unit tests for todos-manager skill logic.

## Quick Start

```bash
# Run all unit tests
uv run pytest tests/skill-test-environment/unit/ -v

# Run a single test file
uv run pytest tests/skill-test-environment/unit/test_id_sequencing.py -v
```

## Structure

- `demo-project/` — TODOS.md fixtures with diverse entries
- `golden/` — YAML assertion descriptors for structural verification
- `unit/` — Deterministic unit tests (zero token cost, <5s)
- `skill_logic.py` — Pure-Python implementation of skill schema rules
- `verify.py` — Golden file loader + structural assertion runner
- `conftest.py` — Shared pytest fixtures (all prefixed with `skill_`)

## Golden Files

Each golden YAML file declares structural assertions for one subcommand:
- `add_happy_path.yaml` — entry count, ID sequence, preamble
- `init_output.yaml` — file creation, headers
- `audit_report.yaml` — entry count, issue detection
- `archive_result.yaml` — entries moved, IDs preserved
- `convert_result.yaml` — preamble insertion, field flags

## Phase 2 (Deferred)

Agent-driven integration tests with AI-judged semantic validation.
