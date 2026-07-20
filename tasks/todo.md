# Todo: Pluggable Pipeline Skill-Set Profiles

See `tasks/plan.md` for full context, rationale, and verification details.

## Phase 1: Contract Schema + Phase Resolution
- [x] Task 1: Add `profile` field to `PipelineContract`, bump `CONTRACT_SCHEMA_VERSION` to 2 (`hermes_pipeline/contract.py`)
- [ ] Task 2: Add `resolve_profile_phases_path()`, repoint `load_phases()` default (`hermes_pipeline/phases.py`)

### Checkpoint A
- [ ] `uv run pytest tests/test_contract.py tests/test_phases.py -v`

## Phase 2: Bundled Phase Files
- [ ] Task 3: Move gstack phases to `hermes_pipeline/data/profiles/gstack/phases.yaml`, swap `phase_8_finish_branch` prompt to `/ship`, delete old `data/phases.yaml`
- [ ] Task 4: Author `hermes_pipeline/data/profiles/agent-skills/phases.yaml` (8 phases per SPEC.md mapping table)

### Checkpoint B
- [ ] `uv run pytest tests/test_phases.py -v`
- [ ] Manual: both profiles load with correct phase counts (9 / 8)

## Phase 3: CLI Surface
- [ ] Task 5: `pipeline-watch init --profile` flag (`hermes_pipeline/cli.py`)
- [ ] Task 6: `pipeline-watch doctor` profile-aware capability check + fail-closed on malformed profile YAML (`hermes_pipeline/cli.py`)

### Checkpoint C
- [ ] `init --profile agent-skills` then `doctor` end-to-end, exit 0
- [ ] `uv run pytest` full suite

## Phase 4: Docs
- [ ] Task 7: Update `docs/howto-pipeline-contract.md`, add `docs/howto-agent-skills-profile.md`

### Checkpoint D: Complete
- [ ] All SPEC.md Success Criteria checked
- [ ] `uv run pytest` and `uv run ruff check hermes_pipeline/ tests/` clean
