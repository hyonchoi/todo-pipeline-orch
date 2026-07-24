# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Spec:**, **Reference:**
> - **Spec:**/**Reference:** are `--revise`-only (never suggested by `--add` or auto-research); always typed verbatim
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

## Harness

- [ ] **TODO-4: build a massive integration test project for Hermes, Kanban, and Claude Code** — End-to-end phase progression harness
  - **What:** Build an automated, step-by-step integration harness on a dedicated test project with mock TODOs, driving real phase progression across Hermes, Kanban, and Claude Code.
  - **Why:** Current behavior is hard to debug once Kanban and Claude Code interact, especially around blocking decisions and late-phase review transitions.
  - **Pros:** Provides deterministic reproduction for cross-system bugs, exposes status drift clearly, and creates a concrete debug surface for decision-gated phases.
  - **Cons:** Expensive to build/maintain and may require fixtures, logging hooks, and orchestration around blocking prompts.
  - **Context:** The harness should seed representative TODOs, progress each phase, and record status transitions plus stalls/mismatches across all three systems.
  - **Depends on:** `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P1`, Effort `L`, Phase `4 (Development)`, Branch `feature/massive-integration-test-project`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-5: selection-agent model lifecycle policy** — Pinned model + documented fallback ladder
  - **What:** Add a model-lifecycle policy in `.hermes/config.toml`: pinned `selection.model` (already shipping with TODO-2/3) plus `selection.model_fallback` ladder + alert behavior on Anthropic API deprecation (e.g., 404 on the pinned model id).
  - **Why:** TODO-2/3 hardcode `claude-opus-4-7` with no plan for the day Anthropic retires that model id. Without a documented fallback path, the first deprecation produces silent shadow-mode failures one morning.
  - **Pros:** Cheap insurance once the fallback mechanic is understood; aligns model handling with the prompt SHA pinning pattern from TODO-2/3; one-time decision.
  - **Cons:** Adds two config knobs; the fallback ladder needs revisiting as Anthropic's model lineup shifts. Designing cold is partial guesswork — better with one deprecation event of empirical data.
  - **Context:** Builds on TODO-2/3 once `config.py` and `decision/agent.py` exist. Today's design fails loudly on 404 (acceptable for v1). Revisit when Anthropic announces opus-4-7 EOL.
  - **Depends on:** `TODO-2`, `TODO-3`
  - **Decisions:** Priority `P3`, Effort `S`, Phase `2 (Design)`, Branch `feature/selection-model-fallback`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-23: Harden kanban-as-scheduler edge cases in harness.py** — Fix timeout/hang gaps found in TODO-20 adversarial review
  - **What:** Address remaining edge cases in `_poll_kanban_phases`/`_run_with_timeout` surfaced by Codex and Claude adversarial review of TODO-20: (1) daemon polling thread stays alive after `_run_with_timeout` times out in `--kanban hermes` mode, with no stop/archive/cancel of registered kanban tasks before temp project cleanup; (2) `--phase <gate> --kanban hermes` creates a single blocked gate task with no predecessor entry, hanging until overall timeout; (3) unrecognized/unknown kanban status value causes silent infinite poll loop; (4) `ConvergenceHaltError` may be masked by a simultaneous worker-join timeout race; (5) `_auto_complete_gate_tasks` idempotency under status flapping is unverified, now more relevant since a fix in TODO-20 broadened which transitions call it.
  - **Why:** These are known correctness/robustness gaps in the kanban-as-scheduler harness path, identified but out of scope for TODO-20's core `--kanban` flag delivery. Left unaddressed, they can cause silent hangs or leaked background threads in `--kanban hermes` test runs.
  - **Depends on:** `TODO-20`
  - **Decisions:** Priority `P2`, Effort `M`, Phase `4 (Development)`, Branch `feature/harden-kanban-scheduler-edge-cases`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-28: Conditional kanban-task registration for optional pipeline phases** — register_todo_phases should skip creating a kanban task entirely when a phase's applicability signal (e.g. Security Review: not-required) says it doesn't apply, instead of dispatching a no-op task.
  - **What:** Add conditional registration to `register_todo_phases` (`kanban_tasks.py`): before creating a kanban task for an optional phase (e.g. `phase_6_1_cso`, and any future QA phase from TODO-24/29), check the TODO's applicability signal (TODOS.md `Decisions:` field, e.g. `Security Review: required/not-required`) and skip task creation entirely when not applicable — rather than the current TODO-24 interim fix (approach A) where the task is always created and the subagent self-checks and exits 0 as a no-op. Requires handling the `--parent` chain gap: the next real phase's `--parent` must point to the last *actually created* task, not literally `task_ids[phase_idx - 1]`.
  - **Why:** Approach A (in-prompt self-check) still creates and dispatches a kanban task every time, wasting a full phase-execution cycle (subprocess spawn, turn budget) even when the phase is a guaranteed no-op. Skipping registration entirely is more correct and avoids the wasted dispatch, but is a real code change to `kanban_tasks.py`'s task-creation loop and `--parent` chaining logic — out of scope for TODO-24 ("revised phases.yaml... not a full pipeline rewrite").
  - **Context:** Surfaced during TODO-24 phase_6_1_cso review — TODO-24 ships approach A (in-prompt guard) as the interim fix; this TODO is the proper follow-up.
  - **Depends on:** `TODO-24`
  - **Decisions:** Priority `P3`, Effort `M`, Phase `2 (Design)`, Branch `feature/conditional-phase-registration`, Test Coverage `required`, Security Review `not-required`

- [ ] **TODO-33: Rename main CLI script to `tpo`** — Replace `pipeline-watch`/`hermes-pipeline` console-scripts with a single short `tpo` entry point
  - **What:** Replace both `pipeline-watch` and `hermes-pipeline` console-script entries in `pyproject.toml`'s `[project.scripts]` with a single `tpo` entry point (`hermes_pipeline.cli:main`), then update all references across docs (10+ files under `docs/`), tests (test_contract.py, test_recover_counter_cli.py, test_cli_entrypoint.py, test_tick_subcommand.py, test_cli.py), README, and CHANGELOG to use `tpo`.
  - **Why:** The current script names (`pipeline-watch`, `hermes-pipeline`) are too long to type repeatedly during manual/interactive CLI usage; a single short name (`tpo`) improves day-to-day ergonomics.
  - **Pros:** Much faster to type; single unambiguous CLI name going forward.
  - **Cons:** Full breakage of old names — every doc, test, and any external script/muscle-memory using `pipeline-watch`/`hermes-pipeline` must be updated in the same change.
  - **Context:** `pyproject.toml` `[project.scripts]` (lines 11-12); docs: howto-agent-skills-profile.md, howto-pipeline-contract.md, howto-approve-and-ship.md, howto-multi-project-setup.md, + gstack design docs; tests: test_contract.py, test_recover_counter_cli.py, test_cli_entrypoint.py, test_tick_subcommand.py, test_cli.py.
  - **Decisions:** Priority `P2`, Effort `L`, Phase `4 (Development)`, Branch `feature/rename-cli-to-tpo`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`

- [ ] **TODO-34: Embed todos-manager skill as package data with `tpo skills install` subcommand** — Replace git-clone-dependent install script with an in-package installer usable after `uv tool install`
  - **What:** Move `skills/todos-manager/` into `hermes_pipeline/skills/todos-manager/`, mark it as package data in `pyproject.toml` (hatch wheel force-include or similar), and add a `tpo skills install` CLI subcommand (in `cli.py`, alongside `_cmd_install_profile` at cli.py:1070) that uses `importlib.resources.files("hermes_pipeline") / "skills/todos-manager"` to locate the packaged skill and symlinks/copies it into `~/.claude/skills/` and `~/.agents/skills/`, porting the logic currently in `scripts/install-todos-manager.sh`.
  - **Why:** `scripts/install-todos-manager.sh` requires a git clone of the repo to run, which is incompatible with the `uv tool install todo-pipeline-orchestrator` distribution model where users never see the source tree.
  - **Pros:** No git dependency; single source of truth versioned with the package; works for any `uv tool install` user; testable via pytest like other CLI subcommands.
  - **Cons:** One-time migration effort — pyproject.toml package-data wiring, porting bash symlink logic to Python, deleting/deprecating the old script.
  - **Context:** Existing subcommand pattern to follow: `_cmd_install_profile` (cli.py:1070) and its parser registration (cli.py:289). Old install path: `scripts/install-todos-manager.sh`.
  - **Depends on:** `TODO-33`
  - **Assumptions:** hatchling build backend (already in use) supports package-data inclusion for non-Python files via `[tool.hatch.build.targets.wheel]` config.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Branch `feature/embed-todos-manager-skill-install`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`

- [ ] **TODO-32: Separate `data/profiles` into identity and phase-config contexts** — Split mixed Hermes identity profile and pipeline phase definitions into distinct directories
  - **What:** Separate `hermes_pipeline/data/profiles` into two distinct directories (or restructure) — one for Hermes' identity/profile data (`pipeline/SOUL.md`) and another for pipeline phase configurations (`gstack/phases.yaml`, `agent-skills/phases.yaml`). The `pipeline/` subdirectory contains persona/identity data, while `gstack/` and `agent-skills/` contain pipeline orchestration metadata. These two contexts currently share a `profiles` namespace but represent completely different domains.
  - **Why:** The `data/profiles` directory mixes two unrelated contexts: Hermes' core identity profile (`pipeline/SOUL.md`) and phase definition configs for different pipelines (`gstack/phases.yaml`, `agent-skills/phases.yaml`). They share a `profiles` namespace but represent completely different domains — one is persona/identity data, the other is pipeline orchestration metadata. This conflation makes it easy to accidentally load the wrong type of data and obscures the semantic distinction between "who Hermes is" and "how pipelines run."
  - **Pros:** Clearer directory semantics reduces risk of loading wrong data type; each domain can evolve its own file structure independently; easier to reason about which load paths access identity vs phase config
  - **Cons:** Requires updating all import/load paths that reference `data/profiles/`; breaks any external tooling that assumes the current layout; migration touches multiple files
  - **Context:** Current layout: `data/profiles/pipeline/SOUL.md` (identity), `data/profiles/gstack/phases.yaml` (gstack phases), `data/profiles/agent-skills/phases.yaml` (agent-skills phases)
  - **Assumptions:** The codebase loads these paths by relative routing (`data/profiles/pipeline/`, `data/profiles/gstack/`, `data/profiles/agent-skills/`), so any restructure requires updating import/load paths.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Branch `feature/separate-profiles-data`, Test Coverage `required`, Security Review `not-required`
