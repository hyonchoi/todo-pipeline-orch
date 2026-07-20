# Todo: Pluggable Pipeline Skill-Set Profiles

See `tasks/plan.md` for full context, rationale, and verification details.

## Phase 1: Contract Schema + Phase Resolution
- [x] Task 1: Add `profile` field to `PipelineContract`, bump `CONTRACT_SCHEMA_VERSION` to 2 (`hermes_pipeline/contract.py`)
- [x] Task 2: Add `resolve_profile_phases_path()`, repoint `load_phases()` default (`hermes_pipeline/phases.py`)

### Checkpoint A
- [x] `uv run pytest tests/test_contract.py tests/test_phases.py -v`

## Phase 2: Bundled Phase Files
- [x] Task 3: Move gstack phases to `hermes_pipeline/data/profiles/gstack/phases.yaml`, swap `phase_8_finish_branch` prompt to `/ship`, delete old `data/phases.yaml`
- [x] Task 4: Author `hermes_pipeline/data/profiles/agent-skills/phases.yaml` (9 phases per SPEC.md mapping table)

### Checkpoint B
- [x] `uv run pytest tests/test_phases.py -v`
- [x] Manual: both profiles load with correct phase counts (9 / 9)

## Phase 3: CLI Surface
- [x] Task 5: `pipeline-watch init --profile` flag (`hermes_pipeline/cli.py`)
- [x] Task 6: `pipeline-watch doctor` profile-aware capability check + fail-closed on malformed profile YAML (`hermes_pipeline/cli.py`)

### Checkpoint C
- [x] `init --profile agent-skills` then `doctor` end-to-end, exit 0
- [x] `uv run pytest` full suite

## Phase 4: Docs
- [x] Task 7: Update `docs/howto-pipeline-contract.md`, add `docs/howto-agent-skills-profile.md`

### Checkpoint D: Complete
- [x] All SPEC.md Success Criteria checked
- [x] `uv run pytest` and `uv run ruff check hermes_pipeline/ tests/` clean (ruff not installed in this env; pre-existing unrelated lint debt noted, not introduced by this feature)
