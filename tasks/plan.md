# Implementation Plan: Pluggable Pipeline Skill-Set Profiles

## Context

`hermes_pipeline` currently bundles exactly one phase composition
(`hermes_pipeline/data/phases.yaml`) whose prompts hardcode gstack +
superpowers skill invocations. SPEC.md ("Pluggable Pipeline Skill-Set
Profiles") asks for a `profile` field on `PipelineContract` so a project can
select between independent, self-contained phase lists — `gstack` (the
current default, renamed/relocated) and a new `agent-skills` profile — with
no shared phase-schema constraint between them. This plan breaks that spec
into vertical, independently-verifiable tasks.

On approval, this plan is saved to `tasks/plan.md` and the checklist to
`tasks/todo.md` (per the `agent-skills:planning-and-task-breakdown` skill
convention), then handed to `/build` or manual implementation.

## Key Findings From Exploration

- **Naming**: `contract.py` already has an unrelated `bundled_profile_dir()`
  helper for a "Hermes agent profile" (SOUL.md) concept at
  `data/profiles/pipeline/`. It never touches phase loading and won't
  collide at runtime with the new `profile` field, but add a clarifying
  docstring on the new field to prevent human confusion between "Hermes
  profile" (agent identity) and "pipeline profile" (skill-set ecosystem).
- **Schema version bump is justified**: `load_contract()` (contract.py:106-148)
  hard-fails on `schema_version != CONTRACT_SCHEMA_VERSION`. Bumping to 2 is
  the right call per the spec's Decisions section — signal the schema
  change explicitly rather than silently accepting stale versions. New
  contracts get `profile = "gstack"` by default (SPEC.md's backward-compat
  requirement), so no migration is needed for users who re-`init`, and
  `load_contract` continues to fail closed for real version drift.
- **Test breakage**: `load_phases()` (phases.py:23-30) resolves its default
  bundled path via `importlib.resources`. Once that literal moves from
  `data/phases.yaml` to `data/profiles/gstack/phases.yaml` inside
  `load_phases()` itself, `tests/test_phases.py` and
  `tests/test_phases_package_resolution.py` (which call `load_phases()`
  with no args) keep passing unmodified. `tests/test_contract.py` and any
  `tests/test_cli_contract.py` fixtures with hardcoded
  `schema_version = 1` TOML strings need updating to `schema_version = 2`
  (plus a new test asserting v1-without-`profile` still round-trips through
  a clear `ContractVersionMismatchError`, since the spec chose fail-closed
  over silent v1 acceptance — resolved below).
- **Open question resolution**: `doctor` should use the same fail-closed
  pattern as its other contract-error paths (`ContractSchemaError` etc.) —
  a malformed profile `phases.yaml` should print an `INVALID:` message and
  exit 2, not warn-and-continue.
- **Version bump semantics**: Since `load_contract` fails closed on any
  version mismatch today (no partial-migration precedent in this codebase),
  keep that behavior — v1 contracts (`schema_version = 1`, no `profile`
  field) raise `ContractVersionMismatchError` same as any other version
  drift. This matches existing project convention (CLAUDE.md's
  "version-mismatch-fails-closed" note) and keeps `load_contract` simple:
  no dual-version branch logic. Existing users re-run `pipeline-watch init`
  to regenerate a v2 contract — acceptable since `profile` defaults to
  `"gstack"` and `init` is idempotent/non-destructive already.

## Task List

### Phase 1: Contract Schema + Phase Resolution (foundation)

**Task 1: Add `profile` field, bump `CONTRACT_SCHEMA_VERSION` to 2**
- Files: `hermes_pipeline/contract.py`
- Add `profile: str = "gstack"` to `PipelineContract` (with clarifying
  comment distinguishing from `bundled_profile_dir()`'s unrelated concept).
- Bump `CONTRACT_SCHEMA_VERSION` 1 → 2.
- Update `_render_contract_toml()` / `_render_default_contract_toml()` to
  emit `profile = "..."`.
- `load_contract()` keeps its existing fail-closed version check unchanged
  (no v1-acceptance branch — see resolution above); reads `profile` from
  TOML with `.get("profile", "gstack")` for defensive parsing of hand-edited
  files that are already schema_version=2 but omit profile.
- Acceptance criteria:
  - [ ] `PipelineContract(...).profile == "gstack"` by default
  - [ ] Writing then loading a contract round-trips `profile`
  - [ ] A `schema_version = 1` file raises `ContractVersionMismatchError`
- Verification: `uv run pytest tests/test_contract.py -v`
- Dependencies: None

**Task 2: `resolve_profile_phases_path()` helper**
- Files: `hermes_pipeline/phases.py`
- Add `resolve_profile_phases_path(profile: str) -> Path`, resolving
  `data/profiles/<profile>/phases.yaml` via `importlib.resources`; raise
  `ContractSchemaError` listing available profiles (sorted dir names under
  `data/profiles/`) when the target file doesn't exist.
- Update `load_phases()`'s default-path branch to call
  `resolve_profile_phases_path("gstack")` instead of the old literal
  `data/phases.yaml` path.
- Acceptance criteria:
  - [ ] `resolve_profile_phases_path("gstack")` and `("agent-skills")`
        both resolve once Task 3 files exist
  - [ ] Unknown profile raises `ContractSchemaError` naming valid profiles
  - [ ] `load_phases()` (no args) still returns the gstack phase list
- Verification: `uv run pytest tests/test_phases.py tests/test_phases_package_resolution.py -v`
- Dependencies: Task 1 (imports `ContractSchemaError`)

### Checkpoint A: Foundation
- [ ] `uv run pytest tests/test_contract.py tests/test_phases.py -v` passes
      (some tests still red until Task 3 supplies the moved/new YAML files —
      acceptable at this checkpoint; note which are expected-red)

### Phase 2: Bundled Phase Files (vertical: one working profile at a time)

**Task 3: Move `gstack` phases + swap `phase_8_finish_branch` prompt**
- Files: `hermes_pipeline/data/profiles/gstack/phases.yaml` (new, moved
  content), delete `hermes_pipeline/data/phases.yaml`
- Move the 9 existing phases verbatim; change only
  `phase_8_finish_branch`'s prompt from superpowers
  `finishing-a-development-branch` to the gstack `/ship` skill (open PR,
  HALT — same contract).
- Acceptance criteria:
  - [ ] `load_phases(resolve_profile_phases_path("gstack"))` returns the
        same 9 phase_keys in the same order as before
  - [ ] `phase_8_finish_branch.prompt` references `/ship`, not
        `finishing-a-development-branch`
  - [ ] Old `hermes_pipeline/data/phases.yaml` no longer exists
- Verification: `uv run pytest tests/test_phases.py -v` (all green now)
- Dependencies: Task 2

**Task 4: Author `agent-skills` phases.yaml**
- Files: `hermes_pipeline/data/profiles/agent-skills/phases.yaml` (new)
- 8 phases per SPEC.md's mapping table: `phase_1_spec` (gate:
  `phase_1b_spec_gate`), `phase_2_plan`, `phase_3_implement`,
  `phase_4_review`, `phase_5_security`, `phase_6_document_release`
  (verbatim copy of gstack's doc-release prompt), `phase_7_ship`,
  `phase_8_ship` (gate, terminal).
- Acceptance criteria:
  - [ ] `load_phases(resolve_profile_phases_path("agent-skills"))` returns
        8 phases in the order above
  - [ ] `phase_1b_spec_gate` and `phase_8_ship` have `gate: true`;
        `phase_8_ship` also `terminal: true`
  - [ ] Non-gate phases reference the correct `agent-skills:*` skill name
        from the mapping table
- Verification: new unit tests in `tests/test_phases.py` mirroring the
  existing `test_real_phases_yaml_*` pattern, scoped to the agent-skills file
- Dependencies: Task 2

### Checkpoint B: Both Profiles Loadable
- [ ] `uv run pytest tests/test_phases.py -v` fully green
- [ ] Manual: `uv run python -c "from hermes_pipeline.phases import resolve_profile_phases_path, load_phases; print(len(load_phases(resolve_profile_phases_path('gstack')))); print(len(load_phases(resolve_profile_phases_path('agent-skills'))))"` prints `9` then `8`

### Phase 3: CLI Surface

**Task 5: `pipeline-watch init --profile`**
- Files: `hermes_pipeline/cli.py`
- Add `--profile` arg (default `"gstack"`) to the `init` subparser.
- Thread `profile` through `write_default_contract()` /
  `_render_default_contract_toml()` (or patch post-write, matching the
  existing `--assignee` patch pattern at `_cmd_init` ~1339-1349) so
  capabilities are computed from the *selected* profile's phases, not
  always gstack's.
- Validate the profile name via `resolve_profile_phases_path()` before
  writing, so a typo fails before any file is created.
- Acceptance criteria:
  - [ ] `pipeline-watch init <project>` (no flag) writes `profile = "gstack"`
  - [ ] `pipeline-watch init <project> --profile agent-skills` writes
        `profile = "agent-skills"` and computes capabilities from the
        agent-skills phase list
  - [ ] `pipeline-watch init <project> --profile bogus` fails before
        writing any contract file, with the valid-profiles error message
- Verification: `uv run pytest tests/test_cli_contract.py -v` (extend or add
  a test module if it doesn't already cover `init`)
- Dependencies: Tasks 1, 2, 3, 4

**Task 6: `pipeline-watch doctor` profile-aware capability check**
- Files: `hermes_pipeline/cli.py`
- Load `phases = load_phases(resolve_profile_phases_path(contract.profile))`
  instead of the unconditional bundled default.
- Wrap the resolve+load in a try/except catching `ContractSchemaError` (bad
  profile name) and YAML parse errors, printing an `INVALID:` message and
  returning exit code 2 in both cases (resolves SPEC.md's Open Question —
  same fail-closed pattern as other contract errors).
- Include `profile=<name>` in the existing "OK:" summary line.
- Acceptance criteria:
  - [ ] `doctor` on a gstack-profile project checks against gstack's
        required capabilities (today's behavior, unchanged)
  - [ ] `doctor` on an agent-skills-profile project checks against
        agent-skills' required capabilities
  - [ ] `doctor` on a project with an unresolvable/malformed profile prints
        `INVALID: ...` and exits 2
- Verification: `uv run pytest tests/test_cli_contract.py -v`
- Dependencies: Task 5

### Checkpoint C: End-to-End CLI
- [ ] Manual: `uv run pipeline-watch init <tmp-project> --profile agent-skills && uv run pipeline-watch doctor <tmp-project>` exits 0, contract shows `profile = "agent-skills"`, `schema_version = 2`
- [ ] `uv run pytest` (full suite) passes

### Phase 4: Docs

**Task 7: Update contract docs, add agent-skills how-to**
- Files: `docs/howto-pipeline-contract.md` (document `profile` field + both
  values), `docs/explanation-pipeline-contract.md` (if present — note
  independent-profile design rationale), new
  `docs/howto-agent-skills-profile.md` (setup + phase-by-phase summary +
  gstack comparison table, reusing SPEC.md's mapping table).
- Acceptance criteria:
  - [ ] `docs/howto-pipeline-contract.md` shows a `profile = "agent-skills"`
        example and links to the new how-to
  - [ ] `docs/howto-agent-skills-profile.md` walks through `init --profile
        agent-skills` → `doctor` → first tick
- Verification: manual read-through; no automated check
- Dependencies: Task 6

### Checkpoint D: Complete
- [ ] All SPEC.md Success Criteria checked off
- [ ] `uv run pytest` and `uv run ruff check hermes_pipeline/ tests/` both clean
- [ ] Docs committed alongside code per this repo's `docs/gstack/**`
      finalize-on-approval convention (not applicable here — these are
      `docs/*.md` how-tos, not gstack planning docs, so normal commit rules
      apply)

## Risks

| Risk | Mitigation |
|---|---|
| Circular import between `contract.py` (needs `ContractSchemaError`... already local) and `phases.py` (needs it too) | `ContractSchemaError` stays defined in `contract.py`; `phases.py` imports it — confirm no reverse import of `phases` symbols into `contract.py` beyond the existing `load_phases()` call, which is already how `_render_default_contract_toml()` works today |
| `agent-skills` phase prompts drift from what those skills actually expect (skill names/behavior change upstream) | Out of scope per SPEC.md — no runtime skill-availability check; call out in the how-to doc that prompts are best-effort against skill names current as of this spec |
| Deleting `hermes_pipeline/data/phases.yaml` breaks any external caller passing that literal path | Grep confirmed no literal path references outside `importlib.resources` calls — safe |

## Verification Summary

```
uv run pytest tests/test_contract.py tests/test_phases.py -v
uv run pytest tests/test_cli_contract.py -v   # or wherever init/doctor CLI tests live
uv run pytest                                  # full suite
uv run ruff check hermes_pipeline/ tests/
uv run pipeline-watch init <tmp-project> --profile agent-skills
uv run pipeline-watch doctor <tmp-project>
```
