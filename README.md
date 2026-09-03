# todo-pipeline-orchestrator

Pipeline orchestration toolkit for a GitHub Issues backlog, packaged as a uv-managed Python project. The installed CLI is `tpo`.

## Overview

`tpo` selects TODOs from GitHub Issues (`tpo:todo`; the issue number is the TODO ID), keeps their labels consistent, prepares pipeline contracts, and runs pipeline ticks through PR handoff and issue closeout. Pipeline phases run through Hermes agent profiles and use kanban tasks for phase scheduling.

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

For pipeline execution, install **Hermes >= 0.19.0** on `PATH` and configure an
agent runtime/profile. Provider authentication is model-specific and is not a
baseline `tpo` prerequisite. `tpo doctor <project>` verifies the version floor,
profile prerequisites, GitHub auth, the label vocabulary, run registrations,
and Plan readiness for Plan-required profiles.

Install the bundled pipeline profile when you want unattended pipeline phase execution:

```bash
tpo install-profile
```

### Backlog lives in GitHub Issues

The TODO backlog is the project's GitHub Issues (`gh` >= 2.44 must be on
`PATH` and authenticated). A TODO is an open issue carrying `tpo:todo`; its ID
is `TODO-<issue-number>`. Create the label vocabulary once per repository, then
file TODOs through the "TPO TODO" issue form:

```bash
cd ~/my-projects/my-project
tpo todos labels sync my-project
gh issue create --web --template "TPO TODO"
```

The "TPO TODO" form is repository-local: copy `.github/ISSUE_TEMPLATE/tpo-todo.yml`
from this repository into `<project>/.github/ISSUE_TEMPLATE/` and commit it (or
use the scripted `render_issue_body` + `--body-file` path). `tpo todos audit`
reads the project's copy for the allowed Phase options.

See [Manage TODOs as GitHub Issues](docs/howto-github-issues-todos.md) for
filing, triage, audit, dependencies, and completion.

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
| `native-sdd` | `ai-coding-agents` | hermes | `Hermes skill registry` / `claude -p` | `Hermes skill registry` / `codex exec` | Conditional |

See [agent client release qualification](docs/release-qualification-agent-clients.md)
for the evidence required to advertise a `Conditional` pair.

Use `tpo init <project> --profile native-sdd` for the compiled workflow:
Hermes cron invokes TPO, TPO selects an eligible TODO and pins its embedded Plan
from the issue snapshot, then Hermes Kanban dispatches workers. A `tpo-plan`
manifest produces one visible worker card and controller gate per ordered task;
a legacy Markdown Plan remains compatible as one development card with a
doctor warning. Independent review, bounded review-fix rounds, PR closeout, and
the human merge gate remain visible on the board. It does not require gstack,
superpowers, or client-side workflow skills. See the
[native SDD profile guide](docs/howto-native-sdd-profile.md).

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

Start a new project (a clone of a github.com repository under `projects_dir`):

```bash
cd ~/my-projects/my-project
tpo init my-project
tpo todos labels sync my-project
# copy .github/ISSUE_TEMPLATE/tpo-todo.yml from this repository into the project and commit it
tpo skills install todo-manager --target codex --scope user
# use the todo-manager skill to preview and approve `tpo todos create`
tpo doctor my-project
```

Adopt an existing project by creating one issue per backlog item through the
same form (or scripted via `render_issue_body` + `--body-file`), triaging the
ones that are ready, and running `tpo todos audit my-project --fix` to align
the mirror labels. A legacy `TODOS.md` is not read by the pipeline; see the
[migration notes](docs/migration/todos-to-issues.md) for how the previous
backlog was converted and how `legacy-id:TODO-<n>` labels preserve old IDs.

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
| `doctor` | Verify a project's pipeline contract, GitHub backlog state, and run registrations. |
| `todos complete` | Close a delivered TODO issue by hand after its pull request merged. |
| `todos create` | Preview, create, or resume a validated TODO with an embedded Plan. |
| `todos labels sync` | Create the missing pipeline label vocabulary in the project's repository. |
| `todos audit` | Check TODO issue bodies against the backlog contract and normalize mirror labels. |
| `skills install/uninstall/recover` | Transactionally manage the bundled `todo-manager` skill. |
| `plan validate` | Validate a TODO's Plan attachment and optional `tpo-plan` manifest. |
| `install-profile` | Install or refresh the bundled pipeline Hermes profile. |
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
| [Manage TODOs as GitHub Issues](docs/howto-github-issues-todos.md) | How-to | Bootstrapping labels, filing, Plan readiness, triage, audit, dependencies, completion |
| [Issue tracker conventions](docs/agents/issue-tracker.md) | Reference | Label vocabulary, issue body contract, and eligibility rules |
| [TODOS.md to GitHub Issues migration](docs/migration/todos-to-issues.md) | Reference | Legacy ID mapping and what the migration changed |

### Pipeline setup and operation

| Doc | Type | When to read |
|---|---|---|
| [Run a manual tick](docs/howto-pipeline-tick.md) | How-to | Running `tpo tick` for iterative development |
| [Set up the pipeline profile](docs/howto-pipeline-profile.md) | How-to | Installing the dedicated pipeline Hermes profile |
| [Debug ticks and recover runs](docs/howto-debugging-and-recovery.md) | How-to | Using `--verbose`, `--debug`, run markers, and issue-state recovery |
| [Handle phase 5 review outcomes](docs/howto-review-outcomes.md) | How-to | Inspecting review artifacts and reverted/timed-out reviews |

### Multi-project configuration

| Doc | Type | When to read |
|---|---|---|
| [Configure `.hermes/config.toml`](docs/howto-config-toml.md) | How-to | Tuning selection model or circuit-breaker thresholds |
| [Set up multiple projects](docs/howto-multi-project-setup.md) | How-to | Configuring per-project settings and the scan loop |
| [Multi-project scan tutorial](docs/tutorial-multi-project-scan.md) | Tutorial | Setting up two projects and running the scan loop |
| [How the scan loop works](docs/explanation-multi-project-scan.md) | Explanation | Global lock, migration decisions, and trade-offs |

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
| [Mock integration test harness](docs/howto-mock-integration-test-harness.md) | How-to | Running `tpo test` against mock project data |
| [Harness production-code coverage checklist](docs/checklist-harness-production-coverage.md) | Reference | Acceptance criteria for production-code path reuse |

### Architecture and reference

| Doc | Type | When to read |
|---|---|---|
| [Architecture overview](docs/ARCHITECTURE.md) | Explanation | Understanding lane structure, data flow, and phase execution |
| [Pipeline state machine](docs/hermes-state-machine.md) | Explanation | Understanding `.hermes/` file layout and transitions |
| [Modularization plan](docs/pipeline-modularization-plan.md) | Explanation | Historical architecture and design plan |
| [Kanban-as-Scheduler](docs/reference-kanban-as-scheduler.md) | Reference/Explanation | How `tpo tick` uses kanban for phase state and ordering |
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
