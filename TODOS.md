# TODOS

gstack-format work queue for `todo-pipeline-orchestrator`. Each entry keeps the required fields: What/Why/Pros/Cons/Context/Depends on/Decisions. Status markers: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold. See `docs/gstack/hyonchoi-main-design-20260610-195349.md` ("TODOS Manager Skill") for the full schema and `TODO-<n>` ID assignment rules.

- [ ] **TODO-10: implement `pipeline-tick` Hermes command** — The cron-driven selection loop
  - **What:** Implement the `pipeline-tick` command that `hermes cron set pipeline-tick '*/5 * * * *'` fires every 5 minutes. The command mints a ULID tick_id, acquires `.hermes/tick.lock` (atomic mkdir), calls `hermes_pipeline.decision.run_selection(tick_id, ctx)`, persists the decision, and spawns `pipeline-phase` for selected TODOs. Concurrent ticks exit early ("tick already in flight, skipping"). Stale-lock sweep for holders older than `max_tick_duration_min`.
  - **Why:** The tutorial (`docs/tutorial-getting-started.md`), README, CHANGELOG, and superpowers plan all assume `pipeline-tick` exists as a Hermes command — but the Python code has no handler for it. The tutorial is ahead of the code; the cron fires a command that isn't registered, so the pipeline never actually drives itself.
  - **Context:** Design lives in [docs/superpowers/plans/2026-06-13-hermes-centric-selection.md](docs/superpowers/plans/2026-06-13-hermes-centric-selection.md) (lines 7, 468, 2119, 2577). State machine in [docs/hermes-state-machine.md](docs/hermes-state-machine.md). Circuit breaker backoff in [hermes_pipeline/circuit.py:21](hermes_pipeline/circuit.py:21) already passes `["hermes", "cron", "set", "pipeline-tick", ...]` but the command itself doesn't exist.
  - **Depends on:** `TODO-2`, `TODO-3`, `TODO-6` (needs hermes decision agent, hermes process routing, hermes LLM routing)
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Test Coverage `필요`, Security Review `불필요`

- [x] **TODO-11: rewrite getting-started tutorial to use manual trigger, not cron** — Testing/debugging without waiting
  - **Completed:** v0.3.1 (2026-06-16)
  - **What:** Add a `pipeline-watch tick` CLI subcommand that fires a single tick immediately (mint tick_id, acquire lock, run selection, spawn phase — same logic as the cron path). Rewrite the getting-started tutorial so that the primary workflow uses `pipeline-watch tick` for manual triggering. Move `hermes cron set pipeline-tick` to an "Autopilot" or "Production" section at the end of the tutorial — cron is for production, manual triggering is for development and debugging.
  - **Why:** The current tutorial makes users set up a cron job and wait up to 5 minutes just to see if the pipeline works. For testing and debugging, the primary workflow should be: make a change, run `pipeline-watch tick`, see the result immediately. Cron is how you run the pipeline in production — it shouldn't be the getting-started path.
  - **Pros:** Onboarding is instant — users verify their setup in seconds. Debugging is iterative: fire a tick, check `.hermes/` state, fix, fire again. The tutorial itself becomes testable and fast.
  - **Cons:** Need to ensure `pipeline-watch tick` shares the same lock semantics and decision pipeline as the cron `pipeline-tick` path — two entry points must behave identically.
  - **Context:** The getting-started tutorial (Step 4) says "The first tick may take up to 5 minutes to fire. While you wait, move on to the next steps." — that's the UX gap. After TODO-10 lands, `pipeline-watch tick` replaces that wait. Check if Hermes cron supports `hermes cron run pipeline-tick` (one-shot fire) — if so, the tutorial could use that instead of adding a new CLI subcommand.
  - **Depends on:** `TODO-10` (needs pipeline-tick to exist before there's something to trigger manually)
  - **Decisions:** Priority `P3`, Effort `S`, Phase `4 (Development)`, Test Coverage `불필요`, Security Review `불필요`

- [x] **TODO-9: fix pre-existing eval test failure — missing `.hermes/prompts/selection.md`** — Eval infrastructure repair
  - **What:** The eval test suite (`tests/eval/runner.py::test_selection_fixture`) fails on both `main` and feature branches because `.hermes/prompts/selection.md` does not exist. Create the prompt file or provision it from Hermes.
  - **Why:** Eval tests are the regression gate for selection-agent behavior. Without them, changes to `decision/agent.py` and prompt handling can silently regress.
  - **Context:** Noticed by gstack /ship on 2026-06-15 on branch `worktree-todo-6-hermes-adapter`. Error: `FileNotFoundError: [Errno 2] No such file or directory: '.hermes/prompts/selection.md'` at `hermes_pipeline/decision/agent.py:23` in `compute_prompt_sha()`. Test requires `ANTHROPIC_API_KEY` env var and a working Hermes install with the selection prompt.
  - **Depends on:** none
  - **Decisions:** Priority `P0`, Effort `S`, Test Coverage `필요`, Security Review `불필요`

- [ ] **TODO-1: todos-manager counter recovery mode** — Add `todos-manager --recover-counter`
  - **What:** Add `todos-manager --recover-counter` that scans `TODOS.md` for the max existing `TODO-<n>` ID and initializes `.hermes/todo_id_counter` to that value.
  - **Why:** Prevent ID collisions when bootstrapping a project that already has hand-written `TODO-<n>` entries but no counter file yet.
  - **Pros:** Closes the only remaining gap in the todos-manager spec. Small and isolated implementation.
  - **Cons:** Not needed until a project has pre-existing `TODO-<n>` entries without a counter file, so it does not block current work.
  - **Context:** See `docs/gstack/hyonchoi-main-design-20260610-195349.md` section "TODOS Manager Skill (`todos-manager`)" and the "NOT in scope" / Test Plan note.
  - **Depends on:** none
  - **Decisions:** Priority `P3`, Effort `S`, Phase `4 (Development)`, Branch `feature/todos-manager-counter-recovery`, Test Coverage `필요`, Security Review `불필요`

- [x] **TODO-2: use Hermes agent for TODO parsing and selection** — Agent-first parsing for irregular TODO files
  - **What:** Make TODO parsing and task selection rely on the Hermes agent with an explicit instruction layer instead of assuming a fully strict file schema.
  - **Why:** The project must extract useful task data from irregular TODO formats and still select the correct task even when structure is partial.
  - **Pros:** Handles real-world TODO files, improves selection accuracy for noisy structure, and aligns behavior with project requirements.
  - **Cons:** Adds prompt-design and evaluation work beyond regex parsing. May require stricter validation for deterministic selection.
  - **Context:** Applies to TODO ingestion and selection behavior across the Hermes pipeline where TODO structure can be mixed or inconsistent.
  - **Depends on:** none
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/hermes-todo-selection`, Test Coverage `필요`, Security Review `불필요`

- [x] **TODO-3: route non-Hermes process spawning through Hermes commands** — Hermes as the only process control surface
  - **What:** Require all process-spawning paths, except direct execution of `hermes ...` itself, to route through Hermes commands instead of invoking tools directly.
  - **Why:** Direct non-Hermes process execution creates behavior drift, bypasses intended control surfaces, and weakens the Hermes-centered execution model.
  - **Pros:** Keeps orchestration aligned with the Hermes contract, centralizes execution policy, and reduces hidden shell integrations.
  - **Cons:** Increases coupling to Hermes command/skill coverage and may require refactors where code shells out to system tools.
  - **Context:** Examples include using `hermes cron ...` instead of `crontab`, and routing Claude Code invocation through Hermes-managed skill paths.
  - **Depends on:** none
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/hermes-process-routing`, Test Coverage `필요`, Security Review `불필요`

- [ ] **TODO-4: build a massive integration test project for Hermes, Kanban, and Claude Code** — End-to-end phase progression harness
  - **What:** Build an automated, step-by-step integration harness on a dedicated test project with mock TODOs, driving real phase progression across Hermes, Kanban, and Claude Code.
  - **Why:** Current behavior is hard to debug once Kanban and Claude Code interact, especially around blocking decisions and late-phase review transitions.
  - **Pros:** Provides deterministic reproduction for cross-system bugs, exposes status drift clearly, and creates a concrete debug surface for decision-gated phases.
  - **Cons:** Expensive to build/maintain and may require fixtures, logging hooks, and orchestration around blocking prompts.
  - **Context:** The harness should seed representative TODOs, progress each phase, and record status transitions plus stalls/mismatches across all three systems.
  - **Depends on:** `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P1`, Effort `L`, Phase `4 (Development)`, Branch `feature/massive-integration-test-project`, Test Coverage `필요`, Security Review `불필요`

- [ ] **TODO-5: selection-agent model lifecycle policy** — Pinned model + documented fallback ladder
  - **What:** Add a model-lifecycle policy in `.hermes/config.toml`: pinned `selection.model` (already shipping with TODO-2/3) plus `selection.model_fallback` ladder + alert behavior on Anthropic API deprecation (e.g., 404 on the pinned model id).
  - **Why:** TODO-2/3 hardcode `claude-opus-4-7` with no plan for the day Anthropic retires that model id. Without a documented fallback path, the first deprecation produces silent shadow-mode failures one morning.
  - **Pros:** Cheap insurance once the fallback mechanic is understood; aligns model handling with the prompt SHA pinning pattern from TODO-2/3; one-time decision.
  - **Cons:** Adds two config knobs; the fallback ladder needs revisiting as Anthropic's model lineup shifts. Designing cold is partial guesswork — better with one deprecation event of empirical data.
  - **Context:** Builds on TODO-2/3 once `config.py` and `decision/agent.py` exist. Today's design fails loudly on 404 (acceptable for v1). Revisit when Anthropic announces opus-4-7 EOL.
  - **Depends on:** `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P3`, Effort `S`, Phase `2 (Design)`, Branch `feature/selection-model-fallback`, Test Coverage `필요`, Security Review `불필요`

- [x] **TODO-6: route LLM queries through `hermes` instead of direct Claude calls** — Hermes as the only LLM surface
  - **Completed:** v0.3.0 (2026-06-15)
  - **What:** Remove any direct `claude`/Anthropic SDK invocations from the orchestrator and route all LLM queries through the `hermes` command.
  - **Why:** Direct Claude usage bypasses Hermes' control surface, breaking the Hermes-centered execution model and producing drift in prompt/model policy.
  - **Pros:** Centralizes LLM policy (model pinning, prompt SHA, fallback ladder) under Hermes; consistent with TODO-3's process-routing rule.
  - **Cons:** Requires auditing existing call sites and may need new Hermes subcommands where coverage is missing.
  - **Context:** Narrows TODO-3 specifically to LLM query paths (decision agent, selection agent, any ad-hoc Claude calls). Coordinates with TODO-5's model-lifecycle policy. Hermes CLI surfaces: primary path is `hermes chat -q "<prompt>" -Q -m <model> --source tool` (quiet, non-interactive; `--ignore-user-config`/`--ignore-rules`/`--safe-mode` for isolated CI/eval runs). Lower-effort migration path for existing Anthropic-SDK call sites is `hermes proxy start` (local OpenAI-compatible proxy) — just redirect `base_url`. `hermes model` sets default model+provider so `decision/agent.py` no longer hardcodes one. `hermes fallback` already implements the fallback ladder TODO-5 specifies — TODO-5 can collapse into "configure `hermes fallback`" rather than reinventing in `.hermes/config.toml`.
  - **Assumptions:** Hermes is correctly configured with a working model on the target machine, including external Claude invocation (auth via `hermes login` / `hermes auth`, model selectable via `hermes model`, end-to-end query via `hermes chat -q` returns a valid response). The orchestrator does not own Hermes provisioning — broken Hermes config is out of scope and surfaces as a `hermes chat` non-zero exit / stderr, not as logic this task must handle.

- [ ] **TODO-7: insert gstack `review` phase before `cso`** — Code-review pass with codex voice
  - **What:** Add a new phase between `phase_4_development` and `phase_6_1_cso` in `configs/phases.yaml` that runs the gstack `/review` (a.k.a. `code-review`) skill — with codex voice when applicable (`/code-review --voice codex` / `--codex`) — autofixes findings, and commits.
  - **Why:** Today the pipeline jumps straight from development into security (CSO). Functional/code-quality review is missing, so correctness bugs and reuse/simplification cleanups only get caught at human PR review (or not at all).
  - **Pros:** Catches correctness + reuse/simplification issues earlier in the pipeline, before CSO and before the human gate. codex voice gives a second-opinion review style. Mirrors how gstack's own `ship` flow expects a clean review before landing.
  - **Cons:** Adds turns/cost per TODO. Auto-fix can regress tests — phase prompt must require running tests after fixes and rolling back on failure.
  - **Context:** New phase key suggestion: `phase_5_review`. Prompt: "Run gstack `/code-review --codex` (or `--voice codex`); apply findings with `--fix`; run tests; commit with message `chore: address review findings`." Existing phases live in `configs/phases.yaml`. Routes through Hermes per [[TODO-6]] (`hermes chat -q "use code-review skill ..." -Q`).
  - **Assumptions:** gstack `code-review` skill is installed (see CLAUDE.md skills index — `/code-review` is listed); codex voice is supported by the current `/code-review` invocation; tests are runnable via `uv run pytest` from the repo root.
  - **Depends on:** `TODO-6`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/phase-5-review`, Test Coverage `필요`, Security Review `불필요`

- [ ] **TODO-8: replace `phase_8_finish_branch` with gstack `ship` — Kanban-gated merge to main, skip PR** — Ship straight to main via Kanban approval
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
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/ship-replaces-finish-branch`, Test Coverage `필요`, Security Review `필요`, Kanban Status Reuse `running (no new approved column)`, Approval Signal `runner-side awaiting_approval flag + Kanban running transition`
  - **Depends on:** `TODO-3`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `2 (Design)`, Branch `feature/hermes-llm-routing`, Test Coverage `필요`, Security Review `불필요`

