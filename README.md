# todo-pipeline-orchestrator

Pipeline watcher and TODOS manager orchestration toolkit, packaged as a uv-managed Python project. The installed CLI is `tpo`.

## Overview

`tpo` helps maintain schema-enforced `TODOS.md` files, install the bundled `todos-manager` skill, prepare pipeline contracts, and run pipeline ticks through PR handoff. Pipeline phases run through Hermes agent profiles and use kanban tasks for phase scheduling.

Use this README as the quick map. Detailed setup, reference, and recovery guides live in the documentation index below.

## Install

Install `uv` if it is not already available:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install the CLI from a source checkout as a uv tool:

```bash
git clone https://github.com/hyonchoi/todo-pipeline-orch.git
cd todo-pipeline-orch
uv tool install .
tpo --version
```

For contributor work in that checkout, use the project environment instead:

```bash
uv sync
uv run tpo --version
```

## Prerequisite setup

For pipeline phase execution, make sure the Hermes CLI is installed, on `PATH`, and has an agent runtime/profile available. Provider authentication is model-specific and is not a baseline `tpo` prerequisite.

Install the bundled pipeline profile when you want unattended pipeline phase execution:

```bash
tpo install-profile
```

Install `todos-manager` for the agent that will edit TODOs:

```bash
tpo skills install --target all
```

For project-local Codex/agents setup:

```bash
tpo skills install --scope project --target codex
```

Skill targets:

| Target | Installs to |
|---|---|
| `codex` | `.agents/skills` convention |
| `claude` | `.claude/skills` convention |
| `all` | both conventions |

### Client prerequisites

The bundled profiles reference externally distributed skills. Selecting a
prompt client does not install those skills. `Conditional` rows are supported
only when every listed skill is installed and discoverable by the worker.
`Unverified` rows are unsupported until their external discovery and invocation
contracts have qualification evidence. `tpo doctor` fails closed with exit code
2 when the selected profile contains an `Unverified` prerequisite, and `tpo tick`
refuses to select or register work for that unsupported profile/client pair.
Hermes-owned prerequisites are verified against the assigned Hermes profile's
local skill registry; remote worker prerequisites remain operator-provisioned.

| Profile | Referenced skill | Distribution owner | Claude discovery / invocation | Codex discovery / invocation | Support |
|---|---|---|---|---|---|
| `gstack` | `ai-coding-agents` | hermes | `Hermes skill registry` / `claude -p` | `Hermes skill registry` / `codex exec` | Conditional |
| `gstack` | `autoplan` | gstack | `.claude/skills` / `/autoplan` | `.codex/skills` / `$autoplan` | Conditional |
| `gstack` | `writing-plans` | superpowers | `~/.claude/plugins/cache/claude-plugins-official/superpowers/*/skills` / `/writing-plans` | `~/.config/codex/plugins/cache/openai-curated-remote/superpowers/*/skills` / `$superpowers:writing-plans` | Conditional |
| `gstack` | `subagent-driven-development` | superpowers | `~/.claude/plugins/cache/claude-plugins-official/superpowers/*/skills` / `/subagent-driven-development` | `~/.config/codex/plugins/cache/openai-curated-remote/superpowers/*/skills` / `$superpowers:subagent-driven-development` | Conditional |
| `gstack` | `review` | gstack | `.claude/skills` / `/review` | `.codex/skills` / `$review` | Conditional |
| `gstack` | `cso` | gstack | `.claude/skills` / `/cso` | `.codex/skills` / `$cso` | Conditional |
| `gstack` | `qa` | gstack | `.claude/skills` / `/qa` | `.codex/skills` / `$qa` | Conditional |
| `gstack` | `document-release` | gstack | `.claude/skills` / `/document-release` | `.codex/skills` / `$document-release` | Conditional |
| `gstack` | `document-generate` | gstack | `.claude/skills` / `/document-generate` | `.codex/skills` / `$document-generate` | Conditional |
| `gstack` | `ship` | gstack | `.claude/skills` / `/ship` | `.codex/skills` / `$ship` | Conditional |
| `agent-skills` | `agent-skills:spec-driven-development` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:planning-and-task-breakdown` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:incremental-implementation` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:test-driven-development` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:code-review-and-quality` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:code-reviewer` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:security-and-hardening` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:security-auditor` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:ship` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |

See [agent client release qualification](docs/release-qualification-agent-clients.md)
for the evidence required to advertise a `Conditional` pair.

## Core workflows

Configure the project scan directory and prompt vocabulary:

```bash
tpo config init
tpo config set projects_dir ~/my-projects
tpo config get prompt_client
tpo config set prompt_client codex
tpo config get prompt_client
tpo doctor <project>
```

One global client covers every project under `projects_dir`; use separate
project roots for mixed Claude/Codex fleets. The settings have separate jobs:

| Setting | Selects |
|---|---|
| Global `prompt_client` | Prompt vocabulary only (`claude` or `codex`) |
| Contract `profile` | Bundled phase and skill workflow |
| Contract `assignee` | Hermes profile and agent identity |
| Hermes configuration | Models and provider authentication |

See the [CLI configuration reference](docs/reference-cli.md#config) for accepted
values, source behavior, and installed-user commands.

Start a new project with no `TODOS.md` by asking your agent to invoke the installed `todos-manager` skill with `--init`, then `--add`:

```bash
cd ~/my-projects/my-project
```

```text
todos-manager --init
todos-manager --add
```

Adopt an existing hand-written `TODOS.md` by invoking the `todos-manager` skill with `--convert`, then `--audit`:

```bash
cd ~/my-projects/my-project
```

```text
todos-manager --convert
todos-manager --audit
```

Invoke `todos-manager --revise` for individual entries that need stronger fields.

Write or verify the pipeline contract for an existing project:

```bash
tpo init my-project
tpo doctor my-project
```

Run the pipeline:

```bash
tpo tick
tpo tick my-project
```

## Subcommands

| Subcommand | Purpose |
|---|---|
| `tick` | Discover configured projects, select eligible TODOs, and register pipeline phases. |
| `approve` | Legacy guarded merge helper for existing ship-gate sidecars. |
| `init` | Write `.hermes/pipeline.toml` for an existing project. |
| `doctor` | Verify a project's pipeline contract against the selected profile. |
| `recover-counter` | Rebuild legacy TODO counter compatibility state from tracked TODO IDs. |
| `install-profile` | Install or refresh the bundled pipeline Hermes profile. |
| `skills` | Install or remove the bundled `todos-manager` skill. |
| `config` | Read and write global `tpo` configuration. |
| `test` | Run the mock integration test harness. |

See [CLI reference](docs/reference-cli.md) for arguments, exit codes, and detailed behavior.

## Documentation

### Start here

| Doc | Type | When to read |
|---|---|---|
| [Getting-started tutorial](docs/tutorial-getting-started.md) | Tutorial | First time using `tpo` end-to-end |
| [CLI reference](docs/reference-cli.md) | Reference | All subcommands, arguments, exit codes, and environment variables |

### TODO management

| Doc | Type | When to read |
|---|---|---|
| [TODOS Manager skill](hermes_pipeline/data/skills/todos-manager/SKILL.md) | Reference | TODOS.md schema, ID assignment, and subcommands |
| [Getting started with todos-manager](docs/tutorial-todos-manager.md) | Tutorial | Step-by-step TODO lifecycle walkthrough |
| [Manage TODOS.md with todos-manager](docs/howto-todos-manager.md) | How-to | Using `--init`, `--add`, `--convert`, `--audit`, `--archive`, `--list`, `--revise` |

### Pipeline setup and operation

| Doc | Type | When to read |
|---|---|---|
| [Run a manual tick](docs/howto-pipeline-tick.md) | How-to | Running `tpo tick` for iterative development |
| [Set up the pipeline profile](docs/howto-pipeline-profile.md) | How-to | Installing the dedicated pipeline Hermes profile |
| [Debug ticks and recover counters](docs/howto-debugging-and-recovery.md) | How-to | Using `--verbose`, `--debug`, and `recover-counter` |
| [Handle phase 5 review outcomes](docs/howto-review-outcomes.md) | How-to | Inspecting review artifacts and reverted/timed-out reviews |

### Multi-project configuration

| Doc | Type | When to read |
|---|---|---|
| [Configure `.hermes/config.toml`](docs/howto-config-toml.md) | How-to | Tuning selection model or circuit-breaker thresholds |
| [Set up multiple projects](docs/howto-multi-project-setup.md) | How-to | Configuring per-project settings and the scan loop |
| [Multi-project scan tutorial](docs/tutorial-multi-project-scan.md) | Tutorial | Setting up two projects and running the scan loop |
| [How the scan loop works](docs/explanation-multi-project-scan.md) | Explanation | Global lock, migration decisions, and trade-offs |
| [Troubleshoot state migration](docs/howto-troubleshoot-state-migration.md) | How-to | Migration failed or skipped with multiple projects |

### Contracts, profiles, and adapters

| Doc | Type | When to read |
|---|---|---|
| [Configure the pipeline contract](docs/howto-pipeline-contract.md) | How-to | Editing assignee, fixing capability drift, schema migration |
| [Why the pipeline contract](docs/explanation-pipeline-contract.md) | Explanation | Design rationale for versioned contracts and capability gates |
| [Use the agent-skills profile](docs/howto-agent-skills-profile.md) | How-to | Selecting `gstack` or `agent-skills` pipeline phases |
| [Qualify agent clients for release](docs/release-qualification-agent-clients.md) | Reference | Capturing evidence for conditional profile/client support |
| [Use the Hermes adapter](docs/howto-hermes-adapter.md) | How-to | How `hermes chat -q` routes LLM calls |
| [Selection seat contract](hermes_pipeline/decision/README.md) | Reference | Integrating with the Hermes config repo |

### Testing and harnesses

| Doc | Type | When to read |
|---|---|---|
| [Run the eval suite](docs/howto-eval-suite.md) | How-to | Before changing the prompt, model, or `decision/agent.py` |
| [Skill test environment](tests/skill-test-environment/README.md) | How-to | Running structural unit tests for `todos-manager` |
| [Skill test environment quickstart](docs/howto-skill-test-environment.md) | How-to | Adding and maintaining skill harness tests |
| [Skill test harness API](docs/reference-skill-test-harness.md) | Reference | Complete API for the skill test environment |
| [Why the skill test harness is pure-Python](docs/explanation-skill-test-harness-design.md) | Explanation | Golden-file harness design rationale |
| [Mock integration test harness](docs/howto-mock-integration-test-harness.md) | How-to | Running `tpo test` against mock project data |
| [Harness production-code coverage checklist](docs/checklist-harness-production-coverage.md) | Reference | Acceptance criteria for production-code path reuse |

### Architecture and reference

| Doc | Type | When to read |
|---|---|---|
| [Architecture overview](docs/ARCHITECTURE.md) | Explanation | Understanding lane structure, data flow, and phase execution |
| [Pipeline state machine](docs/hermes-state-machine.md) | Explanation | Understanding `.hermes/` file layout and transitions |
| [Modularization plan](docs/pipeline-modularization-plan.md) | Explanation | Historical architecture and design plan |
| [Kanban-as-Scheduler](docs/reference-kanban-as-scheduler.md) | Reference/Explanation | How `tpo tick` uses kanban for phase state and ordering |
| [Counter recovery](docs/reference-counter.md) | Reference/Explanation | How `recover_counter()` works |
| [Circuit breaker](docs/explanation-circuit-breaker.md) | Explanation | How no-progress tracking works and why it alerts |
| [Decision module API](docs/reference-decision-api.md) | Reference | Selection schemas, outcome sidecars, and plan-gate types |

## Contributing

Found a bug or feature request? [Open an issue on GitHub](https://github.com/hyonchoi/todo-pipeline-orch/issues).

Pull requests record their release intent with
[Changesets-style fragments](https://github.com/changesets/changesets) managed
by the repository's Python release command. This workflow requires Python and
`uv`; it does not require Node.js, npm, or the Changesets client.

Choose the semantic-version impact and provide the user-facing changelog entry:

```bash
uv run python scripts/release_changesets.py add --bump patch --summary "Describe the user-facing change."
```

Use `patch` for backward-compatible fixes, `minor` for backward-compatible
features, and `major` for breaking changes. The generated `.changeset/*.md`
file contains the selected bump and changelog text and should be committed with
the pull request. If a pull request intentionally has no release or changelog
impact, record that decision explicitly:

```bash
uv run python scripts/release_changesets.py add --empty
```

Before opening or updating the pull request, verify both its release intent and
the repository's release metadata:

```bash
uv run python scripts/release_changesets.py status --since origin/main
uv run python scripts/release_changesets.py check
```

After changes land on `main`, automation collects pending fragments into a
Version Packages pull request. The highest pending bump determines the next
version. That pull request updates `pyproject.toml`, regenerates `uv.lock`, adds
the release section to `CHANGELOG.md`, and consumes the fragments. Merging it
lets the existing auto-tag workflow create the matching `vX.Y.Z` tag.

`pyproject.toml` is the sole editable version manifest. Do not add or update a
`VERSION` file, edit `uv.lock` by hand, or manually add generated release
sections to `CHANGELOG.md`.

### Release automation token

The Version Packages workflow uses a fine-grained GitHub personal access token
so that its branch pushes and pull-request updates can trigger the repository's
normal automation. Create a token restricted to this repository with these
repository permissions:

- **Contents:** Read and write
- **Pull requests:** Read and write

Add the token under **Settings → Secrets and variables → Actions** as a
repository secret named `CHANGESETS_TOKEN`. The workflow passes that secret
only to checkout, authenticated Git operations, and the GitHub CLI. Never put
the token in a tracked file, command output, changeset fragment, or pull-request
description.

If the token belongs to an organization account, complete any required SSO
authorization and ensure the organization permits fine-grained personal access
tokens. Token expiration or revoked access will prevent the workflow from
pushing `changeset-release/main` or creating and updating the Version Packages
pull request.

## License

See LICENSE for details.
