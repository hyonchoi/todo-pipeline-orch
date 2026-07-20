# Why the Pipeline Execution Contract

The pipeline execution contract (`.hermes/pipeline.toml`) is a per-project TOML file that declares which Hermes assignee and tool capabilities a project's phases require. It was introduced to solve a specific problem: the pipeline needed a way to validate that a project's environment can actually run its phases before investing time in selection, kanban registration, and LLM calls.

## The Problem

Before the contract, the pipeline assumed every project could use the same tool set and assignee. Two failure modes emerged:

1. **Silent capability gaps.** A phase defined in `phases.yaml` required a tool (e.g., `Edit`) that a project's Hermes profile did not support. The phase would fail at execution time — after selection, kanban registration, and LLM invocation — with an error unrelated to the missing capability.

2. **Hardcoded assignee drift.** Kanban tasks were created with a hardcoded `--assignee` value. When operators changed their Hermes profile configuration, phases ran under the wrong profile with different model or tool permissions.

Both failures surfaced late in the tick lifecycle. The contract moves validation to tick start, before any work is done.

## The Approach

The contract is a declarative manifest — not a configuration file. It lives at `.hermes/pipeline.toml` and declares four things:

- **`schema_version`** — which version of the contract format is in use
- **`assignee`** — the Hermes profile that runs phases for this project
- **`profile`** — which pipeline skill-set profile's phases to run (e.g., `gstack`, `agent-skills`)
- **`capabilities`** — the tools phases are allowed to use

At tick start, `_tick_project` loads the contract, resolves the profile's phases, and validates capabilities. The validation has two paths:

1. **Contract exists.** Load it. Validate schema version and field types. Resolve the phase list from the contract's `profile` field. Check that every tool required by the profile's phases is in the contract capabilities. If anything is wrong, the tick fails with a specific error and exit code.

2. **Contract does not exist.** Auto-compute a contract from the default (`gstack`) profile's capabilities and default assignee. The tick proceeds. This makes the contract additive — projects that predate it keep working.

```
_tick_project(project)
    |
    +-- pipeline.toml exists?
    |       |
    |       +-- Yes --> load_contract()
    |       |   validate schema_version, assignee, capabilities, profile
    |       |   resolve_profile_phases_path(contract.profile) -> phases
    |       |   missing_capabilities(contract, phases)
    |       |       |
    |       |       +-- empty --> proceed with contract.assignee + contract.profile
    |       |       +-- not empty --> fail with remediation message
    |       |
    |       +-- No --> compute contract from gstack profile's capabilities
    |                   assignee = "default"
    |                   profile = "gstack"
    |                   capabilities = required_capabilities(load_phases(gstack_profile))
    |                   proceed
```

## Trade-offs

**Contract file vs config.toml section.** The contract could have been a section in `.hermes/config.toml`, which already exists for selection and circuit breaker settings. It was placed in a separate file because contract validation has different semantics: a config overlay is optional (unset keys fall back to defaults), but a contract is a gate. Mixing optional overlay with mandatory validation in one file would make the error surface confusing.

**Fail-closed on capability mismatch.** If a project's contract declares fewer capabilities than phases.yaml requires, the tick fails. It could instead warn and proceed, but that risks the same late-stage failures the contract was designed to prevent. The operator sees the gap at tick time, with a message that names the missing capabilities and points to `pipeline-watch doctor` for details.

**Computed defaults vs hardcoded defaults.** `pipeline-watch init` writes a contract whose capabilities are computed from `phases.yaml`, not the hardcoded `DEFAULT_CAPABILITIES` tuple. This means the init output matches the current phase definitions. If a future phase adds a new tool, `init --force` regenerates the contract with the new capability.

## Alternatives Considered

**Profile-based capabilities.** The Hermes profile API could theoretically declare which tools a profile supports. The contract was decoupled from the profile API because the Hermes profile API did not support model/tools/skills flags needed for pipeline work. See TODO-15 and TODO-16 for the upstream profile design tracking.

**Inline phase capability declaration.** Each phase could declare its own capability requirements in phases.yaml (which it already does via the `tools` field). The contract is a project-level aggregation of those declarations, validated at tick start. Without the contract, validation would need to happen at phase execution time — too late.

## See Also

- [How to configure the pipeline execution contract](howto-pipeline-contract.md) — task-oriented guide for editing, validating, and migrating contracts
- [Getting started tutorial](tutorial-getting-started.md) — `init` and `doctor` in the onboarding flow
- [README — pipeline execution contract](../README.md#pipeline-execution-contract) — reference: TOML schema and field descriptions
