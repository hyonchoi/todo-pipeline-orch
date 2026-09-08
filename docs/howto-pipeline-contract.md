# How to Configure the Pipeline Execution Contract

Each project declares the assignee and tool capabilities its phases require in a versioned TOML file at `.hermes/pipeline.toml`. This guide covers creating, editing, and validating the contract.

## Prerequisites

- `todo-pipeline-orchestrator` installed and `uv run tpo` working
- A project with a github.com `origin` remote under `tpo config get projects_dir`
- Hermes CLI installed and authenticated (`hermes login`)

## Steps

### 1. Write the default contract

Run `init` once per project. It computes capabilities from the selected profile's `phases.yaml` and writes `.hermes/pipeline.toml`:

```bash
uv run tpo init <project>
```

Expected output:
```
Wrote pipeline execution contract: /path/to/<project>/.hermes/pipeline.toml
```

The file looks like:
```toml
# Pipeline execution contract — read at tick start.
# See docs/tutorial-getting-started.md and `tpo doctor --help`.
schema_version = 3
assignee = "default"
review_assignee = "default"
capabilities = ["Bash", "Edit", "Read", "Write"]
profile = "native-sdd"
```

If a contract already exists, `init` is a no-op. Use `--force` to regenerate:

```bash
uv run tpo init <project> --force
```

Use `--profile` to select a pipeline skill-set profile other than the default `native-sdd`:

```bash
uv run tpo init <project> --profile agent-skills
```

See [How to use the agent-skills profile](howto-agent-skills-profile.md) for details on the bundled profiles and how to add your own.

### 2. Verify the contract is consistent

Run `doctor` to check the contract against the selected profile's `phases.yaml`:

```bash
uv run tpo doctor <project>
```

Four possible outcomes:

| Output | Exit code | Meaning |
|--------|-----------|---------|
| `OK: ...` | 0 | Contract capabilities cover all phases in the selected profile |
| `DRIFT: ...` | 1 | Contract is missing capabilities the profile's phases.yaml requires |
| `MISSING: ...` | 2 | No contract file exists, or the contract's `profile` doesn't exist |
| `INVALID: ...` | 2 | Malformed TOML, missing fields, wrong schema version, or malformed profile phases.yaml |

### 3. Edit the Contract

Edit `.hermes/pipeline.toml` directly to customize the assignee, capabilities, or profile:

```toml
schema_version = 3
assignee = "pipeline"           # your Hermes profile name
review_assignee = "reviewer"    # Hermes profile for review cards
capabilities = ["Bash", "Edit", "Read", "Write", "Agent"]
profile = "native-sdd"          # which phases.yaml to run
```

- **`schema_version`** — Do not edit manually. Bump only when the contract field set changes. Regenerate with `init --force` instead.
- **`assignee`** — Passed as `--assignee` when registering each phase's kanban task. Change this to route phases to a different Hermes profile.
- **`review_assignee`** — The Hermes profile that review cards are registered to, so an independent review runs under a different profile than the workers. `init` writes `"default"`; `init --assignee <name>` re-renders it as a copy of `assignee`.
- **`capabilities`** — The tool set phases are allowed to use. If a phase in the selected profile's phases.yaml requires a tool not in this list, the tick fails with a capability mismatch error.
- **`profile`** — Which pipeline skill-set profile's phases.yaml to run (`native-sdd` by default, or `agent-skills`; `gstack` is deprecated). Omitting the key entirely is legacy behavior: the contract then resolves to `gstack` and `doctor`/`tick` print a deprecation notice. See [How to use the agent-skills profile](howto-agent-skills-profile.md).

### 4. Fix Drift

If `doctor` reports drift, the contract is missing capabilities that the selected profile's phases.yaml requires. Two options:

**Add the missing capabilities to the contract:**
```toml
capabilities = ["Bash", "Edit", "Agent", "Read", "Write"]
```

**Regenerate the default contract:**
```bash
uv run tpo init <project> --force
```

This overwrites the file with capabilities computed from the current profile's phases.yaml. Any custom `assignee`, `review_assignee`, or `capabilities` will be lost.

## Verification

Confirm the contract is valid and the assignee is used by ticks:

```bash
uv run tpo doctor <project>
# Should print: OK: schema_version=3 assignee=... profile=... capabilities=[...]

uv run tpo tick
# Run a tick, check logs for: "registered N kanban tasks for TODO-X"
```

## Troubleshooting

**"CapabilityMismatchError: contract missing capabilities"**
- The contract exists but is missing tools phases.yaml requires.
- **Fix:** Run `tpo doctor <project>` to see which capabilities are missing, then add them to `.hermes/pipeline.toml` or regenerate with `init --force`.

**"ContractVersionMismatchError: schema_version=99, expected 2"**
- The contract file has a `schema_version` the code doesn't recognize.
- **Fix:** Run `tpo init <project> --force` to regenerate with the current schema version. This resets `profile` to `native-sdd` unless you also pass `--profile <name>` — if the project was previously running a non-default profile, re-specify it explicitly or the regenerated contract will silently switch phase sets.

**"ContractMissingError"**
- No `.hermes/pipeline.toml` exists for this project.
- **Fix:** Run `tpo init <project>`. Ticks still work without a contract (they fall back to computed defaults), but `doctor` will report the file as missing.

**"MISSING: ... unknown profile"**
- The contract's `profile` field names a profile that doesn't exist under `hermes_pipeline/data/phase-profiles/`.
- **Fix:** Correct the `profile` field to a valid name, or run `tpo init <project> --force --profile <valid-profile>`.

## Related

- [Why the contract-first design](explanation-pipeline-contract.md) — design rationale: versioned contracts, drift detection, capability gates
- [Getting started tutorial](tutorial-getting-started.md) — step-by-step pipeline setup including `init` and `doctor`
- [How to use the agent-skills profile](howto-agent-skills-profile.md) — pluggable pipeline skill-set profiles
