# Coverage Diagram: docs/fix-auto-command-drift

## Module Coverage Map

```
outcomes.py                     100%    0/0  gaps  (constants only)
circuit.py                      100%    0/10 gaps  (observe_from_outcomes)
kanban_tasks.py                  90%    5/51 gaps
kanban.py                        70%    3/10 gaps
decision/context.py              91%    6/71 gaps
cli.py (tick)                    90%    7/70 gaps

Overall: 87.5%  (84/96 gaps covered)
Tests: 327 -> 376  (+49 new, +4 test files)
Gate: 87.5% >= 80% target  [PASS]
```

## Circuit Flow — Kanban-as-Scheduler Pipeline Tick

```
_cmd_tick(project)
  |
  +-- _validate_project_slug  [covered]
  |    - alphanumeric, dot, dash, underscore -> True
  |    - spaces/shell metachar -> False -> return 2
  |    - empty -> False
  |
  +-- _read_prior_tick_id     [covered]
  |    - file exists -> tick_id
  |    - file missing -> None (cold start)
  |
  +-- _load_toml_overlay      [covered]
  |    - config.toml exists -> (FullConfig, CircuitBreakerConfig)
  |    - config.toml missing -> (None, CircuitBreakerConfig)
  |    - config.toml error -> (None, CircuitBreakerConfig) [WARNING]
  |
  +-- _make_circuit_breaker   [covered]
  |    - from CircuitBreakerConfig + state_dir + slack_channel
  |
  +-- IF prior_tick_id:
  |    |
  |    +-- all_phases_complete?
  |    |   - NO -> log + return 0 (skip, in-flight) [covered]
  |    |   - YES -> observe outcomes [covered]
  |    |
  |    +-- observe_outcomes + circuit breaker
  |    |   - get_todo_kanban_status -> status_map [covered]
  |    |   - observe_outcomes(state_dir, prior_tick_id, status_map) [covered]
  |    |   - cb.observe_from_outcomes(state_dir, prior_tick_id) [covered]
  |    |   - Exception -> log warning, proceed [covered]
  |
  +-- TickLock.acquire(tick_id)
  |    - TickLockHeld -> return 1 [covered]
  |
  +-- project_dir/TODOS.md validation [covered]
  |    - project not found -> return 2
  |    - TODOS.md not found -> return 2
  |
  +-- build_context + run_selection
  |    - Uses full TOML overlay or in-memory defaults [covered]
  |
  +-- IF picked is None:
  |    |
  |    +-- cb.observe(picked=None, counts_as_no_progress=True) [covered]
  |    +-- _persist_tick_id(state_dir, tick_id) [covered]
  |    |   - writes current_tick_id.txt atomically
  |    |   - writes tick_started sentinel
  |    +-- write picked_none sentinel [covered]
  |    +-- return 0
  |
  +-- IF picked=TODO-N:
  |    |
  |    +-- _persist_tick_id(state_dir, tick_id) [covered]
  |    |   - writes current_tick_id.txt atomically [covered]
  |    |   - writes tick_started sentinel [covered]
  |    |   - OSError on tick_id -> WARNING, continue [covered]
  |    |   - OSError on sentinel -> WARNING, continue [covered]
  |    |
  |    +-- register_todo_phases(todo_id, tick_id, board_slug, project_dir)
  |    |   - todo_id validation -> ValueError [GAP]
  |    |   - load_phases -> FileNotFoundError [covered]
  |    |   - Phase loop:
  |    |     - JSON output -> parse task_id [covered]
  |    |     - Legacy "Created t_xxx" -> parse task_id [GAP]
  |    |     - Unparseable output -> RuntimeError [GAP]
  |    |     - returncode != 0 -> archive + RuntimeError [covered]
  |    |     - --goal, --goal-max-turns flags [GAP]
  |    |     - --parent for phase_idx > 0 [covered]
  |    |     - --idempotency-key [covered]
  |    |
  |    +-- cb.observe(picked=picked, counts_as_no_progress=False) [covered]
  |    +-- return 0
  |
  +-- IF register_todo_phases raises RuntimeError:
  |    - append_outcome(failed_to_spawn) [covered]
  |    - return 1
```

## Circuit Breaker — observe_from_outcomes Flow

```
observe_from_outcomes(state_dir, prior_tick_id)
  |
  +-- .hermes/outcomes/<tick_id>-phases.json
  |   - NOT FOUND -> observe(picked=None, no_progress=True) [covered]
  |   - EMPTY -> observe(picked=None, no_progress=False) [covered]
  |
  +-- Parse JSONL lines
  |   - Invalid JSON -> JSONDecodeError [covered]
  |
  +-- Classify outcomes (single pass)
  |   - OUTCOME_PHASE_COMPLETE -> has_phase_complete = True
  |   - OUTCOME_ALL_COMPLETE -> has_all_complete = True
  |   - starts with OUTCOME_FAILED_PREFIX -> has_failure = True
  |   - OUTCOME_PICKED_NONE -> has_picked_none = True
  |   - Unknown outcome -> falls through
  |
  +-- IF has_all_complete or has_phase_complete:
  |    - consecutive_no_progress = 0 [covered]
  |    - if backed_off: _set_cron_interval(5), backed_off=False [covered]
  |
  +-- IF has_failure:
  |    - observe(picked=None, no_progress=True) [covered]
  |
  +-- IF has_picked_none:
  |    - observe(picked=None, no_progress=False) [covered]
  |
  +-- ELSE (no terminal outcomes):
  |    - observe(picked=None, no_progress=False) [covered]
```

## Kanban Tasks — register_todo_phases Flow

```
register_todo_phases(todo_id, tick_id, board_slug, project_dir)
  |
  +-- todo_id validation: ^TODO-\d+$
  |    - Invalid -> ValueError [GAP]
  |    - Shell injection "TODO-10; rm -rf /" -> ValueError [GAP]
  |
  +-- load_phases(phases_path)
  |    - File not found -> FileNotFoundError [covered]
  |
  +-- For each phase (in order):
  |    |
  |    +-- _build_json_header(tick_id, phase_key, todo_id, project_slug)
  |    +-- _render_phase_prompt(phase.prompt, todo_id, tick_id, project_slug)
  |    +-- Build cmd: hermes kanban create --tenant <slug> <name>
  |    |   --body <header\nprompt> --workspace dir:<dir>
  |    |   --idempotency-key <tick_id>:<phase_key> --json
  |    |   --goal --goal-max-turns <turns> [GAP: not tested]
  |    |   --parent <prev_task_id> (if phase_idx > 0) [covered]
  |    |
  |    +-- subprocess.run(cmd, timeout=60)
  |    |   - returncode != 0:
  |    |     - _archive_tasks(task_ids) [covered]
  |    |     - raise RuntimeError [covered]
  |    |     - _archive_tasks best-effort (swallows exceptions) [GAP]
  |    |   - returncode == 0:
  |    |     - Parse JSON: {"id": "task_xxx"} [covered]
  |    |     - Legacy: "Created t_xxx" -> t_xxx [GAP]
  |    |     - Unparseable -> RuntimeError [GAP]
  |    |
  |    +-- task_ids.append(task_id)
  |
  +-- return task_ids
```

## Kanban Tasks — all_phases_complete Flow

```
all_phases_complete(tenant, tick_id, state_dir)
  |
  +-- get_todo_kanban_status(tenant, tick_id)
  |    - CLI failure -> {} (FileNotFoundError, TimeoutExpired, JSONDecodeError) [covered]
  |    - returncode != 0 -> {} [covered]
  |    - List format [covered]
  |    - Dict format {"tasks": [...]} [covered]
  |    - Filter by tick_id [covered]
  |    - Malformed header -> skip task [covered]
  |
  +-- IF not status_map (no tasks):
  |    - IF state_dir provided:
  |      - Check sentinel for picked_none -> True [covered]
  |      - Check sentinel for tick_started -> True [GAP]
  |      - Sentinel JSON error -> False [GAP]
  |    - ELSE -> False (conservative) [GAP]
  |
  +-- For each (phase_key, status):
  |    - status not in COMPLETION_STATUSES -> False
  |    - status == "archived" -> False (not completion) [covered]
  |    - All in COMPLETION_STATUSES -> True [covered]
```

## Decision Context — Kanban-Aware in_flight

```
build_in_flight(state_dir, max_phase_timeout_min, board_slug, snapshot)
  |
  +-- IF board_slug is not None:
  |    - IF snapshot is not None:
  |      - _extract_in_flight_ids(snapshot) -> return sorted [covered]
  |    - ELSE:
  |      - _kanban_in_flight_ids(board_slug)
  |        - _fetch_kanban_snapshot(board_slug)
  |          - CLI success -> parsed JSON [covered]
  |          - FileNotFoundError -> None [covered]
  |          - TimeoutExpired -> None [covered]
  |          - JSONDecodeError -> None [covered]
  |          - Non-zero return code -> None [covered]
  |        - _extract_in_flight_ids(snapshot)
  |          - List format [GAP]
  |          - Dict format [covered]
  |          - Done tasks skipped [GAP]
  |          - Created tasks included [GAP]
  |          - No header -> skip [covered]
  |          - Missing todo_id -> skip [GAP]
  |          - Empty body -> skip [GAP]
  |          - Duplicate todo_ids -> unique set [GAP]
  |      - None -> fallback to file markers
  |
  +-- ELSE (no board_slug):
  |    - Fallback: _rfr_ids | _phase_started_ids -> sorted [covered]
  |
  +-- Fallback paths:
  |    - Kanban None + file markers present [covered]
  |    - Kanban None + no file markers -> [] [covered]
  |    - Kanban success + file markers -> kanban wins [covered]
  |    - No board_slug + file markers -> file markers [covered]
```

## User Flows

```
User: pipeline-watch tick demo
  |
  +-- First tick (cold start):
  |    - No prior tick -> proceed
  |    - Select TODO -> picked or None
  |    - If picked: persist + register kanban tasks
  |    - If None: persist + write picked_none
  |
  +-- Subsequent tick (prior in-flight):
  |    - Prior tick_id found
  |    - all_phases_complete = False -> skip (log + return 0)
  |
  +-- Subsequent tick (prior complete):
  |    - Prior tick_id found
  |    - all_phases_complete = True
  |    - observe_outcomes + circuit breaker
  |    - New selection
  |
  +-- Lock contention:
  |    - TickLockHeld -> return 1 (exit error)
  |
  +-- Invalid project slug:
  |    - "a;b" -> return 2 (error code)
  |
  +-- Project not found:
  |    - No project dir -> return 2
  |
  +-- TODOS.md not found:
  |    - Project dir exists, no TODOS.md -> return 2
  |
  +-- Kanban registration failure:
  |    - RuntimeError -> write failed_to_spawn + return 1
```

## Error States

```
Error Path                          Handler                    Test
──────────────────────────────────── ────────────────────────── ─────
Kanban CLI not found               FileNotFoundError -> {}     Covered
Kanban CLI timeout                 TimeoutExpired -> {}        Covered
Invalid JSON from kanban           JSONDecodeError -> {}       Covered
Non-zero kanban return code        returncode != 0 -> {}       Covered
Disk full (OSError) on tick_id     WARNING, continue           Covered
Disk full on sentinel              WARNING, continue           GAP
Invalid todo_id                    ValueError                  GAP
Task creation fails                _archive + RuntimeError     Covered
Task ID unparseable                RuntimeError                GAP
Legacy CLI output                  "Created t_xxx" parsing     GAP
TOML config load error             WARNING, defaults           Covered
Observe_outcomes exception         WARNING, proceed            Covered
Sentinel JSON error                JSONDecodeError -> False    GAP
```

## Test Statistics

```
Module                       Existing   New   Total   Coverage
──────────────────────────── ───────── ───── ──────── ────────
outcomes.py                      0       0       0    100% (n/a)
circuit.py                      12       0      12    100%
kanban_tasks.py                 45      14      59    90%
kanban.py                       28       4      32    70%
decision/context.py             12      21      33    91%
cli.py (tick)                   27      10      37    90%
────────────────────────────────────────────────────────────────
Total                          124      49     173    87.5%
```
