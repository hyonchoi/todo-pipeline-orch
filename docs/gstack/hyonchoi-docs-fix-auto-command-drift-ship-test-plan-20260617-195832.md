# Ship Test Plan: docs/fix-auto-command-drift

**Branch:** docs/fix-auto-command-drift
**Base:** main
**Date:** 2026-06-17
**Auditor:** Claude Code

## Summary

| Metric | Value |
|--------|-------|
| Test files before | 27 |
| Test files after | 31 |
| Tests before | 327 |
| Tests after | 376 |
| New tests added | 49 |
| Coverage % | 87.5% |
| Coverage gate (60% min / 80% target) | PASS |
| Uncovered gaps | 12 |

## Changed Source Files

1. **outcomes.py** (NEW) — Shared outcome string constants
2. **circuit.py** — Added `observe_from_outcomes()` method
3. **kanban_tasks.py** (NEW) — Kanban task registration, status queries, outcome observation
4. **kanban.py** — set_active_task refactored: `--board` -> `--tenant`, JSON output parsing
5. **decision/context.py** — Kanban-aware in-flight detection, `_fetch_kanban_snapshot`, `_kanban_snapshot`
6. **cli.py** — Tick subcommand, helper functions (`_read_prior_tick_id`, `_generate_tick_id`, etc.)

## Coverage by Module

### outcomes.py — 100% (constants only, no logic)
No codepaths to test. Constants are referenced throughout the codebase.

### circuit.py — 100% (observe_from_outcomes)
All 10 codepaths covered by 12 tests in test_circuit.py.

### kanban_tasks.py — 90.2% (46/51 codepaths)
Gaps: 5 uncovered paths in registration and fallback parsing.

### kanban.py — 70% (7/10 codepaths)
Gaps: 3 uncovered paths (JSON parse error in set_active_task, legacy outbox fallback, _ensure_board removed).

### decision/context.py — 91.4% (65/71 codepaths)
Gaps: 6 uncovered paths in in-flight detection and context building.

### cli.py — 90% (63/70 codepaths)
Gaps: 7 uncovered paths in TOML overlay loading and sentinel writing.

## Remaining Gaps (12)

1. **kanban_tasks.py: legacy CLI "Created t_xxx" fallback** — Old Hermes CLI returns plaintext; parsing falls back to extracting task ID from "Created t_xxx" format.
2. **kanban_tasks.py: unparseable stdout** — Output is neither JSON nor "Created t_xxx" — raises RuntimeError.
3. **kanban_tasks.py: todo_id validation** — Invalid format like "INVALID" or "TODO-10; rm -rf /" rejected before subprocess calls.
4. **kanban_tasks.py: goal flags** — --goal and --goal-max-turns flags in command.
5. **kanban_tasks.py: _archive_tasks exception** — Best-effort archive; FileNotFoundError swallowed.
6. **kanban_tasks.py: tick_started sentinel** — No kanban tasks + tick_started sentinel + state_dir -> True.
7. **kanban_tasks.py: sentinel JSON error** — Sentinel file with invalid JSON -> caught, return False.
8. **kanban.py: JSON decode error in set_active_task** — Non-JSON output from kanban create raises parse error.
9. **kanban.py: missing "id" key in JSON** — JSON without "id" key raises parse error.
10. **decision/context.py: _extract_in_flight_ids list format** — Bare list snapshot (not dict with "tasks" key).
11. **decision/context.py: _fetch_kanban_snapshot error cases** — CLI failure, timeout, JSON error, non-zero return code.
12. **cli.py: _load_toml_overlay error cases** — Missing config file, exception during load, valid config.

## Test Files Added

| File | Tests | Module |
|------|-------|--------|
| tests/test_kanban_tasks_legacy.py | 14 | kanban_tasks legacy paths |
| tests/test_decision_context_edge.py | 21 | decision/context edge cases |
| tests/test_kanban_json_parse.py | 4 | kanban JSON parse errors |
| tests/test_tick_subcommand_edge.py | 10 | tick subcommand edge cases |

## Gate Check

- **Minimum 60%:** 87.5% >= 60% -> PASS
- **Target 80%:** 87.5% >= 80% -> PASS
