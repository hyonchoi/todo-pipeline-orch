# TODOS Archive

Completed TODOs, archived via `todos-manager --archive`.

Archived: 2026-07-14T00:00:00Z

## Entries

- [x] **TODO-24: Wire remaining harness kanban-scheduler checklist rows to production functions** — Complete checklist rows 1-6, 10-13 — mechanical production-function wiring left out of TODO-21's narrowed scope.
  - **What:** Refactor `harness.py::_poll_kanban_phases` and callees to call production functions for checklist rows 1-6 and 10-13 (registration, status polling, outcome persistence, contract lookup, timeout reporting) instead of local re-implementations. Also resolve the flagged follow-up: whether `_auto_complete_gate_tasks`'s predecessor/eligibility logic should move from harness.py into `phases.py`.
  - **Why:** TODO-21 was narrowed to only the two rows requiring new design decisions (gate completion routing, circuit-breaker premise correction). The remaining 11 checklist rows are still open acceptance criteria from `docs/checklist-harness-production-coverage.md` and must be wired for the harness to actually validate production behavior.
  - **Context:** docs/checklist-harness-production-coverage.md (rows 1-6, 10-13), hermes_pipeline/harness.py, kanban_tasks.py, phases.py, contract.py
  - **Depends on:** `TODO-21`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `worktree-todo24-harness-remaining-checklist`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** 2026-07-19
  - **Resolved design:** Most production-function wiring already existed in `harness.py` after #19 merged (re-read confirmed rows 1, 4, 6/7, 10, 11, 12 already call production functions directly; rows 2, 3 are internal to `register_todo_phases`; rows 5, 8, 9, 13 are N/A/harness-internal per the checklist doc). Actual remaining work, scoped and closed via [#21](https://github.com/hyonchoi/todo-pipeline-orch/issues/21): updated `docs/checklist-harness-production-coverage.md`'s Test link column for rows 1, 2, 3, 4, 10, 11, 12; wrote a new spy test for row 12 (contract/assignee resolution, previously untested); recorded the decision that `_auto_complete_gate_tasks`'s predecessor/eligibility logic stays in `harness.py` (harness-fixture workaround for the kanban board not propagating unblock signals, not a `phases.py`/production concern).

- [x] **TODO-22: Fine-grained checklist for harness test coverage of production code paths** — Based on the production (pipeline-watch) code and docs, a checklist to verify the harness test meets all requirements by exercising production code paths.
  - **What:** Create a fine-grained checklist mapping each harness capability to the corresponding production pipeline-watch code path, covering: phase execution (phases.run), kanban-as-scheduler (kanban_tasks.register_todo_phases, get_todo_kanban_status, all_phases_complete, observe_outcomes), contract resolution (contract.load_contract), convergence/circuit breaker (circuit.CircuitBreaker), state management (state.State, tick.TickLock), error handling (hermes_adapter error types), timeout/kill (killpg), gate handling (gates.check_gate_status), preflight, and config isolation. Each item verifies the harness uses (not re-implements) the production function.
  - **Why:** There's no structured way to verify the harness actually tests pipeline-watch behavior vs a parallel implementation. The harness re-implements convergence detection, gate handling, phase dispatch, error classification, and timeout handling — each needs a checklist item mapped to the production code path it should exercise. The checklist serves as acceptance criteria for TODO-21.
  - **Pros:** Provides measurable acceptance criteria for TODO-21 refactor, ensures no production path is left untested by the harness, serves as a regression checklist for future harness changes
  - **Cons:** Checklist may grow stale as production code evolves — needs periodic refresh. Some production paths (multi-project scan, slack alerts) may not be harness-applicable.
  - **Context:** docs/reference-kanban-as-scheduler.md, docs/howto-mock-integration-test-harness.md, docs/hermes-state-machine.md, hermes_pipeline/ (contract.py, circuit.py, runner.py, phases.py, tick.py, kanban_tasks.py, state.py)
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `worktree-todo21-harness-prod-reuse`, Test Coverage `not-required`, Security Review `not-required`
  - **Completed:** 2026-07-17
  - **Resolved design:** `docs/checklist-harness-production-coverage.md` — 13 transition rows scoped to the kanban-as-scheduler path (`_poll_kanban_phases` and callees). Design doc: `~/.gstack/projects/todo-pipeline-orchestrator/hyonchoi-main-design-20260717-113337.md`. Key finding: 2 transitions (gate auto-completion, convergence/circuit) have NO existing production counterpart in harness.py today — checklist names the correct target (`gates.py`, `circuit.py::CircuitBreaker`) for TODO-21 to build/route through, not what harness.py calls currently.

- [x] **TODO-21: Revise pipeline harness to maximum use of production module/code** — The harness is to test/verify/validate the production (pipeline-watch) code. Currently, harness is written in its own logic code. It must use production code/function as much as possible to test the production.
  - **What:** Refactor harness.py to import and delegate to production modules instead of custom implementations — e.g., use production runner, config loading, state management, tick generation, error classification, and convergence detection. Keep only fixture/seed logic in the harness.
  - **Why:** The harness re-inplements logic that already exists in production modules (runner, circuit, state, config, tick, hermes_adapter, phases). This means bugs fixed in production don't benefit the harness, and harness fixes never reach production — defeating the purpose of a test/verification tool. Reusing production code paths ensures the harness actually validates pipeline-watch behavior.
  - **Pros:** Single source of truth for pipeline logic, harness tests become integration tests (not unit tests of a parallel implementation), production bug fixes automatically improve harness coverage
  - **Cons:** Tight coupling to production internals means API changes in production modules break the harness. Requires careful dependency graph analysis to avoid circular imports. Some production functions have side effects (markers, subprocess spawns) that need fixture isolation.
  - **Context:** harness.py (28.7KB, 20 top-level symbols), production modules: cli.py, runner.py, phases.py, contract.py, kanban_tasks.py, state.py, circuit.py, tick.py, config.py, hermes_adapter.py. TODO-22's eng review found two transitions with NO existing production counterpart: (1) convergence detection — harness.py currently uses local `ConvergenceDetector`/`_ConvergenceMonitor` classes instead of `circuit.py`'s `CircuitBreaker`/`observe_from_outcomes`; (2) `_auto_complete_gate_tasks()` has no direct equivalent in `gates.py`. For these two, TODO-21 is not a pure "wire up existing calls" refactor — it also requires deciding/building the correct production function to route through, per the checklist in `docs/checklist-harness-production-coverage.md`.
  - **Depends on:** `TODO-19`, `TODO-20`, `TODO-22`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `worktree-todo21-harness-prod-reuse`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-20: Add `--kanban {null,hermes}` option to `hermes-pipeline test`** — Let the mock integration harness exercise the real HermesKanbanAdapter, not just NullKanbanAdapter
  - **What:** Add a `--kanban {null,hermes}` CLI flag to the `test` subcommand (`cli.py:566`), thread it through `run_harness()` (`harness.py:352`), and when `hermes` is selected, construct `HermesKanbanAdapter(outbox, active_tasks)` wired to `KanbanOutbox`/`ActiveTasksStore` paths under the fixture's temp `state_dir`, instead of hardcoding `NullKanbanAdapter()` (`harness.py:410`).
  - **Why:** The harness currently can't validate real kanban sync behavior (task creation, phase comments, complete/archive) end-to-end against a mock project — it silently no-ops. TODO-19's harness already runs real `hermes`/`claude` subprocesses for phases; kanban is the one system left mocked.
  - **Pros:** Closes the last gap in true end-to-end pipeline verification; reuses the existing `HermesKanbanAdapter`/outbox machinery with no new abstractions.
  - **Cons:** Real kanban calls against a mock tenant require a reachable `hermes kanban` backend/tenant — may need a dedicated test tenant or additional mocking at the `hermes kanban` CLI boundary to stay hermetic.
  - **Context:** `docs/howto-mock-integration-test-harness.md` also got a new "Run with real kanban adapter" step documenting the flag.
  - **Depends on:** `TODO-19`
  - **Assumptions:** A test/mock kanban tenant is available or acceptable for CI use; if not, this TODO may need to scope down to "outbox/dry-run verification only."
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Branch `feature/harness-real-kanban-adapter`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.5.0 (2026-07-16)

- [x] **TODO-19: partial impl of TODO-4, integration test data that is repeatable, verifiable mock data for whole pipeline from start to the end** — Repeatable mock integration test harness for pipeline end-to-end verification
  - **What:** Five deliverables: (1) setup script + mock project fixtures (git, preset, TODOS.md) in temp dir, (2) pipeline execution through mock project, (3) monitoring/verification of pipeline steps and kanban status, (4) findings report generation, (5) loopable 1-4 for iterative fix cycles. Assumes local running Hermes configuration.
  - **Why:** TODO-4 (end-to-end integration harness) is P1 with no implementation progress. This partial impl creates repeatable, verifiable mock data — a prerequisite for debugging cross-system bugs (Hermes + Kanban + Claude Code) without manual setup each time. Prior test infra (TODO-16) is structural-only; no integration-level fixtures exist.
  - **Pros:** Deterministic reproduction for cross-system debugging, reusable fixtures for future integration tests, validates pipeline end-to-end without prod data
  - **Cons:** Temp dir setup may not capture all edge cases of a real project. Hermes-local assumption limits portability. Report generator is a new artifact to maintain.
  - **Context:** TODO-4 (parent), TODO-16 (skill-test-environment Phase 1, tests/skill-test-environment/). Pipeline modules: hermes_pipeline/decision/, hermes_pipeline/kanban.py, hermes_pipeline/runner.py, hermes_pipeline/phases.py
  - **Depends on:** `TODO-4`, `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P1`, Effort `L`, Phase `4 (Development)`, Branch `feature/integration-test-harness`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.4.11 (2026-07-15)

- [x] **TODO-18: add `--revise` subcommand for fixing existing TODOs** — Fill missing fields or refine decisions after audit/convert
  - **What:** Add a `--revise` subcommand to the todos-manager skill: select an existing TODO-<n> from TODOS.md, scan for missing or weak fields (What, Why, Decisions, Branch, etc.), auto-research the codebase to pre-fill gaps, present a confirm/edit gate, and write the updated entry back to disk. Reuses the auto-research phase from `--add`.
  - **Why:** `--audit` and `--convert` are report-only with no path to fixing discovered issues. After an audit surfaces missing What/Why/Decisions or incomplete fields, users have no skill-driven workflow to fill the gaps — they must manually edit TODOS.md.
  - **Pros:** Closes the audit→fix loop without manual markdown editing. Reuses auto-research from `--add` for gap filling. Keeps entries schema-compliant after `--convert` migration.
  - **Cons:** Adds a subcommand to an already feature-rich skill. Edge case: revising archived entries may surprise users who expect archives to be append-only.
  - **Context:** Skill source: `skills/todos-manager/SKILL.md` (section-skeleton pattern). Related entries in this repo's TODOS-archive.md were flagged by the prior audit (TODO-9, TODO-11, TODO-12, TODO-13, TODO-14, TODO-15 missing What/Why/Branch).
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Branch `feat/todos-manager-revise`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.4.10 (2026-07-14)

- [x] **TODO-17: Add `--list` subcommand to todos-manager** — List existing active todos, `--all` flag includes archived
  - **What:** Add a `--list` subcommand to the todos-manager skill that displays all active TODO entries from TODOS.md in a formatted, readable summary showing ID, status, title, and summary for each entry. Support an optional `--all` flag that also includes entries from TODOS-archive.md.
  - **Why:** Users frequently need to see what TODOs exist without running a full audit. A lightweight listing command provides quick visibility into project state and reduces context-switching overhead.
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `debug/todos-manager`, Test Coverage `not-required`, Security Review `not-required`
  - **Completed:** v0.4.8 (2026-07-13)

- [x] **TODO-16: skill test environment Phase 1 for todos-manager** — Structural unit test suite with golden files
  - **Completed:** v0.4.7 (2026-07-13)
  - **What:** Build `tests/skill-test-environment/` — a demo-project TODOS.md/TODOS-archive.md fixture, golden YAML assertion files per subcommand (`--add`, `--init`, `--audit`, `--archive`), and pure-Python verification modules (`skill_logic.py`, `verify.py`) covering ID sequencing, entry parsing, format validation, and archive logic.
  - **Why:** The `todos-manager` skill (markdown-based, prompt-driven) had no automated regression coverage. Structural unit tests catch schema/logic regressions at zero token cost, in under 5 seconds, before any agent-driven semantic testing is needed.
  - **Context:** Design in [docs/gstack/hyonchoi-main-design-20260711-153841.md](docs/gstack/hyonchoi-main-design-20260711-153841.md). Plan in [docs/superpowers/plans/2026-07-11-skill-test-environment-phase1.md](docs/superpowers/plans/2026-07-11-skill-test-environment-phase1.md). Phase 2 (agent-driven, AI-judged semantic validation) is deferred — related to [[TODO-4]]'s broader integration harness but scoped narrower (structural only, no agent spawning).
  - **Depends on:** none
  - **Decisions:** Priority `P2`, Effort `M`, Phase `4 (Development)`, Branch `feature/skill-test-environment`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-15: design and register a dedicated Hermes profile for the pipeline orchestrator** — Purpose-built profile for kanban-as-scheduler
  - **Completed:** docs/superpowers/plans/2026-07-10-pipeline-profile.md (2026-07-10)
  - **What:** Design a Hermes profile specifically matched to this pipeline orchestrator's needs, and provide a way to register it via `init` or a dedicated subcommand (e.g., `pipeline-watch setup-profile` or `hermes profile install` from the project repo). The profile should configure the right model, tool permissions, skills, and behavior for driving kanban phases autonomously.
  - **Why:** The "default" Hermes profile is a general-purpose chat profile — not optimized for unattended, goal-driven kanban task execution. A purpose-built profile can lock in the right model (e.g., one with code and bash permissions), attach relevant skills (gstack autoplan, writing-plans, finishing-a-development-branch), and set safe-mode constraints appropriate for automated pipeline work. This avoids the risk of running the pipeline with a profile the operator customized for interactive use.
  - **Pros:** Predictable pipeline behavior independent of the operator's personal Hermes setup. The profile can be versioned with the project, making onboarding trivial. `hermes profile install` or `pipeline-watch init` sets it up once.
  - **Cons:** The profile definition needs to keep pace with Hermes profile schema changes. A second profile adds a small per-task memory overhead (profile context).
  - **How — profile shape (tentative):**
    - Profile name: `"pipeline"` or `"orchestrator"` — short, distinct from operator profiles.
    - Model: auto-pinned to the selection model (respects TODO-5's fallback ladder via `hermes fallback`).
    - Tools: `Read`, `Write`, `Bash` — the core set needed for phase execution.
    - Skills: attach gstack skills used by phases (autoplan, writing-plans, finishing-a-development-branch).
    - Safe-mode: enabled to prevent interactive prompts (no `input()`, no user-facing UI).
  - **How — registration:** Provide a `pipeline-watch init` or `pipeline-watch setup-profile` subcommand that calls `hermes profile create <name> --model ... --tools ... --skills ...` or `hermes profile install <dist-url>`. The subcommand should detect whether the profile already exists and skip if so. Run once during onboarding.
  - **Depends on:** none (can be designed now; the default for TODO-14 will point to this profile once it ships)
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Test Coverage `required`, Security Review `required`

- [x] **TODO-14: kanban assignee configuration** — Configurable profile for kanban task assignee
  - **Completed:** vUnreleased (2026-07-09)
  - **What:** Add a config setting for the Hermes profile used as the `--assignee` when registering kanban tasks via `register_todo_phases`. Default: the profile created by TODO-15.
  - **Why:** Kanban tasks are created with `--assignee` so the dispatcher processes them. The hardcoded value should be configurable per-project so operators can route phases to different profiles (e.g., a dedicated pipeline profile).
  - **How — config shape:** Solved via `.hermes/pipeline.toml` contract (`assignee` field) instead of `config.toml`. `pipeline-watch init` writes the default; `pipeline-watch doctor` validates drift.
  - **Depends on:** none (the `--assignee` flag was added in the circuit breaker / dispatch fix on this branch)
  - **Decisions:** Priority `P3`, Effort `S`, Phase `4 (Development)`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-13: add `--verbose` / `--debug` logging flags** — Improve debugging experience
  - **Completed:** v0.3.2 (2026-06-19)
  - **What:** Add `--verbose` and `--debug` CLI flags to `pipeline-watch` commands. `--verbose` increases log output to include informational details (tick_id, lock state, selection results). `--debug` enables full debug logging (agent call summaries, circuit breaker transitions, kanban registration). Flags should route through Python's logging module with appropriate levels (INFO vs DEBUG).
  - **Why:** Debugging pipeline issues currently requires digging through `.hermes/` state files manually. A `--debug` flag would surface internal state inline — what the selection agent received, what it returned, why a TODO was or wasn't selected, lock acquisition details, etc.
  - **Depends on:** none
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Test Coverage `not-required`, Security Review `not-required`

- [x] **TODO-12: enable multi-project tick scanning with project-level config** — Scan-and-per-project-selection tick
  - **Completed:** this branch (2026-06-23)
  - **What:** Refactor `pipeline-watch tick` (no `project` argument) to scan `projects_dir` for active projects and run one selection per project. Introduce `<project>/.hermes/project.toml` as a per-project marker file for filtering and config.
  - **Why:** The current `tick <project>` requires one Hermes cron entry per project. With many projects this is unmanageable and defeats the kanban-as-scheduler model — one cron, one tick, one global lock should drive the whole pipeline.
  - **How — filtering:** `projects_dir.iterdir()` → `TODOS.md` exists? → `.hermes/project.toml` says `enabled = false`? → skip. Default (no file) is active — opt-out for archived projects.
  - **How — config shape:**
    ```toml
    # <project>/.hermes/project.toml
    [active]
    enabled = true  # default if file missing; set false to archive
    slack_channel = "project__my-slug"  # per-project alert/notification channel
    ```
  - **How — Slack notification:** Use `project.toml`'s `slack_channel` if set, otherwise fall back to `PIPELINE_SLACK_CHANNEL` env var, otherwise fall back to `#alert`. Notifications go on: new selection, circuit breaker trip, phase completion.
  - **How — tick flow:** Single global lock, single global `current_tick_id.txt`. For each active project: build `SelectionContext`, run `run_selection`, register kanban phases. Prior-tick check (`all_phases_complete`) is per-project — if a project has an in-flight tick, skip it and move to the next project.
  - **Pros:** One cron entry drives the whole pipeline. Project-level config is filesystem-based (no network calls before selection). Slack channel is configurable per-project without global env coupling.
  - **Cons:** Need to coordinate multiple `register_todo_phases` calls under one lock. The `current_tick_id.txt` becomes shared across projects — need per-project `picked_none` sentinel in outcomes. `build_context` and `run_selection` are project-scoped — the tick loop becomes the orchestrator of multiple contexts.
  - **Context:** The `tick` subcommand currently requires a `project` argument (`cli.py:287`). `collect_pending` in `status.py:67` already demonstrates the scan pattern. `Config.projects_dir` is wired up via `PIPELINE_PROJECTS_DIR`. Slack channel is currently global via `PIPELINE_SLACK_CHANNEL` in `config.py:40`.
  - **Depends on:** none (builds on existing tick infrastructure from TODO-11)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/multi-project-tick`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-11: rewrite getting-started tutorial to use manual trigger, not cron** — Testing/debugging without waiting
  - **Completed:** v0.3.1 (2026-06-16)
  - **What:** Add a `pipeline-watch tick` CLI subcommand that fires a single tick immediately (mint tick_id, acquire lock, run selection, spawn phase — same logic as the cron path). Rewrite the getting-started tutorial so that the primary workflow uses `pipeline-watch tick` for manual triggering. Move `hermes cron set pipeline-tick` to an "Autopilot" or "Production" section at the end of the tutorial — cron is for production, manual triggering is for development and debugging.
  - **Why:** The current tutorial makes users set up a cron job and wait up to 5 minutes just to see if the pipeline works. For testing and debugging, the primary workflow should be: make a change, run `pipeline-watch tick`, see the result immediately. Cron is how you run the pipeline in production — it shouldn't be the getting-started path.
  - **Pros:** Onboarding is instant — users verify their setup in seconds. Debugging is iterative: fire a tick, check `.hermes/` state, fix, fire again. The tutorial itself becomes testable and fast.
  - **Cons:** Need to ensure `pipeline-watch tick` shares the same lock semantics and decision pipeline as the cron `pipeline-tick` path — two entry points must behave identically.
  - **Context:** The getting-started tutorial (Step 4) says "The first tick may take up to 5 minutes to fire. While you wait, move on to the next steps." — that's the UX gap. After TODO-10 lands, `pipeline-watch tick` replaces that wait. Check if Hermes cron supports `hermes cron run pipeline-tick` (one-shot fire) — if so, the tutorial could use that instead of adding a new CLI subcommand.
  - **Depends on:** `TODO-10` (needs pipeline-tick to exist before there's something to trigger manually)
  - **Decisions:** Priority `P3`, Effort `S`, Phase `4 (Development)`, Test Coverage `not-required`, Security Review `not-required`

- [x] **TODO-10: implement `pipeline-tick` Hermes command** — The cron-driven selection loop
  - **Completed:** v0.3.1 (2026-06-16)
  - **What:** Implement the `pipeline-tick` command that `hermes cron set pipeline-tick '*/5 * * * *'` fires every 5 minutes. The command mints a ULID tick_id, acquires `.hermes/tick.lock` (atomic mkdir), calls `hermes_pipeline.decision.run_selection(tick_id, ctx)`, persists the decision, and spawns `pipeline-phase` for selected TODOs. Concurrent ticks exit early ("tick already in flight, skipping"). Stale-lock sweep for holders older than `max_tick_duration_min`.
  - **Why:** The tutorial (`docs/tutorial-getting-started.md`), README, CHANGELOG, and superpowers plan all assume `pipeline-tick` exists as a Hermes command — but the Python code has no handler for it. The tutorial is ahead of the code; the cron fires a command that isn't registered, so the pipeline never actually drives itself.
  - **Context:** Design lives in [docs/superpowers/plans/2026-06-13-hermes-centric-selection.md](docs/superpowers/plans/2026-06-13-hermes-centric-selection.md) (lines 7, 468, 2119, 2577). State machine in [docs/hermes-state-machine.md](docs/hermes-state-machine.md). Circuit breaker backoff in [hermes_pipeline/circuit.py:21](hermes_pipeline/circuit.py:21) already passes `["hermes", "cron", "set", "pipeline-tick", ...]` but the command itself doesn't exist.
  - **Depends on:** `TODO-2`, `TODO-3`, `TODO-6` (needs hermes decision agent, hermes process routing, hermes LLM routing)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-9: fix pre-existing eval test failure — missing `.hermes/prompts/selection.md`** — Eval infrastructure repair
  - **What:** The eval test suite (`tests/eval/runner.py::test_selection_fixture`) fails on both `main` and feature branches because `.hermes/prompts/selection.md` does not exist. Create the prompt file or provision it from Hermes.
  - **Why:** Eval tests are the regression gate for selection-agent behavior. Without them, changes to `decision/agent.py` and prompt handling can silently regress.
  - **Context:** Noticed by gstack /ship on 2026-06-15 on branch `worktree-todo-6-hermes-adapter`. Error: `FileNotFoundError: [Errno 2] No such file or directory: '.hermes/prompts/selection.md'` at `hermes_pipeline/decision/agent.py:23` in `compute_prompt_sha()`. Test requires `ANTHROPIC_API_KEY` env var and a working Hermes install with the selection prompt.
  - **Depends on:** none
  - **Decisions:** Priority `P0`, Effort `S`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-8: replace `phase_8_finish_branch` with gstack `ship` — Kanban-gated merge to main, skip PR** — Ship straight to main via Kanban approval
  - **Completed:** v0.3.4 (2026-06-29)
  - **What:** Replace the terminal `phase_8_finish_branch` (opens a PR and HALTs) with a phase that runs gstack `/ship` to merge directly into `main` (no PR). The human gate is the **Kanban board**, not a TTY prompt: after Phase 7 completes, the runner sets Kanban status to `ready_for_review` and halts; when the operator moves the card back to `running` (approval), the watcher resumes the TODO and executes Phase 8 (ship). The typed-confirmation `input()` in [merge.py:27-29](hermes_pipeline/merge.py:27) is removed.
  - **Why:** The current TTY prompt is the wrong UX — it requires the operator to be at the orchestrator's terminal at the moment of merge. The Kanban board is already the source of truth for TODO state, and the operator already interacts with it; promoting it to the approval surface means review can happen from anywhere (web/mobile) and the orchestrator stays unattended.
  - **Pros:** Asynchronous human review — operator approves from the Kanban UI, not by sitting at the orchestrator's TTY. Single source of truth (Kanban) for both pipeline state and approval. Removes the `gh`/PR round-trip. gstack `/ship` already handles VERSION bump + CHANGELOG, which deduplicates [merge.py:32-77](hermes_pipeline/merge.py:32).
  - **Cons:** Loses PR history on GitHub as a review artifact (mitigation: have `/ship` push the merge commit to main, which preserves diff on GitHub even without a PR). Watcher must reliably detect the Kanban `ready_for_review → running` transition without polling storms. Phase 9 (`merge.py`) and `/ship` overlap on bump/changelog — pick one source of truth before this lands.
  - **Context — current state:** Terminal phase prompt is "Use superpowers finishing-a-development-branch. Open a PR and HALT — do NOT merge." in [phases.yaml:42-48](configs/phases.yaml:42). Runner sets `ready_for_review` + holds the lock at [runner.py:237-263](hermes_pipeline/runner.py:237). Kanban already models the required state machine — `PhaseStatus = Literal["running", "done", "failed", "ready_for_review"]` in [kanban.py:21](hermes_pipeline/kanban.py:21) — so the new gate reuses existing transitions, not new columns. Routes through Hermes per [[TODO-6]].
  - **Human review gate — target design:** (1) Phase 7 (`document_release`) completes; runner writes `ready_for_review` record and sets Kanban card status to `ready_for_review` (same as today). Runner halts the TODO; lock remains held. (2) Operator opens the Kanban board, reviews the branch diff + Phase-7 artifacts, and drags the card from `ready_for_review` back to `running` (or fires a Kanban transition equivalent). (3) Watcher detects the `ready_for_review → running` transition for a TODO whose pipeline state is "awaiting approval" and resumes execution at Phase 8. (4) Phase 8 runs gstack `/ship` with merge-to-main, skip-PR mode; on success Kanban transitions to `done` and the lock is released; on failure Kanban → `failed` and the lock is held for retry. The `confirm_fn` injection point in [merge.py:80-87](hermes_pipeline/merge.py:80) is repurposed (or removed) — the gate is now a Kanban-state check, not a TTY prompt. Tests can drive the gate by calling the Kanban adapter directly instead of mocking `input()`.
  - **Human review gate audit (current behavior, for contrast):** Two-pronged today. (a) GitHub PR review (Phase 8 opens a PR, operator reviews on github.com). (b) TTY `input()` requiring the operator to type the literal string `TODO-<n>` at [merge.py:27-29](hermes_pipeline/merge.py:27). Under this TODO **both** prongs are replaced by the single Kanban-state gate.
  - **Assumptions:** gstack `/ship` exposes a flag/mode to merge into main without opening a PR (verify with `/ship --help` before implementation); the Kanban adapter exposes (or can be extended to expose) a transition-watch primitive — see `update_phase` semantics in [kanban.py:400-440](hermes_pipeline/kanban.py:400); `git push origin main` is permitted on this repo by the operator; the operator interacts with Kanban regularly enough that asynchronous approval is acceptable (no SLA on approval latency from the orchestrator's perspective).
  - **Resolved design — Kanban status reuse:** Reuse the existing `running` status for post-approval execution; **do not** introduce an `approved` status. `PhaseStatus = Literal["running", "done", "failed", "ready_for_review"]` at [kanban.py:21](hermes_pipeline/kanban.py:21) is already wired through `update_phase`, comment formatting, op-log replay, and all adapters — adding a new value means touching every site, every test, and every external board mapping. Semantically `running` is honest: `ready_for_review` is the pause, `running` is execution; there is no third thing to name.
  - **Resolved design — disambiguating "approval" vs "normal running":** Use a runner-side `awaiting_approval` flag in the existing `ready_for_review` state record (do not rely on Kanban status alone — it's external, human-editable, and lossy). Flow: (1) Phase 7 completes → runner writes `ready_for_review` record with `awaiting_approval: true`; Kanban → `ready_for_review`. (2) Watcher poll loop, for each TODO with `awaiting_approval == true`, reads Kanban status; if status is now `running` (operator moved the card), watcher transitions the record to `awaiting_approval: false, approved_at: <ISO-8601>` and dispatches Phase 8. (3) Phase 8 runs `/ship`; runner does not need to re-set Kanban to `running` (operator's move already did so — make it an idempotent no-op write if anything). (4) On success: Kanban → `done`. On failure: Kanban → `failed`, `awaiting_approval` stays `false`, lock held for retry. Watcher check is a one-liner: `if rec.awaiting_approval and kanban.get_status(todo) == "running": resume()`. Gate becomes testable without touching Kanban — flip the flag in a fixture and assert Phase 8 fires.
  - **Depends on:** `TODO-6`, `TODO-7`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/ship-replaces-finish-branch`, Test Coverage `required`, Security Review `required`, Kanban Status Reuse `running (no new approved column)`, Approval Signal `runner-side awaiting_approval flag + Kanban running transition`

- [x] **TODO-7: insert gstack `review` phase before `cso`** — Code-review pass with codex voice
  - **Completed:** v0.4.0 (2026-07-07)
  - **What:** Add a new phase between `phase_4_development` and `phase_6_1_cso` in `configs/phases.yaml` that runs the gstack `/review` (a.k.a. `code-review`) skill — with codex voice when applicable (`/code-review --voice codex` / `--codex`) — autofixes findings, and commits.
  - **Why:** Today the pipeline jumps straight from development into security (CSO). Functional/code-quality review is missing, so correctness bugs and reuse/simplification cleanups only get caught at human PR review (or not at all).
  - **Pros:** Catches correctness + reuse/simplification issues earlier in the pipeline, before CSO and before the human gate. codex voice gives a second-opinion review style. Mirrors how gstack's own `ship` flow expects a clean review before landing.
  - **Cons:** Adds turns/cost per TODO. Auto-fix can regress tests — phase prompt must require running tests after fixes and rolling back on failure.
  - **Context:** New phase key suggestion: `phase_5_review`. Prompt: "Run gstack `/code-review --codex` (or `--voice codex`); apply findings with `--fix`; run tests; commit with message `chore: address review findings`." Existing phases live in `configs/phases.yaml`. Routes through Hermes per [[TODO-6]] (`hermes chat -q "use code-review skill ..." -Q`).
  - **Assumptions:** gstack `code-review` skill is installed (see CLAUDE.md skills index — `/code-review` is listed); codex voice is supported by the current `/code-review` invocation; tests are runnable via `uv run pytest` from the repo root.
  - **Depends on:** `TODO-6`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/phase-5-review`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-6: route LLM queries through `hermes` instead of direct Claude calls** — Hermes as the only LLM surface
  - **Completed:** v0.3.0 (2026-06-15)
  - **What:** Remove any direct `claude`/Anthropic SDK invocations from the orchestrator and route all LLM queries through the `hermes` command.
  - **Why:** Direct Claude usage bypasses Hermes' control surface, breaking the Hermes-centered execution model and producing drift in prompt/model policy.
  - **Pros:** Centralizes LLM policy (model pinning, prompt SHA, fallback ladder) under Hermes; consistent with TODO-3's process-routing rule.
  - **Cons:** Requires auditing existing call sites and may need new Hermes subcommands where coverage is missing.
  - **Context:** Narrows TODO-3 specifically to LLM query paths (decision agent, selection agent, any ad-hoc Claude calls). Coordinates with TODO-5's model-lifecycle policy. Hermes CLI surfaces: primary path is `hermes chat -q "<prompt>" -Q -m <model> --source tool` (quiet, non-interactive; `--ignore-user-config`/`--ignore-rules`/`--safe-mode` for isolated CI/eval runs). Lower-effort migration path for existing Anthropic-SDK call sites is `hermes proxy start` (local OpenAI-compatible proxy) — just redirect `base_url`. `hermes model` sets default model+provider so `decision/agent.py` no longer hardcodes one. `hermes fallback` already implements the fallback ladder TODO-5 specifies — TODO-5 can collapse into "configure `hermes fallback`" rather than reinventing in `.hermes/config.toml`.
  - **Assumptions:** Hermes is correctly configured with a working model on the target machine, including external Claude invocation (auth via `hermes login` / `hermes auth`, model selectable via `hermes model`, end-to-end query via `hermes chat -q` returns a valid response). The orchestrator does not own Hermes provisioning — broken Hermes config is out of scope and surfaces as a `hermes chat` non-zero exit / stderr, not as logic this task must handle.
  - **Depends on:** `TODO-3`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/hermes-llm-routing`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-3: route non-Hermes process spawning through Hermes commands** — Hermes as the only process control surface
  - **What:** Require all process-spawning paths, except direct execution of `hermes ...` itself, to route through Hermes commands instead of invoking tools directly.
  - **Why:** Direct non-Hermes process execution creates behavior drift, bypasses intended control surfaces, and weakens the Hermes-centered execution model.
  - **Pros:** Keeps orchestration aligned with the Hermes contract, centralizes execution policy, and reduces hidden shell integrations.
  - **Cons:** Increases coupling to Hermes command/skill coverage and may require refactors where code shells out to system tools.
  - **Context:** Examples include using `hermes cron ...` instead of `crontab`, and routing Claude Code invocation through Hermes-managed skill paths.
  - **Depends on:** none
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/hermes-process-routing`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-2: use Hermes agent for TODO parsing and selection** — Agent-first parsing for irregular TODO files
  - **What:** Make TODO parsing and task selection rely on the Hermes agent with an explicit instruction layer instead of assuming a fully strict file schema.
  - **Why:** The project must extract useful task data from irregular TODO formats and still select the correct task even when structure is partial.
  - **Pros:** Handles real-world TODO files, improves selection accuracy for noisy structure, and aligns behavior with project requirements.
  - **Cons:** Adds prompt-design and evaluation work beyond regex parsing. May require stricter validation for deterministic selection.
  - **Context:** Applies to TODO ingestion and selection behavior across the Hermes pipeline where TODO structure can be mixed or inconsistent.
  - **Depends on:** none
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/hermes-todo-selection`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-1: todos-manager counter recovery mode** — Add `pipeline-watch recover-counter`
  - **Completed:** v0.3.2 (2026-06-19)
  - **What:** Add `pipeline-watch recover-counter` that scans `TODOS.md` for the max existing `TODO-<n>` ID and initializes `.hermes/todo_id_counter` to that value.
  - **Why:** Prevent ID collisions when bootstrapping a project that already has hand-written `TODO-<n>` entries but no counter file yet.
  - **Pros:** Closes the only remaining gap in the todos-manager spec. Small and isolated implementation.
  - **Cons:** Not needed until a project has pre-existing `TODO-<n>` entries without a counter file, so it does not block current work.
  - **Context:** See `docs/gstack/hyonchoi-main-design-20260610-195349.md` section "TODOS Manager Skill (`todos-manager`)" and the "NOT in scope" / Test Plan note.
  - **Depends on:** none
  - **Decisions:** Priority `P3`, Effort `S`, Phase `4 (Development)`, Branch `feature/todos-manager-counter-recovery`, Test Coverage `required`, Security Review `not-required`

- [x] **TODO-31: Add 'UI Review' decision for phase_6_2 skip signal to TODOS.md schema** — Document `UI Review required/not-required` in schema, auto-research, SKILL.md, and preamble so new TODO entries always carry the skip signal phase_6_2 reads.
  - **What:** Add `UI Review required/not-required` to the Decisions field definition in sections/schema.md, sections/auto-research.md derivation rules, SKILL.md workflow/preamble, and TODOS.md preamble blockquote. Auto-detect from title/summary keywords: ui, frontend, design, visual, layout, component, css, style, dashboard, artifact, page, screen, modal, form, navigation, button, icon, animation.
  - **Why:** phase_6_2_qa prompt already checks `UI Review: required` in Decisions, but the sub-field is undocumented in schema, auto-research, SKILL.md, and preamble — new entries omit it, causing phase_6_2 to have no skip signal to check.
  - **Pros:** All new TODO entries get a UI Review decision; phase_6_2 can skip non-UI work without dispatching a no-op QA task; consistent with Security Review pattern
  - **Cons:** Adds another Decisions sub-field — revising old entries to backfill UI Review is a manual effort for ~30 existing TODOs
  - **Context:** phases.yaml phase_6_2_qa already reads `UI Review: required`; TODO-24 (phases.yaml refinement) is where this gap was surfaced.
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P2`, Effort `S`, Phase `2 (Design)`, Branch `feature/ui-review-decision-schema`, Test Coverage `not-required`, Security Review `not-required`
  - **Completed:** v0.5.7 (2026-07-23)

- [x] **TODO-30: Add live status monitoring to `pipeline-watch test` poll loop** — Replace silent timeout wait with real-time kanban phase status table and transitions
  - **What:** (1) Add live console output to `_poll_kanban_phases()` in harness.py — print an initial status table after registration, then log each phase status transition (→ running, → done, → failed) as the poll loop detects them. (2) Raise the `--timeout` default for `pipeline-watch test` from 3600s (1h) to 86400s (24h) so healthy long test runs are no longer killed by the default.
  - **Why:** The `test` command sits silent during the entire poll duration, giving no feedback until timeout or completion. Users can't track which phases are executing or see progress. Additionally, the artificial timeout can kill a running pipeline before phases finish.
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `worktree-todo-30-live-status-monitoring`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.5.8 (2026-07-23)

- [x] **TODO-29: Remove dead post-phases.run cleanup: ready_for_review/merge.py + cmd_kill markers** — merge.py's run_phase9/_cmd_merge and cli.py's cmd_kill both depend exclusively on state written by phases.py's run()/_invoke_hermes — dead once TODO-27 removes that code, and cmd_kill is already non-functional against real kanban-dispatched phases today.
  - **What:** Delete `merge.py` (`run_phase9`, `make_default_bump_fn`, `default_bump_fn`, `default_confirm_fn`), the `cli.py merge` subcommand (`_cmd_merge`, its parser registration at `cli.py:421`), and `ReadyForReview`/`read_ready_for_review`/`write_ready_for_review`/`write_ready_for_review_min`/`list_ready_for_review_pending` in `state.py`. Also resolve `cmd_kill` (`cli.py:172`), `_kill_all_projects` (`cli.py:269`), and the marker helpers (`_marker_path`, `_write_marker`, `_update_marker_pid`, `_delete_marker`, `MarkerHeld` in `phases.py`): once TODO-27 deletes `phases.py`'s `run()`/`_invoke_hermes` (the only marker writers), nothing ever populates `phase_started/*.json` for a real kanban-dispatched phase. Either (a) delete `cmd_kill`/marker machinery entirely, or (b) redesign it to kill in-flight phases via real kanban task state (`get_todo_kanban_status`/kanban task IDs) instead — this half is a design decision, not pure deletion.
  - **Why:** `state.write_ready_for_review`/`write_ready_for_review_min` — the only writers of a `ready_for_review` record — live exclusively in `phases.py:342` (inside `_invoke_hermes`) and `runner.py:301` (inside `PipelineRunner`), both deleted by TODO-27; production's real Phase 9 path is `ship.py`'s `maybe_ship_ready`/`approve_ship` instead, which never touches `ready_for_review`. Separately, `_write_marker` is called only inside `phases.py`'s `run()` — no call site exists on the real production path (`cli.py`'s `_cmd_tick` → `register_todo_phases` → kanban tasks spawn hermes agents outside this codebase) — so `cmd_kill` reading `state_dir/phase_started/` already always reports "no in-flight phases" for real pipeline runs; it's dead/non-functional today, not just soon-to-be-dead.
  - **Context:** Surfaced while reviewing TODO-27's harness-parity cleanup. `test_phases_marker.py` exercises the marker mechanism directly (via `phases.run`), which is why the dead state wasn't obvious from tests alone. **Superseded 2026-07-22:** absorbed into the combined TODO-26+27 deletion PR (see `docs/gstack/hyonchoi-main-design-20260722-124228.md`) — the eng review for that PR independently found merge.py/ReadyForReview/cmd_kill were all orphaned by the same deletion graph and folded them in. Design decision (a) delete cmd_kill entirely — resolved automatically, since the same PR removes cmd_kill's only writer.
  - **Depends on:** `TODO-27`
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Branch `feature/remove-dead-post-phases-run-cleanup`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.5.6 (2026-07-22)

- [x] **TODO-27: Fix test harness to drive real kanban-task pipeline; remove dead phases.run/watcher.py path** — harness.py bypasses register_todo_phases and calls phases.run directly — violates the harness-must-match-production invariant; watcher.py is unreferenced dead code.
  - **What:** `harness.py` supports two modes via `--kanban {null|hermes}` (`cli.py:571`, default `"null"`). `--kanban hermes` (`_poll_kanban_phases`, `harness.py:250`) already drives the real production path — it registers phases via `register_todo_phases` and polls `get_todo_kanban_status` to observe transitions, including `_auto_complete_gate_tasks` for auto-approving the phase_9 gate in the mock project (valid to keep — the harness has no human to approve it). This part is NOT broken and stays as-is. The bug is the `--kanban null` mode (the default): it runs a local scheduler (`PipelineRunner` in `runner.py`) that drives phases via `_dispatch_phase` → `phases.run` (`harness.py:418`) → `_invoke_hermes`/`_invoke_review_phase` → `review_phase.py`'s PRE/subagent/POST split for phase_5 — a code path that never runs in production, since kanban-as-scheduler handles phase transitions itself and no local scheduler/monitor loop exists there. Fix: delete the `--kanban null` mode entirely — remove the `--kanban` CLI flag (or restrict its choices to `"hermes"` only, making it the harness's sole mode), delete `harness.py`'s null-mode branch (`_dispatch_phase`, the `PipelineRunner`/`NullKanbanAdapter` wiring at `harness.py:711-736`), delete `runner.py` (`PipelineRunner`) and `tests/test_runner.py`, then delete `watcher.py`, `phases.run`, `_invoke_hermes`, `_invoke_review_phase`, and `review_phase.py`'s PRE/POST machinery (`capture_pre_review_state`, `finalize_review`, `run_pytest`, `commit_all`) since nothing will call them anymore.
  - **Why:** Violates the harness-must-mirror-production invariant (established in a prior completed TODO). Confirmed via grep that `PipelineRunner`/`runner.py` has no callers outside `harness.py`'s null-mode branch and its own dedicated `tests/test_runner.py`; only `harness.py` and self-referencing `watcher.py` import `phases.run`; production's real flow (`cli.py`, `contract.py`, `kanban_tasks.py`) only ever imports `load_phases`/`_render_phase_prompt` — pure data, never the executing function. Surfaced while reviewing TODO-24's phase_5_review prompt: discovered the prompt's "pipeline runs tests/commits after you" claim describes a code path (`finalize_review`) that never executes for real kanban-dispatched tasks — the prompt text is the only real lever in production, so it needs redesigning to own test-run-and-commit itself, but the current dead PRE/POST code is misleading anyone reading `review_phase.py` into thinking it's live.
  - **Context:** Discovered via grep during TODO-24 phase_5_review review — `register_todo_phases` (`kanban_tasks.py:66`) renders the full prompt into the kanban task body at creation time; nothing dispatches phases dynamically afterward in production. `NullKanbanAdapter` itself stays (still legitimately used by `cli.py:686`'s `_cmd_merge` fallback for `config.kanban_adapter != "hermes"` — unrelated to this dead path). Also folds in the `phase_9_ship` → `phase_9_human_review` phase_key rename (TODO-24 renamed the display `name` only): update `hermes_pipeline/ship.py:29` (`GATE_PHASE_KEY = "phase_9_ship"`), the matching comment in `cli.py:1122`, and `phases.py:326`'s `f"todo-{todo_num}-{phase_key}"` (part of the dead harness path already being deleted here) together with the phases.yaml key change.
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P1`, Effort `L`, Phase `2 (Design)`, Branch `feature/fix-harness-production-parity`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.5.6 (2026-07-22)

- [x] **TODO-26: Remove dead plan-gate code after phase_2b_plan_gate removal** — Delete approve_plan.py, its CLI subcommand, and gates.py plan-gate logic once phases.yaml drops phase_2b.
  - **What:** Once `phase_2b_plan_gate` is removed from `phases.yaml` (TODO-24), delete the now-dead plan-gate machinery: `hermes_pipeline/approve_plan.py` (entire module), the `approve-plan` CLI subcommand in `cli.py` (parser + `_cmd_approve_plan`), and the plan-gate-specific logic in `gates.py` (`PLAN_GATE_PHASE_KEY`, `is_high_risk` status-map handling) and `gate_state.py` (confirm `gate_status()`'s default arg/callers still make sense with only `phase_9_ship` using `gate: true`), and `kanban_tasks.py`'s `all_phases_complete` partial-registration guard (~line 443: the `if key == "phase_2b_plan_gate":` branch that treats a rejected/archived gate task as a completion signal) — this branch stops matching anything once phase_2b is removed from phases.yaml and must be deleted alongside the rest.
  - **Why:** `approve-plan` was exclusively wired to `phase_2b_plan_gate` (`PLAN_GATE_PHASE_KEY = "phase_2b_plan_gate"`); once that gate is removed the CLI, its handler, and gates.py's plan-gate branch become unreachable dead code.
  - **Context:** Surfaced during TODO-24 phase_2b review — verified via grep that `approve_plan.py`/`PLAN_GATE_PHASE_KEY` have no other callers; `phase_9_ship` uses separate gate logic in `ship.py`.
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Branch `feature/remove-plan-gate-dead-code`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.5.6 (2026-07-22)

- [x] **TODO-25: TODOS.md: add optional Spec/Reference field, threaded into autoplan phase prompt** — Add a `**Spec:**` field to the TODOS.md schema; when present, phase_2_autoplan reads and passes that file to the autoplan skill.
  - **What:** Add an optional `**Spec:**` (or `**Reference:**`) field to the TODOS.md entry schema pointing to a spec/reference md file path. When a TODO entry has this field, `phases.py`'s `_render_phase_prompt` for `phase_2_autoplan` must read the field, and inject the file path/content into the autoplan prompt so the skill runs off that doc rather than only the inline TODO text.
  - **Why:** Currently phase_2_autoplan only sees the TODO's inline What/Why/Context — there's no way to hand it a fuller spec doc when one exists, forcing either inline bloat in TODOS.md or the spec being invisible to the pipeline.
  - **Context:** Surfaced during TODO-24 phase_2_autoplan review — schema change lives in `todos-manager` skill (preamble field list) plus `hermes_pipeline/phases.py` (`_render_phase_prompt`, `load_phases`).
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P2`, Effort `S`, Phase `2 (Design)`, Branch `feature/todos-spec-field`, Test Coverage `required`, Security Review `not-required`
  - **Completed:** v0.5.5 (2026-07-21)

- [x] **TODO-24: Refine gstack's phases.yaml — review phase composition and instructions** — Audit `hermes_pipeline/data/profiles/gstack/phases.yaml` for phase composition and per-phase instruction quality
  - **What:** Review `hermes_pipeline/data/profiles/gstack/phases.yaml` (9 phases: autoplan → plan_gate → writing_plan → development → review → cso → document_release → finish_branch → ship_gate) for: (1) phase composition — are these the right phases, in the right order, with correct gates; (2) each phase's `prompt` field — is the instruction clear, correctly scoped, and consistent with the underlying skill's current behavior; (3) `tools`/`turns`/`timeout` budgets — are they still appropriate. Deliverable is a revised phases.yaml (or a design doc proposing changes) — not a full pipeline rewrite.
  - **Why:** The 9-phase pipeline was assembled incrementally via prior TODOs (TODO-6/7/8), each adding/replacing a phase in isolation. No holistic review has been done of phase ordering, tool grants, turn/timeout budgets, or prompt wording as a set.
  - **Pros:** Catches stale/inconsistent phase prompts before they cause pipeline failures; opportunity to right-size turn/timeout budgets based on real run history.
  - **Cons:** Risk of scope creep into a full pipeline redesign; changes to phase prompts affect live orchestrator runs.
  - **Context:** Related prior work — `TODO-7` (added phase_5_review) and `TODO-8` (replaced phase_8 ship gate), both in TODOS-archive.md.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Branch `feature/refine-phases-yaml`, Test Coverage `not-required`, Security Review `not-required`

- [x] **TODO-33: Rename main CLI script to `tpo`** — Replace `pipeline-watch`/`hermes-pipeline` console-scripts with a single short `tpo` entry point
  - **What:** Replace both `pipeline-watch` and `hermes-pipeline` console-script entries in `pyproject.toml`'s `[project.scripts]` with a single `tpo` entry point (`hermes_pipeline.cli:main`), then update all references across docs (10+ files under `docs/`), tests (test_contract.py, test_recover_counter_cli.py, test_cli_entrypoint.py, test_tick_subcommand.py, test_cli.py), README, and CHANGELOG to use `tpo`.
  - **Why:** The current script names (`pipeline-watch`, `hermes-pipeline`) are too long to type repeatedly during manual/interactive CLI usage; a single short name (`tpo`) improves day-to-day ergonomics.
  - **Pros:** Much faster to type; single unambiguous CLI name going forward.
  - **Cons:** Full breakage of old names — every doc, test, and any external script/muscle-memory using `pipeline-watch`/`hermes-pipeline` must be updated in the same change.
  - **Context:** `pyproject.toml` `[project.scripts]` (lines 11-12); docs: howto-agent-skills-profile.md, howto-pipeline-contract.md, howto-approve-and-ship.md, howto-multi-project-setup.md, + gstack design docs; tests: test_contract.py, test_recover_counter_cli.py, test_cli_entrypoint.py, test_tick_subcommand.py, test_cli.py.
  - **Decisions:** Priority `P2`, Effort `L`, Phase `4 (Development)`, Branch `feature/rename-cli-to-tpo`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`

- [x] **TODO-34: Embed todos-manager skill as package data with `tpo skills install` subcommand** — Replace git-clone-dependent install script with an in-package installer usable after `uv tool install`
  - **What:** Move `skills/todos-manager/` into `hermes_pipeline/skills/todos-manager/`, mark it as package data in `pyproject.toml` (hatch wheel force-include or similar), and add a `tpo skills install` CLI subcommand (in `cli.py`, alongside `_cmd_install_profile` at cli.py:1070) that uses `importlib.resources.files("hermes_pipeline") / "skills/todos-manager"` to locate the packaged skill and symlinks/copies it into `~/.claude/skills/` and `~/.agents/skills/`, porting the logic currently in `scripts/install-todos-manager.sh`.
  - **Why:** `scripts/install-todos-manager.sh` requires a git clone of the repo to run, which is incompatible with the `uv tool install todo-pipeline-orchestrator` distribution model where users never see the source tree.
  - **Pros:** No git dependency; single source of truth versioned with the package; works for any `uv tool install` user; testable via pytest like other CLI subcommands.
  - **Cons:** One-time migration effort — pyproject.toml package-data wiring, porting bash symlink logic to Python, deleting/deprecating the old script.
  - **Context:** Existing subcommand pattern to follow: `_cmd_install_profile` (cli.py:1070) and its parser registration (cli.py:289). Old install path: `scripts/install-todos-manager.sh`.
  - **Depends on:** `TODO-33`
  - **Assumptions:** hatchling build backend (already in use) supports package-data inclusion for non-Python files via `[tool.hatch.build.targets.wheel]` config.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Branch `feature/embed-todos-manager-skill-install`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`

- [x] **TODO-32: Separate `data/profiles` into identity and phase-config contexts** — Split mixed Hermes identity profile and pipeline phase definitions into distinct directories
  - **What:** Separated `hermes_pipeline/data/profiles` into `hermes_pipeline/data/hermes-identity/pipeline/` (SOUL.md) and `hermes_pipeline/data/phase-profiles/` (gstack/phases.yaml, agent-skills/phases.yaml). Updated `bundled_profile_dir()` (contract.py) and `resolve_profile_phases_path()` (phases.py) call sites, added `tests/test_profile_layout_split.py` regression coverage, and updated docs/howto-agent-skills-profile.md and docs/howto-pipeline-contract.md.
  - **Completed:** v0.5.9 (2026-07-24)

- [x] **TODO-35: Add --reinstall flag, default-on-exists fail, and uninstall subcommand to skills install CLI** — Make `tpo skills install` fail when dest exists, add explicit `--reinstall` opt-in, and create `tpo skills uninstall` with confirmation
  - **What:** Add --reinstall flag to `tpo skills install` (removes existing dest before copying), make default install fail when dest exists, and add `tpo skills uninstall` subcommand with confirmation prompt.
  - **Why:** `shutil.copytree(dest, dirs_exist_ok=True)` fails on structural mismatches (file vs dir conflicts) and silently overwrites. Users need explicit opt-in to reinstall and a way to remove skills.
  - **Pros:** Prevents accidental silent overwrites; gives users clean reinstall path; adds symmetry with uninstall
  - **Cons:** Breaking change — existing scripts/aliases that double-run install without --reinstall will get errors
  - **Context:** CLI in hermes_pipeline/cli.py:1222-1265, tests in tests/test_skills_install.py
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Branch `feat/skills-install-reinstall`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
  - **Completed:** v0.6.3 (2026-07-27)

- [x] **TODO-37: Add global config file with `tpo config` command, deprecating PIPELINE_PROJECTS_DIR** — Introduce an XDG-style global config.yaml as the source for projects_dir and other base settings, with a `tpo config` subcommand to read/write it.
  - **What:** Add a global config file, resolved via a search order — `${XDG_CONFIG_HOME:-~/.config}/tpo/config.yaml`, else `~/.tpo/config.yaml`, else `${HERMES_HOME:-~/.hermes}/tpo.yaml` (first existing file wins; default to the first path if none exist) — loaded in `Config.from_env()`/`hermes_pipeline/config.py` before env var overrides are applied. Add `tpo config get <key>`, `tpo config set <key> <value>`, and `tpo config path` subcommands (mirroring the `skills` subparser pattern at cli.py:329) to read/write the YAML file. `PIPELINE_PROJECTS_DIR` env var support remains as a deprecated compatibility alias; users should prefer setting `projects_dir` via the global config file.
  - **Why:** `PIPELINE_PROJECTS_DIR` is the only base-level setting not covered by the existing per-project overlays, forcing users into ad hoc shell exports for a value that's effectively static per machine. A discoverable, persistent global config file with a `tpo config` command gives users a standard place to set install-wide defaults, consistent with the XDG convention already implied by `~/.hermes` state dir usage.
  - **Pros:** Persistent config survives shell restarts; XDG-compliant discovery order matches conventions of other CLI tools; `tpo config` subcommand makes the setting discoverable/scriptable instead of requiring users to know an undocumented env var.
  - **Cons:** Keeping PIPELINE_PROJECTS_DIR as a deprecated alias avoids a patch-release break, but leaves two ways to configure `projects_dir` until the alias is removed in a later major/minor release.
  - **Context:** hermes_pipeline/config.py (Config.from_env, env_map at line 35-42); existing per-project overlay pattern at config.py:105 (load_toml_overlay), loaded from .hermes/config.toml per cli.py:409-425 (selection/circuit_breaker settings) — distinct from .hermes/pipeline.toml (contract.py:20, phase/profile contract) and .hermes/project.toml (project_config.py:14, enabled/notifications settings); skills subcommand pattern at cli.py:329-335 for the `tpo config` subparser structure.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Branch `feature/global-config-file`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
  - **Completed:** v0.6.1 (2026-07-26)

- [x] **TODO-38: Track NEXT_TODO_ID in TODOS.md instead of scanning archive** — Track NEXT_TODO_ID in TODOS.md instead of scanning archive
  - **What:** Add a `NEXT_TODO_ID: <n>` line under `## Metadata` as the primary source for `--add`'s ID computation, replacing the per-add archive scan. Add a conflict check: before assigning NEXT_TODO_ID to a new entry, verify no existing TODO-<NEXT_TODO_ID> already exists in TODOS.md (e.g. from a manual edit or merge). If a conflict is detected, automatically run the `--audit` reconciliation scan (max ID across TODOS.md + TODOS-archive.md) to recompute and correct NEXT_TODO_ID in place, log the correction, then continue the `--add` flow with the corrected ID — no user interruption needed. `--audit` (invoked standalone) performs the same full-scan reconciliation. Out of scope: deprecating `.hermes/todo_id_counter` itself (separate cleanup).
  - **Why:** Fixes the ID-assignment mechanism discussed earlier this session: `.hermes/todo_id_counter` is gitignored, so any agent that forgets to update it causes wrong TODO-<n> picks; committing NEXT_TODO_ID directly in tracked TODOS.md removes that failure mode and the archive-scan cost.
  - **Pros:** O(1) next-ID lookup instead of archive scan in the common case; self-healing on drift (no silent bad ID assignment); state lives in a tracked file so it can't silently diverge across clones/worktrees/agents.
  - **Cons:** Requires a one-time migration to backfill NEXT_TODO_ID into existing TODOS.md files; conflict-triggered auto-audit adds a fallback archive scan back on the rare drift path (acceptable since it's no longer the common path).
  - **Decisions:** Priority `P2`, Effort `S`, Phase `2 (Design)`, Branch `feature/todos-next-id-tracking`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
  - **Completed:** v0.6.3 (2026-07-27)

- [x] **TODO-41: Global config for AI agent client selection** — Select Claude or Codex and mention the correct client in phase profiles
  - **What:** Add a global Claude/Codex client setting, propagate it through phase rendering, and replace hardcoded client references in bundled phase profiles, documentation, and tests.
  - **Why:** Phase prompts hardcode Claude Code despite supporting both Claude and Codex, causing incorrect execution instructions.
  - **Pros:** Correct client-specific prompts, clearer configuration semantics, and consistent Claude/Codex support.
  - **Cons:** Touches configuration, prompt rendering, profiles, documentation, and tests; terminology must remain distinct from phase and Hermes profiles.
  - **Context:** `hermes_pipeline/config.py`, `hermes_pipeline/config_loader.py`, `hermes_pipeline/kanban_tasks.py`, `hermes_pipeline/data/phase-profiles/`, and related docs/tests
  - **Depends on:** (none)
  - **Assumptions:** Initial supported values are `claude` and `codex`; the setting is global, while phase profile and Hermes assignee/profile concepts retain their existing meanings.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `4 (Development)`, Branch `feature/global-agent-client-selection`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
  - **Completed:** v0.7.0 (2026-07-29)
