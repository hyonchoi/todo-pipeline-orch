# How to Use the agent-skills Profile

Pipeline profiles let a project select an independent set of phases — the prompts, tool capabilities, and turn/timeout budgets driving each stage of the pipeline — instead of being locked to the bundled `gstack` phase list. This guide covers the bundled `agent-skills` profile and how to configure a project to use it.

## Prerequisites

- `todo-pipeline-orchestrator` installed and `tpo` working
- A project with a `TODOS.md` file under `tpo config get projects_dir`
- The `agent-skills` plugin's skills listed below available in the environment
  that runs the pipeline

Namespaced invocation and discovery for the `agent-skills` profile remain
`Unverified` for both Claude Code and Codex. This profile/client combination is
unsupported, and changing `prompt_client` does not promote its support status.
`tpo doctor` reports `UNSUPPORTED` and exits 2 for this profile until its
client contracts are qualified. Runtime ticks use the same fail-closed support
policy before selection and registration, so an unsupported profile cannot
dispatch merely because an operator skipped `doctor`.
To remediate, use a verified profile/client pair, provide versioned
[qualification evidence](release-qualification-agent-clients.md) that can
promote the package metadata, or keep the row unsupported.

## What is a profile?

A profile is a directory under
`hermes_pipeline/data/phase-profiles/<name>/` containing both `phases.yaml` and
`prerequisites.yaml`. The first file defines the pipeline's phase sequence; the
second is the structured source of truth for client support, discovery, and
invocation metadata. `tpo doctor` loads both files unconditionally. Three
profiles are bundled:

- **`gstack`** (default) — the gstack/superpowers workflow. Skills:
  `ai-coding-agents`, `autoplan`, `writing-plans`,
  `subagent-driven-development`, `review`, `cso`, `qa`, `document-release`,
  `document-generate`, `ship`.
- **`agent-skills`** — the agent-skills plugin workflow. Skills:
  `agent-skills:spec-driven-development`,
  `agent-skills:planning-and-task-breakdown`,
  `agent-skills:incremental-implementation`,
  `agent-skills:test-driven-development`,
  `agent-skills:code-review-and-quality`, `agent-skills:code-reviewer`,
  `agent-skills:security-and-hardening`, `agent-skills:security-auditor`,
  `agent-skills:ship`.
- **`native-sdd`** — Plan-gated native subagent TDD, independent review, PR
  creation, and a human gate, without gstack, superpowers, or client workflow
  skills. Skills: `ai-coding-agents`.

A project's `.hermes/pipeline.toml` contract records which profile it runs via the `profile` field. Switching profiles changes the prompts and required tool capabilities for every phase.

## Steps

### 1. Initialize a project with the agent-skills profile

```bash
tpo init <project> --profile agent-skills
```

Expected output:
```
Wrote pipeline execution contract: /path/to/<project>/.hermes/pipeline.toml
```

The contract records the profile and computes capabilities from `agent-skills/phases.yaml`:
```toml
schema_version = 2
assignee = "default"
capabilities = ["Bash", "Edit", "Read", "Write"]
profile = "agent-skills"
```

If the project already has a contract, `init` is a no-op unless you pass `--force`:

```bash
tpo init <project> --force --profile agent-skills
```

### 2. Verify the contract

```bash
tpo doctor <project>
```

Expected output:
```text
prompt client: <claude-or-codex> (global for all projects under projects_dir)
...
UNSUPPORTED: profile 'agent-skills' has Unverified prerequisites for prompt client '<claude-or-codex>'
```

`doctor` resolves phases from the profile named in the contract, not always
`gstack`. If the `profile` field names a profile that doesn't exist, `doctor`
fails closed with a `MISSING` error and exit code 2. If either profile data file
is missing or malformed, doctor reports
`INVALID: failed to load profile data for '<name>'` and exits 2.
The bundled `agent-skills` profile also exits 2 by design because its external
client contracts remain `Unverified`; the contract itself may still be
schema-valid.

### 3. Run the pipeline

Ticks read the phase list from the contract's `profile` field automatically — no further configuration needed. Each phase's prompt is the `agent-skills`-flavored version (e.g. Phase 1 invokes `agent-skills:spec-driven-development` rather than `/spec`).

## Switching an existing project's profile

Edit `.hermes/pipeline.toml` directly and change the `profile` field, then run `doctor` to confirm capabilities still cover the new profile's phases:

```toml
profile = "agent-skills"
```

```bash
tpo doctor <project>
```

If `doctor` reports drift, regenerate the contract with `init --force --profile agent-skills` (this recomputes capabilities but discards any custom assignee/capabilities), or manually add the missing capabilities.

## Adding a new profile

1. Create `hermes_pipeline/data/phase-profiles/<name>/phases.yaml` following
   the same schema as the bundled profiles (optional top-level
   `requires_plan`, plus `phase_key`, `name`, `prompt`,
   `tools`, `turns`, `timeout` per phase; gate phases use `gate: true`).
2. Create the mandatory sibling
   `hermes_pipeline/data/phase-profiles/<name>/prerequisites.yaml`. It has this
   schema:
   - `schema_version`: integer `1`
   - `profile`: the exact profile directory name
   - `skills`: a list of mappings with `skill_id`, `distribution_owner`,
     `support`, and `clients`
   - `clients`: exactly `claude` and `codex`; each maps to exactly
     `discovery_root` and `invocation`
   - `support`: `Conditional` requires non-empty client discovery and
     invocation strings; `Unverified` requires both client fields to be null
3. Treat `prerequisites.yaml` as package data beside `phases.yaml`;
   `hermes_pipeline.phases.load_profile_prerequisites()` is the validating
   loader; `tpo doctor` is the user-facing check, and `tpo tick` enforces
   `Unverified` rows before selecting or registering work.
4. Run `tpo init <project> --profile <name>` to write a contract selecting it.
5. Run `tpo doctor <project>` to confirm both files resolve, prerequisite
   metadata validates, and capabilities are computed correctly.

## Troubleshooting

**"ERROR: unknown profile '<name>'" on init**
- The `--profile` flag names a profile that doesn't exist under `hermes_pipeline/data/phase-profiles/`.
- **Fix:** Use `gstack` or `agent-skills`, or add a new profile directory first.

**"MISSING: ..." on doctor**
- The contract's `profile` field names a profile that no longer exists (e.g. it was renamed or removed).
- **Fix:** Edit `.hermes/pipeline.toml` to a valid profile name, or run `init --force --profile <valid-profile>`.

**"INVALID: failed to load profile data for '<name>'"**
- The profile's `phases.yaml` or mandatory sibling `prerequisites.yaml` is
  missing or malformed.
- **Fix:** Validate `phases.yaml` against the bundled phase schema and
  `prerequisites.yaml` against the schema above. Confirm its `profile` matches
  the directory name and its `clients` mapping contains exactly `claude` and
  `codex`.

## Related

- [How to configure the pipeline execution contract](howto-pipeline-contract.md) — general contract usage (`init`, `doctor`, editing `pipeline.toml`)
- [Why the contract-first design](explanation-pipeline-contract.md) — design rationale
