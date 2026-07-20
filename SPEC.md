# Spec: Pluggable Pipeline Skill-Set Profiles

## Objective

Today `hermes_pipeline` has exactly one phase composition, bundled at
`hermes_pipeline/data/phases.yaml`, whose prompts hardcode gstack + superpowers
skill invocations (`autoplan`, `writing-plans`, `subagent-driven-development`,
gstack `/review`, `cso`). A project cannot run the pipeline against a
different skill ecosystem — e.g. the `agent-skills` plugin
(`spec-driven-development`, `incremental-implementation`,
`test-driven-development`, `agent-skills:code-reviewer`,
`agent-skills:security-auditor`) — without hand-editing the bundled YAML in
place, which drifts on every `hermes_pipeline` upgrade.

Users of this pipeline: solo devs / small teams running `pipeline-watch tick`
unattended, who want to choose *which* skill ecosystem backs each phase per
project, without forking the package.

Success: a project can declare `profile = "agent-skills"` (or `"gstack"`, the
current default) in its execution contract, and `pipeline-watch` loads a
completely independent, self-contained phase list for that profile —
different phase count, names, order, and prompts allowed — with no shared
phase-schema constraint between profiles.

## Decisions (pre-resolved)

- **Independent profiles, not shared-skeleton-swap-skills.** Each profile is
  its own full `phases.yaml`-shaped file. The `agent-skills` profile is not
  required to have a `phase_6_1_cso` equivalent, or the same phase count, as
  `gstack`. This was chosen over "one canonical phase list, swap skill per
  slot" because the two ecosystems don't decompose into equivalent stages
  1:1 (e.g. gstack's `autoplan` bundles CEO/Eng/UI/DX review into one phase;
  `agent-skills:spec-driven-development` is a four-gate Specify→Plan→Tasks→Implement
  flow that doesn't map onto a single phase).
- **Config surface: `.hermes/pipeline.toml`.** Add a `profile` field to
  `PipelineContract` (`hermes_pipeline/contract.py`). Bundled profiles ship
  inside the package under `hermes_pipeline/data/profiles/<name>/phases.yaml`;
  `profile` selects which bundled directory `load_phases()` reads from. This
  reuses the existing `load_phases(config_path: Path | str | None)` override
  parameter (`hermes_pipeline/phases.py:23`) — no new phase-loading mechanism
  needed, just a new resolution step from `profile` name to path.
- **No runtime skill-availability check.** The pipeline does not verify the
  named skills/plugins are actually installed in the executing Hermes
  profile before a tick starts — same as today, where gstack skill
  availability isn't pre-flighted either. A phase whose skill isn't
  installed fails at execution time with whatever error `hermes chat -q`
  surfaces; this is out of scope for this spec.
- **`gstack` profile is a rename, plus one prompt swap.** The current
  `hermes_pipeline/data/phases.yaml` becomes
  `hermes_pipeline/data/profiles/gstack/phases.yaml`, unchanged except
  `phase_8_finish_branch`'s prompt switches from superpowers
  `finishing-a-development-branch` to the gstack `/ship` skill (open PR and
  HALT, same contract as before). This is the default profile when `profile`
  is unset in an existing project's contract — read: purely additive, zero
  contract-schema migration required for current users, but this one prompt
  changes for everyone on the `gstack` profile.

## Scope

In scope:
- `hermes_pipeline/contract.py` — add `profile: str = "gstack"` field to
  `PipelineContract`, schema version bump, TOML read/write/render updates.
- `hermes_pipeline/phases.py` — add a `resolve_profile_phases_path(profile: str) -> Path`
  helper that maps a profile name to its bundled `phases.yaml` path, raising
  a clear error for an unknown profile name.
- `hermes_pipeline/data/profiles/gstack/phases.yaml` — move of the existing
  bundled file, with `phase_8_finish_branch`'s prompt updated to use `/ship`
  instead of `finishing-a-development-branch` (see phase mapping below).
- `hermes_pipeline/data/profiles/agent-skills/phases.yaml` — new phase list,
  authored per the mapping below.
- `hermes_pipeline/cli.py` — `init` gains a `--profile` flag; `doctor`
  resolves capabilities against the project's configured profile, not always
  the bundled default.
- Docs: `docs/howto-pipeline-contract.md`, `docs/explanation-pipeline-contract.md`
  updated for the new field; new `docs/howto-agent-skills-profile.md`.

Out of scope:
- Per-phase skill override within a single profile (rejected — see Decisions).
- Any change to `hermes_pipeline/harness.py` phase-polling/gate logic — this
  spec only changes *which phases.yaml is loaded*, not how phases execute.
- Skill-availability pre-flight / doctor checks for whether the named
  agent-skills plugin skills are installed (out of scope, see Decisions).
- Migrating existing projects — `profile` defaults to `"gstack"`, matching
  current behavior with zero contract changes required.

## Agent-skills profile phase mapping

| Bundled gstack phase | agent-skills equivalent | Notes |
|---|---|---|
| `phase_2_autoplan` (CEO/Eng/UI/DX review, plan docs, branch) | `phase_1_spec` — `agent-skills:spec-driven-development` Specify phase | Produces `SPEC.md` instead of `docs/pipeline/{todo_id}-plan.md`; branch creation stays in the prompt. |
| `phase_2b_plan_gate` (gate) | `phase_1b_spec_gate` (gate) | Same gate mechanic, human approves the spec instead of the plan. |
| `phase_3_writing_plan` | `phase_2_plan` — `agent-skills:planning-and-task-breakdown` (Plan+Tasks phases per SKILL.md) | Writes `tasks/plan.md` + `tasks/todo.md` per that skill's stated output convention, not `docs/pipeline/{todo_id}-impl-plan.md`. |
| `phase_4_development` | `phase_3_implement` — `agent-skills:incremental-implementation` + `agent-skills:test-driven-development` | Per spec-driven-development's own Phase 4 guidance. |
| `phase_5_review` | `phase_4_review` — `agent-skills:code-review-and-quality` (or the `agent-skills:code-reviewer` subagent) | Same autonomous-mode framing (no user input, apply fixes directly) as current phase_5 prompt. |
| `phase_6_1_cso` | `phase_5_security` — `agent-skills:security-and-hardening` (or `agent-skills:security-auditor` subagent) | |
| `phase_7_document_release` | `phase_6_document_release` | Same prompt, ecosystem-agnostic (CHANGELOG/README updates) — copied verbatim. |
| `phase_8_finish_branch` | `phase_7_ship` | Both profiles switch from superpowers `finishing-a-development-branch` to the `ship` skill (gstack `/ship`; agent-skills `agent-skills:ship`) — open PR and HALT, same as before. Requires updating the bundled `gstack/phases.yaml` prompt too (not just adding the agent-skills equivalent). |
| `phase_9_ship` (gate, terminal) | `phase_8_ship` (gate, terminal) | Same mechanic. |

## Commands

```
Test:  uv run pytest tests/test_contract.py tests/test_phases.py -v
Lint:  uv run ruff check hermes_pipeline/ tests/
Full test suite: uv run pytest
Manual: uv run pipeline-watch init <project> --profile agent-skills
        uv run pipeline-watch doctor <project>
```

## Project Structure

```
hermes_pipeline/contract.py                          → add `profile` field + schema version bump
hermes_pipeline/phases.py                             → add resolve_profile_phases_path()
hermes_pipeline/cli.py                                → `init --profile`, `doctor` profile-aware capability check
hermes_pipeline/data/profiles/gstack/phases.yaml      → moved from hermes_pipeline/data/phases.yaml
hermes_pipeline/data/profiles/agent-skills/phases.yaml→ new
tests/test_contract.py                                → profile field round-trip, unknown-profile error
tests/test_phases.py                                  → resolve_profile_phases_path() cases
docs/howto-pipeline-contract.md                       → document `profile` field
docs/howto-agent-skills-profile.md                    → new how-to
```

## Code Style

Follow existing dataclass + tomllib patterns in `contract.py` — no new
dependencies. Example of the extended contract load path:

```python
@dataclass(frozen=True)
class PipelineContract:
    schema_version: int
    assignee: str = "default"
    capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES
    profile: str = "gstack"


def resolve_profile_phases_path(profile: str) -> Path:
    from importlib.resources import files
    base = files("hermes_pipeline").joinpath("data", "profiles", profile, "phases.yaml")
    path = Path(base)
    if not path.is_file():
        raise ContractSchemaError(
            f"unknown pipeline profile {profile!r} — expected one of: "
            f"{', '.join(sorted(p.name for p in Path(files('hermes_pipeline').joinpath('data', 'profiles')).iterdir()))}"
        )
    return path
```

Bumping `CONTRACT_SCHEMA_VERSION` to 2 is required (new field with a default
is backward-compatible in TOML terms, but the project's existing
version-mismatch-fails-closed design in `load_contract` treats any field-set
change as a version bump — see CLAUDE.md's version-sync convention, which
applies to this package's own schema versioning, not just VERSION/pyproject).

## Testing Strategy

- Framework: pytest, matching existing `tests/test_contract.py` / `tests/test_phases.py` conventions.
- Unit: `resolve_profile_phases_path("gstack")` and `("agent-skills")` both
  resolve to real files with parseable YAML; unknown profile name raises
  `ContractSchemaError` with the list of valid profiles in the message.
- Unit: `load_contract` on a v1 contract (no `profile` field) defaults to
  `"gstack"`; a v2 contract with `profile = "agent-skills"` round-trips.
- Integration: `pipeline-watch init <project> --profile agent-skills` writes
  a contract that `pipeline-watch doctor <project>` then validates cleanly
  against `profiles/agent-skills/phases.yaml`'s required capabilities.

## Boundaries

- **Always:** keep `gstack` as the default profile for contracts that don't
  specify one — this must not require any migration for existing projects.
- **Ask first:** removing or renaming any `gstack` profile phase_key (a
  project's `.hermes/` state may reference phase_keys from an in-flight tick).
- **Never:** make phase_keys collide across profiles in a way that could
  cause `.hermes/` state written under one profile to be silently
  reinterpreted under another if a project's `profile` field is edited
  mid-flight — `doctor` should detect and fail closed on a profile change
  while a tick is in-flight for a todo.

## Success Criteria

- [ ] `PipelineContract` has a `profile` field; `CONTRACT_SCHEMA_VERSION` bumped.
- [ ] `hermes_pipeline/data/profiles/gstack/phases.yaml` is the moved
      bundled file, with `phase_8_finish_branch` updated to use `/ship`;
      existing tests referencing the old `hermes_pipeline/data/phases.yaml`
      path are updated.
- [ ] `hermes_pipeline/data/profiles/agent-skills/phases.yaml` exists per the
      phase mapping table above, and is loadable via `load_phases()`.
- [ ] `pipeline-watch init <project> --profile agent-skills` and
      `pipeline-watch doctor <project>` both work end-to-end against the new
      profile.
- [ ] `uv run pytest` passes.
- [ ] Docs updated: `docs/howto-pipeline-contract.md` +
      new `docs/howto-agent-skills-profile.md`.

## Open Questions

- Should `pipeline-watch doctor` warn (not just silently trust) when a
  project's configured `profile` names a bundled directory that exists but
  whose `phases.yaml` fails to parse — vs. today's fail-closed-with-error
  behavior for other contract issues? Assumed: same fail-closed pattern
  applies, no special-casing needed, but flagging for confirmation.
