# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.9] - 2026-07-14

### Changed
- **`todos-manager --add` subcommand revised** — auto-researches the codebase to pre-fill TODO fields (title, summary, What, Why, Decisions) before the interactive prompts. Reduces manual typing for entries that correspond to existing code areas.

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
