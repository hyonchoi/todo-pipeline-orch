# AI Agent Instructions

Project-specific instructions for Claude Code in this repository.

## Project

`todo-pipeline-orchestrator` — a uv-managed Python package modularizing `pipeline_watcher.py`,
plus a TODOS.md management skill. See [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md)
for the full plan.

## Tooling

- Python 3.12+, managed via `uv`.
- Use `uv sync` / `uv run` / `uv add` for dependency and execution management.

## CRITICAL: Python-native release metadata

`pyproject.toml` is the sole version manifest. Feature pull requests add release
intent under `.changeset/`; they do not edit the version or generated changelog.
Use `uv run python scripts/release_changesets.py add` to create a fragment and
`uv run python scripts/release_changesets.py check` to verify that
`pyproject.toml`, `uv.lock`, and `CHANGELOG.md` agree. The automated Version
Packages pull request consumes fragments, selects the highest semantic-version
bump, regenerates `uv.lock`, and updates `CHANGELOG.md`.

## TODOS.md management

- Use the `todos-manager` skill for all TODOS.md mutations (add, convert, audit, archive).
- TODOS.md format is enforced — see `## Metadata`, `## Entry Schema`, and `## Entries` in TODOS.md for schema rules and tracked `NEXT_TODO_ID` state.
- Skill source: `hermes_pipeline/data/skills/todos-manager/SKILL.md`. Install via `tpo skills install --target all` to copy to `~/.claude/skills/todos-manager/` and/or `~/.agents/skills/todos-manager/`.
- Subcommands: `--add` (new entry), `--init` (new sectioned project), `--convert` (migrate to canonical sections + validate), `--audit` (format check + tracked ID reconciliation), `--archive` (move `[x]` to TODOS-archive.md), `--list` (show active entries, `--all` includes archived), `--revise` (fill missing or weak fields with AI-pre-filled suggestions).

## Document management for gstack and superpowers

### Commit the docs on finalized.

md files under `docs/gstack/**` and `docs/superpowers/**` must commit on finalize.

Commit the md files when:

- Changed to APPROVED in /office-hours gstack skill
- Finalized after plan-eng-review or autoplan gstack skill
- Finalized after writing-plan superpowers skill

## gstack project folder reference

`docs/gstack` is the canonical project document folder. gstack skills resolve a project
`<slug>` (e.g. `todo-pipeline-orchestrator`) and look for `~/.gstack/projects/<slug>`.
If that path doesn't exist, create it as a symlink pointing to `docs/gstack`.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec

## Agent skills

### Issue tracker

Issues live in GitHub Issues (`hyonchoi/todo-pipeline-orch`), managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
