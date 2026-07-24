# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.8] - 2026-07-23

### Added

- `pipeline-watch test` now logs each phase transition (running/done/failed) and an
  initial phase status table to the console via `log.info()`, instead of writing
  silently to `events.jsonl` only. (TODO-30)
- Raised the `--timeout` default for `pipeline-watch test` from 3600s (1h) to 86400s
  (24h) so healthy long test runs are no longer killed by the default. The flag still
  works as an explicit override. (TODO-30)

## [0.5.7] - 2026-07-23

### Added

- `UI Review` decision field for `todos-manager` TODO entries, mirroring the existing `Security Review` field — `required`/`not-required`, auto-derived from title/summary keywords (ui, frontend, design, visual, layout, component, css, style, dashboard, artifact, page, screen, modal, form, navigation, button, icon, animation) during `--add`, surfaced in the synthesis block, validated as a required `Decisions` sub-key, and gap-checked by `--revise`.

## [0.5.6] - 2026-07-22

### Removed

- `hermes_pipeline/approve_plan.py` — dead plan-gate subsystem module (CLI `approve-plan` subcommand removed in v0.5.5; no remaining call sites).
- `hermes_pipeline/runner.py` — dead null-kanban-scheduler subsystem module; consolidation into single-kanban-only design removes its `PipelineRunner` orchestration role.
- `hermes_pipeline/watcher.py` — dead watcher entrypoint (replaced by `__main__.py` event loop in v0.5.1).
- Plan-gate branches in `hermes_pipeline/gates.py` and `hermes_pipeline/gate_state.py` — `PLAN_GATE_PHASE_KEY`, plan-gate phase marker logic.
- Null-kanban-scheduler branches in `hermes_pipeline/harness.py`, `hermes_pipeline/phases.py` — `PipelineRunner` dispatch, `run()` function, `_invoke_hermes()` / `_invoke_review_phase()` null-mode handlers, marker-based state fallback, gate-dispatch harness.
- CLI subcommands: `merge`, `status`, `kill` (null-scheduler dead code; Hermes kanban-only consolidation in v0.5.2 and later supersedes these).
- `ReadyForReview` and its `State` methods (`write_ready_for_review`, `write_ready_for_review_min`, `read_ready_for_review`, `set_merge_status`, `list_ready_for_review_pending`) in `hermes_pipeline/state.py` — orphaned once `phases.py`/`runner.py` (their only writers) were deleted.
- `hermes_pipeline/gates.py` decision-sheet and rejection-sidecar I/O (`write_decision_sheet`, `read_decision_sheet`, `write_rejection_sidecar`, `read_rejection_sidecar`, `_sanitize_override`, `_HIGH_RISK_KEYWORDS`) — dead once the plan-gate branch was removed; only `REJECTION_SUFFIX` remains (still read by `decision/context.py`'s rejection-count reader).
- Test modules and fixtures tied to deleted plan-gate and null-scheduler subsystems.

### Changed

- `hermes_pipeline/decision/context.py` `build_in_flight()` — removed file-marker fallback during kanban service outages. Now returns empty list if kanban lookup fails (strict single-kanban design, no degraded fallback). Added explicit test coverage for the outage path.

## [0.5.5] - 2026-07-21

### Added

- Optional `**Spec:**` and `**Reference:**` fields on TODOS.md entries. `**Spec:**` names a single authoritative deliverable doc; `**Reference:**` is a comma-delimited list of supplementary background paths. When present, the pipeline's first phase (`_invoke_hermes`) validates each path (containment under `project_dir`, existence) and injects the surviving paths into the phase prompt via `_render_phase_prompt`. Both fields are `--revise`-only in the `todos-manager` skill — never AI-pre-filled or auto-suggested — and fail soft: any parse error, missing file, or traversal attempt silently drops that item rather than raising.
- `hermes_pipeline/todos_md.py`: new standalone `find_todo_fields()` parser that extracts `Spec:`/`Reference:` values for a given TODO entry, anchored between that entry's header and the next, so it can't bleed into a neighboring entry.

## [0.5.4] - 2026-07-20

### Changed

- Extracted plan-gate status logic (`GateStatus` enum, `check_gate_status()` → `gate_status()`) out of `hermes_pipeline/gates.py` into a new read-only `hermes_pipeline/gate_state.py` module. `kanban_tasks.py`'s inline rejection-sidecar check now routes through this shared module instead of duplicating the logic.

## [0.5.3] - 2026-07-20

### Added

- Pluggable pipeline phase profiles: `hermes-pipeline init --profile <name>` lets a project choose which skill-set drives its phases (`gstack`, the default, or the new `agent-skills` profile). Each profile ships its own bundled `phases.yaml` and computes its own required capabilities.
- `agent-skills` profile: a 9-phase pipeline that maps to the `agent-skills:*` skill family instead of gstack's own skills.
- Pipeline contracts now record a `profile` field (schema bumped to v2), validated against a lowercase alphanumeric/hyphen naming rule so a malformed or path-unsafe profile name is rejected before any file resolution happens.
- `hermes-pipeline doctor` is profile-aware: it loads phases from the contract's declared profile and reports drift/missing/invalid profile errors by name.
- Docs: `docs/howto-agent-skills-profile.md` walks through setting up the agent-skills profile; `docs/howto-pipeline-contract.md` documents the new `profile` field and schema v2.

### Changed

- Phase execution (`tick`, `doctor`, `init`) now resolves phases from the project's contract-selected profile instead of a single hardcoded `phases.yaml`, falling back to `gstack` only when no contract exists yet.
- Existing (schema v1) contracts without a `profile` field are rejected with a clear version-mismatch error — re-run `init` to upgrade.

## [0.5.2] - 2026-07-19

### Added

- Regression tests confirming that harness kanban-phase registration correctly resolves task assignee from the pipeline contract, and falls back to `"default"` with a warning when the contract can't be loaded.

### Fixed

- Test coverage for the harness kanban-scheduler checklist is now fully wired to production functions — the remaining checklist rows are linked to real tests, closing out TODO-24.

## [0.5.1] - 2026-07-19

### Changed

- Gate-task auto-completion now routes through `kanban_tasks.complete_todo_kanban_task` instead of a harness-local subprocess call, keeping the production completion path in one place.

### Fixed

- A failed gate-task completion no longer gets logged as a success — the harness now only reports "auto-completed" when the completion actually succeeded.
- A gate that fails to auto-complete now logs a warning naming the task and phase, so a stuck gate can be traced back to its failed completion attempt instead of failing silently.

## [0.5.0] - 2026-07-16

### Added
- **`--kanban {null,hermes}` flag** — Opt-in real kanban adapter for the mock integration test harness, wired to a dedicated tenant with tick_id-labeled card bodies (TODO-20). Default (`null`) behavior is unchanged.
- **Preflight validation** — `hermes kanban list --tenant` check with actionable error if the kanban board is unreachable.
- **Kanban-as-scheduler polling** — `run_harness` now drives real pipeline phases end-to-end through `_poll_kanban_phases`, reusing `register_todo_phases`, `get_todo_kanban_status`, and `all_phases_complete` from the production kanban module instead of a harness-only phase loop.
- **Contract-resolved kanban assignee** — Phase registration reads `assignee` from `.hermes/pipeline.toml` via the same `load_contract()` path as `pipeline-watch tick`, falling back to `"default"` if the contract is missing or malformed.
- **Gate task auto-completion** — `_auto_complete_gate_tasks` automatically completes downstream gate tasks once their parent phase finishes, including the ready/`None` → done transition (fast phases that complete between polls without ever being observed as `running`).

### Fixed
- **Invalid `KanbanOutcome` literal** — `"failed"` corrected to `"abandoned"` across all call sites.
- **Silent kanban-cleanup gaps** — Added cleanup on `continue_on_failure=False` phase failure and convergence-halt paths.
- **Kanban phase-completion gap** — Phases that complete between polls without passing through `running` (ready/`None` → done) no longer leave downstream gate tasks blocked.

## [0.4.11] - 2026-07-15

### Added
- **Mock integration test harness** — Repeatable, verifiable end-to-end pipeline testing. Creates mock projects with preset TODOs, runs the full pipeline through isolated temp directories, monitors phase transitions, generates JSONL event logs and structured findings reports. Supports iterative fix cycles with `--loop` to diff reports across runs.
- **`hermes-pipeline` CLI entrypoint** — Registered alias for the Hermes Pipeline CLI, accessible from any terminal.
- **`hermes-pipeline test` subcommand** — Drives the mock harness via `--fixture`, `--loop`, `--phase`, `--keep`, `--timeout`, and `--convergence-threshold` flags.
- **Convergence detector** — Automatic halt when N+ consecutive phase failures share the same error class, preventing infinite retry loops.
- **`continue_on_failure` mode** — PipelineRunner continues through non-critical phase failures and auto-approves gate phases, surfolding structural correctness of the full pipeline.
- **PipelineRunner monitor callbacks** — Real-time hooks for `phase_started`, `phase_completed`, and `phase_failed` transitions.
- **Environment threading for subprocess isolation** — Phase subprocesses inherit only test-scoped environment variables, preventing the harness from reading user-level config or credentials.

### Fixed
- **Harness phase failure reporting** — Timeout and convergence-halt events are recorded in the JSONL event log so reports reflect the actual failure mode instead of silent truncation.
- **Version test resilience** — Version parsing no longer fails when the VERSION file contains unexpected trailing content.

### Changed
- **Test report module** — New `test_report.py` provides `generate_report`, `summarize_report`, `diff_reports`, and `summarize_diff` for structured pipeline analysis.

## [0.4.10] - 2026-07-14

### Added
- **`todos-manager --revise` subcommand** — revise an existing TODO entry by filling missing or weak fields with AI-pre-filled suggestions. Selects an entry by TODO-ID, scans for gaps (What, Why, Decisions, optional fields), auto-researches the codebase scoped to gaps, presents a synthesis block with confidence tags, and writes the updated entry back to TODOS.md. Reuses the auto-research phase from `--add`. Only revises active entries — archived entries are never modified.
- **Entry boundary parsing spec** — shared algorithm for identifying TODO entry start/end positions in TODOS.md. Used by both `--archive` and `--revise` to extract entries without DRY violations.

### Changed
- **`todos-manager --add` subcommand revised** — after you provide a title and summary, auto-researches the codebase to pre-fill TODO fields (What, Why, Decisions) before the interactive prompts. Reduces manual typing for entries that correspond to existing code areas.
- **TODOS Manager skill updated to seven subcommands** — `--revise` is now documented alongside `--init`, `--add`, `--convert`, `--audit`, `--archive`, and `--list`.

## [0.4.8] - 2026-07-13

### Added
- **`todos-manager --list` subcommand** — report-only listing of active TODO entries as a markdown table (ID, status, title, summary). Pass `--all` to also show archived entries from `TODOS-archive.md` in a separate table. Modifies no files.
- **`todos-manager --convert` header-based transformation (Mode B)** — converts header-based TODOS.md entries (freeform text, title-as-header, no schema fields) into the canonical enforced format. Creates dated backup files and a reference document. Idempotent — already-converted files are skipped.

### Fixed
- **Decision agent JSON parser crashes on CLI backend warnings** — `_parse()` no longer requires the response to start with a code fence. CLI backends that prepend stderr-style warning lines before the fenced JSON block now parse correctly. The parser also tolerates one-line fenced JSON, missing closing fences, and trailing prose after fenced blocks.

### Changed
- **TODOS Manager skill updated to six subcommands** — `--list` is now documented alongside `--init`, `--add`, `--convert`, `--audit`, and `--archive`. Updated ARCHITECTURE.md, CLAUDE.md, README.md, and how-to guide to match.

## [0.4.7] - 2026-07-13

### Added
- **Skill test environment (Phase 1)** — `tests/skill-test-environment/` provides a structural unit test suite for the `todos-manager` skill: a demo-project TODOS.md/TODOS-archive.md fixture, golden YAML assertion files for each subcommand (`--add`, `--init`, `--audit`, `--archive`), and pure-Python verification modules (`skill_logic.py`, `verify.py`) covering ID sequencing, entry parsing, format validation, and archive logic. Runs in under 5 seconds with zero token cost: `uv run pytest tests/skill-test-environment/unit/ -v`. Phase 2 (agent-driven, AI-judged semantic validation) is deferred.

## [0.3.2] - 2026-06-19

### Added
- **`--verbose` / `--debug` logging flags** — `--verbose` increases log detail (selection results, lock state, tick_id). `--debug` enables full debug logging (agent call summaries, circuit breaker transitions, kanban registration)
- **`recover-counter` subcommand** — scans `TODOS.md` for the highest `TODO-N` ID and initializes `.hermes/todo_id_counter`; prevents ID collisions when bootstrapping a project with hand-written TODOs

### Fixed
- **`--debug` flag now enables `pipeline.verbose` logger** — verbose log lines are now visible in debug mode (they were previously only shown with `--verbose`)

## [0.3.3] - 2026-06-23

### Added
- **Multi-project scan loop** — `pipeline-watch tick` without a project argument now scans all active projects in `projects_dir`, running one selection per project under a single global lock. `pipeline-watch kill` without a project argument similarly scans all projects
- **Per-project configuration** — `<project>/.hermes/project.toml` for filtering (`enabled = false` to archive) and per-project Slack channel via `[notifications] slack_channel`
- **Project discovery** — new `project_config` module with `_discover_projects()`, `_is_enabled()`, and `_resolve_slack_channel()` for filesystem-based project filtering

### Changed
- **Selection model default** — `SelectionConfig.model` defaults to `"auto"` instead of `"claude-opus-4-7"`. Hermes resolves `"auto"` to the current best model, so the pipeline stays current without reconfiguring.
- **`tick` subcommand** — optional `project` argument; when omitted, scans all active projects instead of requiring a specific project
- **`kill` subcommand** — optional `project` argument; when omitted, scans all projects for in-flight phases
- **State migration** — first-run migration of global state (`~/.hermes/`) to per-project state (`<project>/.hermes/`) via new `state_migration` module

### Fixed
- **Kill across projects** — `kill --todo` now searches all project state directories for the specified TODO, returns exit code 2 if not found anywhere
- **Slug validation** — `_validate_project_slug` rejects single-character slugs and invalid directory names during project discovery, preventing misconfigured projects from entering the scan loop

### Removed
- **Circuit breaker cron backoff** — The circuit breaker no longer adjusts the Hermes cron interval (backoff/resume). `backoff_interval_min` and `backed_off` are removed from config and circuit state. The gateway service owns tick scheduling.

## [0.3.1] - 2026-06-16

### Added
- **`pipeline-watch tick` subcommand** — kanban-as-scheduler pipeline tick: selects a TODO via Hermes agent, registers phases as kanban tasks with `--parent` dependency chain, and observes circuit breaker
- **Kanban task registration** — `register_todo_phases` creates phases as kanban tasks with `--idempotency-key` for dedup and `--parent` for sequential execution
- **Circuit breaker outcome observation** — `observe_outcomes` writes phase completion/failure outcomes to JSONL sidecars; `observe_from_outcomes` reads outcomes to drive circuit breaker state
- **Stale-marker PID verification** — `_phase_started_ids` checks process liveness before sweeping stale markers; wedged-but-alive processes remain visible
- **Kanban-aware in-flight detection** — `build_in_flight` queries kanban for in-flight tasks, falls back to file markers
- **`.hermes/prompts/` tracking** — prompt templates are tracked in git; runtime state (decisions, outcomes, locks) is ignored

### Changed
- **Tutorial updated** — `pipeline-watch tick` is the primary workflow for development and debugging; Hermes cron is optional for production

### Fixed
- **Circuit breaker config loaded once per tick** — eliminated duplicate TOML overlay reads and `CircuitBreaker` instantiations
- **Project slug validation** — rejects path traversal (`..`, `.`) and CLI flag injection (`--help`, `-v`) in project slugs
- **Partial registration detection** — expected phase keys are now persisted after kanban task registration; `all_phases_complete` verifies all expected phases are present before returning true
- **Tick stall detection** — `tick_started` sentinel without terminal outcomes is now treated as a stall (not completion) so the circuit breaker can detect no-progress conditions
- **Hermes kanban CLI adaptation** — adapted to CLI drift: `--board` → `--tenant`, positional title
- **Atomic tick_id persistence** — tick_id is persisted atomically and before kanban registration, preventing split-brain on crash

## [0.1.0] - 2026-06-11

### Added
- Initial release: `pipeline-watch` CLI with auto-tick, merge, and status commands
- Auto-tick discovery: scans projects for TODOS.md changes and selects eligible TODOs
- Phase 9 merge orchestration: confirm, version bump, and git merge to main
- Cron registration: `install-cron.sh` helper for 5-minute automated ticks
- CLI subcommands: `auto`, `merge`, `status`
- Pending records table showing ready-for-review records with status and age
- Configuration via environment variables: `PIPELINE_LOCK_DIR`, `PIPELINE_PROJECTS_DIR`, etc.

### Fixed
- Improved error messages for invalid arguments (e.g., non-numeric `todo_id`)

### Changed
- Updated Python version requirement from >=3.14 to >=3.9 for broader compatibility

## [0.2.0] - 2026-06-14

### Added
- **Hermes decision engine** — LLM-driven selection replaces deterministic selection (`decision/` module with context builder, agent, schema, store)
- **SHA-pinned prompts** — prompts stored with SHA-256 checksums; mismatch alerts at selection time
- **Injection fences** — fence-tag injection neutralized in untrusted regions of the decision pipeline
- **Immutable decisions + sidecar outcomes** — write-once via `os.link`, per-writer UUID temp files, rotation of stale records
- **Circuit breaker** — no-progress counter, cron backoff, Slack alert deduplication
- **Atomic tick lock** — atomic-mkdir `tick.lock` with stale sweep; prevents duplicate ticks
- **Phase markers** — `phase_started` marker write/delete around invocation; exclusive markers prevent double-run
- **Kill subcommand** — `pipeline-watch kill --todo TODO-N` or `--all` for in-flight phases; confirms process exit, releases tick lock when owned
- **Outcome sidecar** — terminal `merge_status` transitions write outcome metadata (failed, killed_by_operator)
- **Eval suite** — 8 selection-prompt fixtures with runner (`tests/eval/`); non-blocking eval workflow
- **Operator how-to guides** — Diataxis-formatted guides for config, eval, kill, and prompt-sha-mismatch troubleshooting
- **Hermes state machine** — docs for phase lifecycle (state machine table)

### Changed
- **Directory structure flattened** — `hermes-pipeline/src/hermes_pipeline/` → `hermes_pipeline/`; `hermes-pipeline/tests/` → `tests/`; `hermes-pipeline/configs/` → `configs/`
- **Raised minimum Python version** from >=3.9 to >=3.12
- **Decision-driven pipeline** — watcher and CLI pruned to delegate selection and scheduling to Hermes

### Fixed
- **Hallucinated picks rejected** — LLM must pick a TODO that exists in TODOS.md; rejects hallucinated IDs
- **Atomic state writes** — `set_merge_status` and `ready_for_review` use tmp+rename to prevent partial writes
- **Best-effort outcome sidecar** — `set_merge_status` doesn't fail if sidecar write fails
- **Config path resolution** — phases resolve config relative to flattened directory structure
- **Kill targets `child_pid`** — kill subcommand targets the phase child process, not the watcher
- **Canonical RFR filename** — decision context normalizes filename and checks PID liveness on sweep

### Removed
- **Deterministic `selection.py`** — replaced by Hermes LLM-driven decision engine
- **`pipeline-watch auto` subcommand** — scheduling moved to Hermes cron (`hermes cron set pipeline-tick */5 * * * *`)
- **System crontab registration** — `install-cron.sh` removed; tick schedule managed via `hermes cron set`
- **Redundant `hermes-pipeline/README.md`** — documentation consolidated in root docs

## [0.3.0] - 2026-06-15

### Added
- **Hermes adapter** — `hermes_pipeline/hermes_adapter.py` with `hermes_call()` (simple one-shot queries) and `hermes_agent_call()` (agent-style subprocess with PID tracking). All LLM traffic now routes through `hermes chat -q` instead of direct Anthropic SDK calls.
- **HermesCallError and HermesAgentResult** — structured error and result types for Hermes CLI failures and agent outcomes.
- **.env file support** — `.env` files are now git-ignored (`.env.example` is allowed).
- **CI action pinning** — GitHub Actions pinned to SHA hashes for supply-chain security.

### Changed
- **Decision agent** — `_anthropic_call()` replaced with `_hermes_call()`; no longer imports the `anthropic` package. Timeout is computed from `max_tokens` (1s per 100 tokens, min 30s, max 300s).
- **Phase execution** — `_run_claude_subprocess()` replaced with `hermes_agent_call()`. Tool and turn constraints are now encoded as prompt headers since `hermes chat -q` lacks `--tools`/`--turns` flags.
- **Requirements** — Anthropic API key is no longer needed for runtime selection (eval suite still checks for it as a skip gate). Hermes CLI must be installed and authenticated (`hermes login`) instead.

### Removed
- **Anthropic SDK dependency** — `anthropic>=0.40` removed from `pyproject.toml`. The orchestrator no longer calls the Anthropic API directly.

### Fixed
- **Process group kill on timeout** — `hermes_agent_call()` kills the entire process group (hermes + children) on timeout instead of only the parent process, preventing orphaned subprocesses.
- **stderr capture on agent timeout** — after SIGKILL, agent timeout path now captures stderr for diagnostics.
- **Transient spawn retry** — `hermes_call()` and `hermes_agent_call()` retry up to 2 times on transient OSError before failing, improving resilience against brief network hiccups.
- **KeyboardInterrupt propagation** — `hermes_agent_call()` propagates KeyboardInterrupt instead of silently swallowing it during timeout handling.
- **Tool enforcement via CLI flags** — tool and turn constraints are enforced via `hermes chat -q` CLI flags (`-t` for tools, `--max-turns` for turns) instead of purely advisory prompt headers.
- **Preflight hermes check** — `check_hermes()` validates the hermes CLI availability before pipeline operations, failing fast with a clear error.
- **Renamed claude functions** — `_anthropic_call()` and `_run_claude_subprocess()` renamed to `_hermes_call()` and `_run_hermes_subprocess()` to reflect the new dependency.

## [0.3.4] - 2026-06-29

### Added
- **Ship gate (Phase 9)** — New `phase_9_ship` blocked kanban task that holds every completed TODO in-flight until a human approves via `pipeline-watch approve`. The blocked gate replaces the `terminal: true` flag on Phase 8, keeping the pipeline loop running until approval.
- **`pipeline-watch approve` subcommand** — Deterministically ships an approved TODO: bumps VERSION/pyproject.toml/CHANGELOG on the work branch, gates on CI-green, and squash-merges to main with `--match-head-commit`. Idempotent — re-running on an already-merged PR just completes the gate.
- **SHA-staleness guard** — Refuses to merge if the PR head SHA has changed since review. `--force --force` (double pass) bypasses this guard and writes an audit log entry.
- **Dirty-tree and CI-green guards** — Refuses approve if the working tree is dirty or CI is not green. Force flag never bypasses these guards.
- **"Ready to ship" Slack alert** — Fired exactly once when all phases complete except the blocked gate. Deduped by the existence of the ship sidecar file.
- **`ShipSidecar` dataclass + atomic sidecar I/O** — Writes `outcomes/<tick_id>-ship.json` with PR details, head SHA, and branch names. Read by `approve` to verify SHA and complete the merge.
- **`approve_lock` via fcntl** — Serializes concurrent approve calls so two operators can't race the same merge.
- **`get_todo_kanban_tasks`** — Queries kanban for all tasks of a tick, returning `KanbanTaskInfo` with task IDs and statuses. Used by `approve` to resolve and complete the gate task.
- **`bump_in_pr`** — Writes VERSION, pyproject.toml, and CHANGELOG on the work branch, commits, and pushes. Restores the original branch after completion (even on failure).

### Changed
- **`Phase` dataclass** — `gate` flag added; `prompt`, `tools`, `turns` now optional with defaults so gate phases need no LLM fields.
- **`configs/phases.yaml`** — `phase_9_ship` added as a `gate: true` phase; `terminal: true` moved from Phase 8 to Phase 9.
- **`register_todo_phases`** — Gate phases are created with `--initial-status blocked` and no `--goal` flags (pure markers, never dispatched to an agent).
- **`_tick_project`** — Calls `maybe_ship_ready` before the `all_phases_complete` early-return, so the "ready to ship" alert fires even though the blocked gate keeps `all_phases_complete` returning False.

### Fixed
- **Branch left on `work_branch` after bump failure** — `bump_in_pr` wraps `git checkout work_branch` in try/finally that restores the original branch, so a CI-red refusal or merge failure doesn't leave the operator on the wrong branch.

## [0.4.0] - 2026-07-07

### Added
- **Code review phase (Phase 5)** — New `phase_5_review` phase runs gstack `/review` skill autonomously via `hermes chat -q` between development and CSO. Pre-review snapshot captures HEAD and diff; post-review runs pytest and either commits fixes (`review_clean`) or restores the worktree (`review_reverted_test_failure`, `review_timeout`, `review_skipped_no_diff`). Machine-verified outcomes enable deterministic pipeline progression.
- **`hermes_pipeline/review_phase.py`** — New module owning the code-owned review lifecycle: `capture_pre_review_state()`, `_run_hermes_subprocess()`, `run_pytest()`, `restore_worktree()`, `finalize_review()`, `write_review_artifacts()`, `commit_all()`.
- **Phase 5 config entry** — Added `phase_5_review` to `configs/phases.yaml` with `tools: "Read,Edit,Bash"`, `turns: 30`, `timeout: 2400`, positioned between `phase_4_development` and `phase_6_1_cso`.
- **Dry-run documentation** — `docs/pipeline/phase_5_review_dry_run_note.md` documents the required manual validation before enabling unattended runs.
- **Comprehensive tests** — New test modules `tests/test_phases.py` (config validation), `tests/test_phases_invoke.py` (routing tests), and `tests/test_review_phase.py` (unit tests with real git repo fixtures for capture/restore/finalize logic).

### Changed
- **`hermes_pipeline/phases.py`** — Added `_invoke_review_phase()` and routing in `_invoke_hermes()` to dispatch `phase_5_review` through the code-owned lifecycle instead of the generic rc-check path.
- **Phase order** — `configs/phases.yaml` now has 9 phases with `phase_5_review` inserted between development and CSO.

### Fixed
- **Path traversal in artifact filenames** — `todo_id` is now validated against a strict pattern before use in file paths, preventing directory escape.
- **Secret leakage in review artifacts** — Hermes stdout embedded in committed findings is now redacted of API keys, tokens, and other sensitive patterns.
- **Race condition in `restore_worktree`** — Documented the isolated-worktree assumption; sequential `reset --hard` + `clean -fd` is safe under that constraint.
- **Missing git author config** — `commit_all()` now sets explicit `user.name`/`user.email` via `-c` flags, preventing failures when no global git config exists.

### Added (docs)
- **Architecture overview** — `docs/ARCHITECTURE.md` documents lane structure, phase execution flow, and data flow across the pipeline.

## [0.4.4] - 2026-07-10

### Added
- **`pipeline-watch install-profile`** — Installs the bundled pipeline Hermes profile for unattended kanban execution. Use `--force` to reinstall after SOUL.md changes. See 0.4.6 below for a follow-up fix to how the profile is created.
- **`--assignee` flag on `init`** — Set the Hermes profile assignee when creating the project contract: `pipeline-watch init <project> --assignee pipeline`.
- **Doctor profile verification** — `doctor` now checks that a non-default assignee profile exists in Hermes. Fails exit code 2 if the profile is missing, with cause/fix guidance.
- **Bundled pipeline profile** — New in-package `data/profiles/pipeline/` with SOUL.md. Ships in the wheel.

### Changed
- **`phases.yaml` moved in-package** — Resolved via `importlib.resources` instead of repo-relative path. Works from installed wheel.
- **Hatchling wheel config** — `pyproject.toml` configured to include `hermes_pipeline` package data in wheel.

## [0.4.6] - 2026-07-11

### Changed
- **`install-profile` clones the active profile instead of installing a bare distribution** — `hermes profile install` only copies files present in the source distribution, so the bundled `distribution.yaml` (SOUL.md only) produced a `pipeline` profile with no `config.yaml`/`.env`/skills, unusable without manual setup. `install-profile` now runs `hermes profile create pipeline --clone` to inherit a working baseline from the currently-active profile, then overlays the bundled pipeline-specific `SOUL.md` on top. `--force` deletes any existing `pipeline` profile first.
- **`install-profile` error handling hardened** — `hermes profile delete`'s exit code is now checked instead of ignored; `hermes profile show` is wrapped in the same "Hermes not on PATH" handling as the other Hermes calls and surfaces its stderr on failure; the parsed profile path is validated as a real directory before `SOUL.md` is copied into it.

### Removed
- **`hermes_pipeline/data/profiles/pipeline/distribution.yaml`** — no longer used now that `install-profile` clones instead of installing a distribution.

## [0.4.3] - 2026-07-10

### Added
- **`pipeline-watch init` subcommand** — Writes the default pipeline execution contract (`.hermes/pipeline.toml`) for a project, declaring assignee and tool capabilities. Idempotent — use `--force` to regenerate after editing `configs/phases.yaml`. Capabilities are computed from phase definitions, not hardcoded.
- **`pipeline-watch doctor` subcommand** — Verifies a project's pipeline execution contract against `configs/phases.yaml`. Exit codes: 0 (clean), 1 (capability drift), 2 (missing/invalid contract).
- **Pipeline execution contract** — Versioned TOML manifest (`.hermes/pipeline.toml`) that declares per-project assignee and tool capabilities. Ticks validate the contract at start: missing contract falls back to computed defaults, stale version or capability mismatch fails the tick with a remediation message.

## [0.4.1] - 2026-07-08

### Added
- **Plan Gate (phase_2b_plan_gate)** — Human review checkpoint between Autoplan and Writing Plan. Autoplan produces a decision sheet (`## Decisions` section) that is parsed into a structured JSON artifact. The gate blocks the pipeline until a human approves or rejects the plan via `pipeline-watch approve-plan`.
- **`pipeline-watch approve-plan` subcommand** — Approve (`--approve`) or reject (`--reject --reason ...`) plan-gate decision sheets. Supports `--override q_id=LABEL` to correct individual recommendations without re-running Autoplan. Override injection protection via sanitization.
- **Risk classifier** — Keyword-based high-risk TODO classification (dependency, architecture, security, data, broad scope). Projects with rejection history are automatically classified as high-risk, triggering the plan gate.

### Changed
- **Phase list** — New `phase_2b_plan_gate` gate phase between `phase_2_autoplan` and `phase_3_writing_plan`. Gate phases are registered as blocked kanban tasks (never dispatched to an agent).
- **Dispatcher** — `maybe_plan_gate_ready` alert fires when plan-gate is blocked but pre-gate phases are complete, notifying via Slack.
- **Runner** — `_invoke_hermes` short-circuits gate phases (approved → skip, blocked/rejected → raise).
- **`all_phases_complete`** — Rejected plan-gate (archived) no longer stalls the tick; rejection sidecar on disk is the authoritative signal.

### Added (internal)
- **Decision sheet schema** — `DecisionSheet` / `DecisionQuestion` / `_Option` frozen dataclasses with full validation (unique question IDs, label matching, answer ∈ options, positive todo_id, schema versioning).
- **Gate status check** — `check_gate_status()` pure read of gate state from kanban + rejection sidecar. Returns `GateStatus` enum (BLOCKED, READY, RUNNING, FAILED, UNKNOWN).

## [0.4.2] - 2026-07-09

### Added
- **TODOS Manager skill v2.1** — Rewrote `skills/todos-manager/SKILL.md` to enforce canonical TODOS.md schema with five subcommands (`--init`, `--add`, `--convert`, `--audit`, `--archive`). Schema requires What/Why/Decisions fields, supports Pros/Cons/Context/Depends on/Assumptions/Completed/Resolved design. Stable TODO-<n> IDs computed by scanning both TODOS.md and TODOS-archive.md. Completed entries archive to `TODOS-archive.md`. Skill source lives at `skills/todos-manager/SKILL.md` (git-tracked); install via `scripts/install-todos-manager.sh` to symlink to `~/.claude/skills/` and `~/.agents/skills/`.

### Changed
- **TODOS.md preamble** — Added format rules blockquote documenting the enforced schema, status markers, required/optional fields, and ID assignment rules.
- **`.claude/` gitignore** — Added `.claude/` to `.gitignore` so agent-client skill installs remain local-only (platform-neutral skill source at `skills/todos-manager/`).

### Removed
- **`.claude/skills/todos-manager/SKILL.md`** — Removed from git tracking; skill now lives at `skills/todos-manager/SKILL.md` (git-tracked) and installs via symlink.

### Planned
- Dashboard UI for pipeline status
- Slack/Discord notifications for merge events
- Migration guides for breaking changes
