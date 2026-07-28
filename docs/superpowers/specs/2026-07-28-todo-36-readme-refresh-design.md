# TODO-36 README Refresh Design

## Goal

Refresh `README.md` so it is a simple, direct front door for `tpo`.
The README should explain what the project does, how to install it, what setup is
required before normal use, what each CLI subcommand is for, and where advanced
topics live. It should not be a full operations manual.

## Scope

In scope:

- Rewrite `README.md` around overview, install, prerequisites, core workflows,
  subcommand summary, grouped documentation, contributing, and license.
- Revise `docs/tutorial-getting-started.md` as the essential first-run companion
  so it matches the README's install path, prerequisites, and onboarding split.
- Reorganize the current flat documentation table into subsystem groups.
- Fix the malformed/broken documentation table and the stale todos-manager
  install link.
- Remove stale or misleading top-level guidance that presents source-checkout
  `uv run tpo ...` usage as the installed-user path.
- Update stale CLI naming in `docs/pipeline-modularization-plan.md` where it
  still points readers at pre-rename commands such as `pipeline-watch` as the
  current entrypoint.
- Keep advanced details in existing linked docs instead of expanding the README.

Out of scope:

- Changing CLI behavior.
- Changing `tpo init` semantics.
- Rewriting the full CLI reference. The CLI reference remains the detailed
  command manual linked from README and the tutorial.
- Adding new install targets. The current CLI target names remain `codex`,
  `claude`, and `all`.

## README Structure

Use this structure:

1. `# todo-pipeline-orchestrator`
2. `## Overview`
3. `## Install`
4. `## Prerequisite setup`
5. `## Core workflows`
6. `## Subcommands`
7. `## Documentation`
8. `## Contributing`
9. `## License`

The overview should be short. It should identify `tpo` as the CLI, describe the
package as a TODO and pipeline orchestration toolkit, and mention that pipeline
phases run through Hermes agent profiles. It should not require provider
authentication or `hermes login` as a baseline prerequisite.

## Install Design

If `uv` is missing, show the uv installer first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The primary install path is the installed CLI:

```bash
uv tool install ...
tpo --version
```

Use the exact install source supported by the current package/release state.
After installation, examples should invoke `tpo ...` directly.

Source-checkout development belongs in a short separate note:

```bash
uv sync
uv run tpo --version
```

Use `uv run tpo ...` only in that contributor/source-checkout context.

## Prerequisite Setup

The README should list only operational prerequisites:

- A Hermes agent runtime/profile available for pipeline phase execution.
- The bundled pipeline profile installed when the user wants unattended pipeline
  execution:

```bash
tpo install-profile
```

- The bundled `todos-manager` skill installed for the agent that will edit
  TODOs:

```bash
tpo skills install --target all
```

For project-local Codex/agents setup, show:

```bash
tpo skills install --scope project --target codex
```

Explain the target vocabulary:

- `codex` installs to the `.agents/skills` convention.
- `claude` installs to the `.claude/skills` convention.
- `all` installs both.

Do not tell users to run `hermes login` in README prerequisites. Provider
authentication is model/provider-specific and belongs in linked setup docs.

## Core Workflow Design

Keep workflows short and command-oriented.

The README should distinguish three onboarding paths:

- New project with no `TODOS.md`: install `todos-manager`, run
  `todos-manager --init`, then use `todos-manager --add`.
- Existing project with hand-written `TODOS.md`: run
  `todos-manager --convert`, then use `--audit` or `--revise` as needed.
- Pipeline contract setup: run `tpo init <project>` only to write
  `.hermes/pipeline.toml`. Do not imply this creates or converts `TODOS.md`.

Then show the normal pipeline loop:

```bash
tpo config init
tpo config set projects_dir ~/my-projects
tpo tick
tpo doctor <project>
tpo approve <project> --todo TODO-5
```

Keep detailed explanations behind links.

## Getting-Started Tutorial Design

`docs/tutorial-getting-started.md` should be revised alongside README because it
is the README's primary first-run link. It should be more detailed than README
but follow the same conceptual order:

1. Install `tpo` as a tool and verify direct `tpo --version` usage.
2. If `uv` is missing, install `uv` first.
3. Call out source-checkout usage separately for contributors.
4. Install the pipeline profile when the user wants pipeline phase execution.
5. Install `todos-manager` for the intended agent target/scope.
6. Choose the project onboarding path:
   - new project: `todos-manager --init`, then `todos-manager --add`;
   - existing project: `todos-manager --convert`, then `--audit` or `--revise`;
   - pipeline contract: `tpo init <project>` writes `.hermes/pipeline.toml`.
7. Configure `projects_dir`.
8. Run `tpo doctor`, `tpo tick`, and later `tpo approve`.

The tutorial must not create a non-canonical hand-written `TODOS.md` as the
first-run example. It should use `todos-manager --init` for a new project or
explicitly frame any legacy file as input to `todos-manager --convert`.

## Subcommand Summary

Use a compact table for these subcommands:

| Subcommand | Purpose |
|---|---|
| `tick` | Discover configured projects, select eligible TODOs, and register pipeline phases. |
| `approve` | Ship a reviewed TODO through the approval/merge workflow. |
| `init` | Write `.hermes/pipeline.toml` for an existing project. |
| `doctor` | Verify a project's pipeline contract against the selected profile. |
| `recover-counter` | Rebuild legacy TODO counter compatibility state from tracked TODO IDs. |
| `install-profile` | Install or refresh the bundled pipeline Hermes profile. |
| `skills` | Install or remove the bundled `todos-manager` skill. |
| `config` | Read and write global `tpo` configuration. |
| `test` | Run the mock integration test harness. |

This table is a map, not a reference manual. Link to `docs/reference-cli.md` for
arguments, exit codes, and detailed behavior.

## Documentation Grouping

Replace the current flat documentation table with grouped sections. Suggested
groups:

- Start here
- TODO management
- Pipeline setup and operation
- Multi-project configuration
- Contracts, profiles, and adapters
- Testing and harnesses
- Architecture and reference
- Troubleshooting and recovery

Every existing useful README documentation link should appear in exactly one
group unless there is a strong reason to remove it. Fix broken anchors and avoid
blank lines that split a markdown table.

## Advanced Content To Move Out Of README

The README should remove or collapse these advanced inline topics:

- TOML overlay field details.
- Full `.hermes/pipeline.toml` schema explanation.
- Architecture lane breakdown.
- Long troubleshooting details.
- Cron scheduling details.
- Full CLI argument and exit-code reference.
- Circuit-breaker internals.

Each removed topic should have a link to the relevant existing doc when one
exists.

## Error Handling

The implementation should avoid introducing new docs drift:

- If a command example is install-context usage, use direct `tpo ...`.
- If a command example is source-checkout usage, label it as source checkout and
  use `uv run tpo ...`.
- If an advanced detail lacks a suitable linked doc, keep a one-line pointer in
  README and prefer improving the existing doc over expanding README.
- If `docs/pipeline-modularization-plan.md` is intentionally historical in a
  specific section, keep historical context but label obsolete entrypoints as
  historical rather than current usage.

## Verification

Implementation verification should include:

- Search README for `uv run tpo` and confirm every occurrence is explicitly
  source-checkout/contributor context.
- Search `docs/tutorial-getting-started.md` for `uv run tpo` and confirm every
  occurrence is explicitly source-checkout/contributor context.
- Search README for `hermes login` and confirm it is absent.
- Search `docs/tutorial-getting-started.md` for `hermes login` and confirm it is
  absent unless it is clearly framed as provider-specific optional setup.
- Search README for `--target agents` and confirm it is absent.
- Search `docs/tutorial-getting-started.md` for `--target agents` and confirm it
  is absent.
- Search README for `tpo init` and confirm it is described as pipeline contract
  setup only.
- Search `docs/tutorial-getting-started.md` for `tpo init` and confirm it is
  described as pipeline contract setup only.
- Run a markdown link check or a focused link/path audit for README links.
- Search `docs/pipeline-modularization-plan.md` for stale current-use references
  to `pipeline-watch` and `hermes-pipeline` CLI usage and update or label them.

## Acceptance Criteria

- README is shorter and easier to scan than the current file.
- README's first-run path does not conflate `todos-manager --init` with
  `tpo init`.
- README and the getting-started tutorial show how to install `uv` when it is
  missing.
- `docs/tutorial-getting-started.md` matches the README's install,
  prerequisites, and onboarding split.
- The getting-started tutorial no longer teaches users to create a
  non-canonical hand-written `TODOS.md` as the normal new-project path.
- Installed CLI examples use `tpo ...` directly.
- Source checkout examples are clearly labeled.
- Prerequisites mention Hermes agent/profile availability but do not require
  provider authentication.
- The documentation index is grouped by subsystem and has no malformed table
  break.
- The todos-manager install guidance names current CLI targets:
  `codex`, `claude`, and `all`.
