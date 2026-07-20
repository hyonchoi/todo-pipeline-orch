# How to Use the agent-skills Profile

Pipeline profiles let a project select an independent set of phases — the prompts, tool capabilities, and turn/timeout budgets driving each stage of the pipeline — instead of being locked to the bundled `gstack` phase list. This guide covers the bundled `agent-skills` profile and how to configure a project to use it.

## Prerequisites

- `todo-pipeline-orchestrator` installed and `uv run pipeline-watch` working
- A project with a `TODOS.md` file in your `PIPELINE_PROJECTS_DIR`
- The `agent-skills` plugin's skills (spec-driven-development, planning-and-task-breakdown, incremental-implementation, test-driven-development, code-review-and-quality, security-and-hardening, ship) available in the environment that runs the pipeline

## What is a profile?

A profile is a directory under `hermes_pipeline/data/profiles/<name>/` containing a `phases.yaml` file that defines the pipeline's phase sequence. Two profiles are bundled:

- **`gstack`** (default) — spec → plan → implement → review → security → document → ship, using gstack skills (`/spec`, `/plan-eng-review`, `/build`, `/review`, `/ship`)
- **`agent-skills`** — the same shape, using the `agent-skills` plugin's skills instead

A project's `.hermes/pipeline.toml` contract records which profile it runs via the `profile` field. Switching profiles changes the prompts and required tool capabilities for every phase.

## Steps

### 1. Initialize a project with the agent-skills profile

```bash
uv run pipeline-watch init <project> --profile agent-skills
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
uv run pipeline-watch init <project> --force --profile agent-skills
```

### 2. Verify the contract

```bash
uv run pipeline-watch doctor <project>
```

Expected output:
```
OK: schema_version=2 assignee=default profile=agent-skills capabilities=['Bash', 'Edit', 'Read', 'Write']
```

`doctor` resolves phases from the profile named in the contract, not always `gstack`. If the `profile` field names a profile that doesn't exist, `doctor` fails closed with a `MISSING` error and exit code 2. If the profile's `phases.yaml` is malformed, `doctor` fails closed with an `INVALID` error and exit code 2.

### 3. Run the pipeline

Ticks read the phase list from the contract's `profile` field automatically — no further configuration needed. Each phase's prompt is the `agent-skills`-flavored version (e.g. Phase 1 invokes `agent-skills:spec-driven-development` rather than `/spec`).

## Switching an existing project's profile

Edit `.hermes/pipeline.toml` directly and change the `profile` field, then run `doctor` to confirm capabilities still cover the new profile's phases:

```toml
profile = "agent-skills"
```

```bash
uv run pipeline-watch doctor <project>
```

If `doctor` reports drift, regenerate the contract with `init --force --profile agent-skills` (this recomputes capabilities but discards any custom assignee/capabilities), or manually add the missing capabilities.

## Adding a new profile

1. Create `hermes_pipeline/data/profiles/<name>/phases.yaml` following the same schema as the bundled profiles (`phase_key`, `name`, `prompt`, `tools`, `turns`, `timeout` per phase; gate phases use `gate: true`).
2. Run `pipeline-watch init <project> --profile <name>` to write a contract selecting it.
3. Run `pipeline-watch doctor <project>` to confirm the profile resolves and capabilities are computed correctly.

## Troubleshooting

**"ERROR: unknown profile '<name>'" on init**
- The `--profile` flag names a profile that doesn't exist under `hermes_pipeline/data/profiles/`.
- **Fix:** Use `gstack` or `agent-skills`, or add a new profile directory first.

**"MISSING: ..." on doctor**
- The contract's `profile` field names a profile that no longer exists (e.g. it was renamed or removed).
- **Fix:** Edit `.hermes/pipeline.toml` to a valid profile name, or run `init --force --profile <valid-profile>`.

**"INVALID: failed to load phases for profile '<name>'"**
- The profile's `phases.yaml` is malformed (bad YAML, missing required fields).
- **Fix:** Validate the profile's `phases.yaml` against the schema used by the bundled profiles.

## Related

- [How to configure the pipeline execution contract](howto-pipeline-contract.md) — general contract usage (`init`, `doctor`, editing `pipeline.toml`)
- [Why the contract-first design](explanation-pipeline-contract.md) — design rationale
