# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**, **Spec:**, **Reference:**
> - **Spec:**/**Reference:** are `--revise`-only (never suggested by `--add` or auto-research); always typed verbatim
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

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

- [x] **TODO-32: Separate `data/profiles` into identity and phase-config contexts** — Split mixed Hermes identity profile and pipeline phase definitions into distinct directories
  - **What:** Separated `hermes_pipeline/data/profiles` into `hermes_pipeline/data/hermes-identity/pipeline/` (SOUL.md) and `hermes_pipeline/data/phase-profiles/` (gstack/phases.yaml, agent-skills/phases.yaml). Updated `bundled_profile_dir()` (contract.py) and `resolve_profile_phases_path()` (phases.py) call sites, added `tests/test_profile_layout_split.py` regression coverage, and updated docs/howto-agent-skills-profile.md and docs/howto-pipeline-contract.md.
  - **Completed:** v0.5.9 (2026-07-24)

- [ ] **TODO-35: Add --reinstall flag, default-on-exists fail, and uninstall subcommand to skills install CLI** — Make `tpo skills install` fail when dest exists, add explicit `--reinstall` opt-in, and create `tpo skills uninstall` with confirmation
  - **What:** Add --reinstall flag to `tpo skills install` (removes existing dest before copying), make default install fail when dest exists, and add `tpo skills uninstall` subcommand with confirmation prompt.
  - **Why:** `shutil.copytree(dest, dirs_exist_ok=True)` fails on structural mismatches (file vs dir conflicts) and silently overwrites. Users need explicit opt-in to reinstall and a way to remove skills.
  - **Pros:** Prevents accidental silent overwrites; gives users clean reinstall path; adds symmetry with uninstall
  - **Cons:** Breaking change — existing scripts/aliases that double-run install without --reinstall will get errors
  - **Context:** CLI in hermes_pipeline/cli.py:1222-1265, tests in tests/test_skills_install.py
  - **Decisions:** Priority `P2`, Effort `S`, Phase `4 (Development)`, Branch `feat/skills-install-reinstall`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`

- [ ] **TODO-36: Reorganize and refresh README.md's docs table** — Fix broken link, remove stale CLI-name references, group the 30-entry doc table by subsystem, and rewrite Getting Started for real install paths
  - **What:** Restructure the flat 30-row "Documentation" table in README.md (README.md:37-75) into subsystem-grouped sections (pipeline core, multi-project setup, pipeline contract, todos-manager, skill test harness) instead of one undifferentiated table. Fix the broken link `[Install TODOS Manager](tpo-skills-install)` (README.md:63) to point at a real doc/section. Update `docs/pipeline-modularization-plan.md`, which still references the pre-rename `pipeline-watch`/`hermes-pipeline` CLI names instead of `tpo`. Rewrite the Getting Started / Installation section (README.md:103-138) to reflect the actual install and onboarding paths, currently undocumented or misleading:
    1. Document `uv tool install` as the primary install method — the CLI is invoked directly as `tpo ...`, not `uv run tpo ...` (README's current "Run"/"CLI Commands" section (README.md:113-155) exclusively shows `uv run tpo <cmd>`, which only applies to running from a source checkout, not the packaged/installed CLI).
    2. Add an explicit "starting a project from scratch" path (`tpo init <project>` on a project with no TODOS.md yet, tying into `todos-manager --init`).
    3. Add an explicit "adopting tpo on an existing project" path — call out that `todos-manager --convert` (or `--revise` for individual entries) is required to bring a pre-existing/hand-written TODOS.md into the enforced schema before `tpo tick` can select from it.
  - **Why:** The docs table has grown organically to 30 links in one flat list with no grouping beyond a `Quadrant` column, making it hard to find the right doc. One link is broken (points at a CLI command name, not a path). `pipeline-modularization-plan.md` was missed during the `tpo` CLI rename (commits `5fbc837`..`e6aab3a`), leaving stale command names in a doc new contributors are pointed to first. Separately, the Getting Started flow only demonstrates `uv run tpo ...` from a source checkout and never mentions `uv tool install` (the actual distribution model per TODO-34's context) or the two distinct onboarding paths — new project vs. existing project with a pre-existing TODOS.md needing `--convert`/`--revise` — leaving new users without a clear starting point for either case.
  - **Pros:** Easier onboarding via a scannable, grouped doc index; no dead links; consistent CLI naming across all docs; new users get a correct install command and a clear fork for "new project" vs. "existing project" onboarding.
  - **Cons:** Touches a widely-linked file (README.md); requires re-verifying every link after restructuring to avoid introducing new breaks; Getting Started rewrite needs to stay in sync with `docs/tutorial-getting-started.md`, which may itself need the same `uv tool install` correction.
  - **Context:** README.md Documentation table (lines 37-75) and Getting Started / Run sections (lines 103-155); stale references in docs/pipeline-modularization-plan.md; CLI rename history in TODO-33 (already-committed `tpo` rename) and TODO-34/35 (skills install follow-ons); `todos-manager --convert`/`--revise` subcommands (docs/howto-todos-manager.md) as the existing-project onboarding mechanism.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `4 (Development)`, Branch `docs/reorganize-readme-docs-table`, Test Coverage `not-required`, Security Review `not-required`, UI Review `not-required`

- [x] **TODO-37: Add global config file with `tpo config` command, deprecating PIPELINE_PROJECTS_DIR** — Introduce an XDG-style global config.yaml as the source for projects_dir and other base settings, with a `tpo config` subcommand to read/write it.
  - **What:** Add a global config file, resolved via a search order — `${XDG_CONFIG_HOME:-~/.config}/tpo/config.yaml`, else `~/.tpo/config.yaml`, else `${HERMES_HOME:-~/.hermes}/tpo.yaml` (first existing file wins; default to the first path if none exist) — loaded in `Config.from_env()`/`hermes_pipeline/config.py` before env var overrides are applied. Add `tpo config get <key>`, `tpo config set <key> <value>`, and `tpo config path` subcommands (mirroring the `skills` subparser pattern at cli.py:329) to read/write the YAML file. `PIPELINE_PROJECTS_DIR` env var support remains as a deprecated compatibility alias; users should prefer setting `projects_dir` via the global config file.
  - **Why:** `PIPELINE_PROJECTS_DIR` is the only base-level setting not covered by the existing per-project overlays, forcing users into ad hoc shell exports for a value that's effectively static per machine. A discoverable, persistent global config file with a `tpo config` command gives users a standard place to set install-wide defaults, consistent with the XDG convention already implied by `~/.hermes` state dir usage.
  - **Pros:** Persistent config survives shell restarts; XDG-compliant discovery order matches conventions of other CLI tools; `tpo config` subcommand makes the setting discoverable/scriptable instead of requiring users to know an undocumented env var.
  - **Cons:** Keeping PIPELINE_PROJECTS_DIR as a deprecated alias avoids a patch-release break, but leaves two ways to configure `projects_dir` until the alias is removed in a later major/minor release.
  - **Context:** hermes_pipeline/config.py (Config.from_env, env_map at line 35-42); existing per-project overlay pattern at config.py:105 (load_toml_overlay), loaded from .hermes/config.toml per cli.py:409-425 (selection/circuit_breaker settings) — distinct from .hermes/pipeline.toml (contract.py:20, phase/profile contract) and .hermes/project.toml (project_config.py:14, enabled/notifications settings); skills subcommand pattern at cli.py:329-335 for the `tpo config` subparser structure.
  - **Decisions:** Priority `P2`, Effort `M`, Phase `2 (Design)`, Branch `feature/global-config-file`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
  - **Completed:** v0.6.1 (2026-07-26)

- [ ] **TODO-38: Track NEXT_TODO_ID in TODOS.md instead of scanning archive** — Track NEXT_TODO_ID in TODOS.md instead of scanning archive
  - **What:** Add a `NEXT_TODO_ID: <n>` line to TODOS.md's preamble as the primary source for `--add`'s ID computation, replacing the per-add archive scan. Add a conflict check: before assigning NEXT_TODO_ID to a new entry, verify no existing TODO-<NEXT_TODO_ID> already exists in TODOS.md (e.g. from a manual edit or merge). If a conflict is detected, automatically run the `--audit` reconciliation scan (max ID across TODOS.md + TODOS-archive.md) to recompute and correct NEXT_TODO_ID in place, log the correction, then continue the `--add` flow with the corrected ID — no user interruption needed. `--audit` (invoked standalone) performs the same full-scan reconciliation. Out of scope: deprecating `.hermes/todo_id_counter` itself (separate cleanup).
  - **Why:** Fixes the ID-assignment mechanism discussed earlier this session: `.hermes/todo_id_counter` is gitignored, so any agent that forgets to update it causes wrong TODO-<n> picks; committing NEXT_TODO_ID directly in tracked TODOS.md removes that failure mode and the archive-scan cost.
  - **Pros:** O(1) next-ID lookup instead of archive scan in the common case; self-healing on drift (no silent bad ID assignment); state lives in a tracked file so it can't silently diverge across clones/worktrees/agents.
  - **Cons:** Requires a one-time migration to backfill NEXT_TODO_ID into existing TODOS.md files; conflict-triggered auto-audit adds a fallback archive scan back on the rare drift path (acceptable since it's no longer the common path).
  - **Decisions:** Priority `P2`, Effort `S`, Phase `2 (Design)`, Branch `feature/todos-next-id-tracking`, Test Coverage `required`, Security Review `not-required`, UI Review `not-required`
