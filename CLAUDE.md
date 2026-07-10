# CLAUDE.md

Project-specific instructions for Claude Code in this repository.

## Project

`todo-pipeline-orchestrator` — a uv-managed Python package modularizing `pipeline_watcher.py`,
plus a TODOS.md management skill. See [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md)
for the full plan.

## Tooling

- Python 3.12+, managed via `uv`.
- Use `uv sync` / `uv run` / `uv add` for dependency and execution management.

## TODOS.md management

- Use the `todos-manager` skill for all TODOS.md mutations (add, convert, audit, archive).
- TODOS.md format is enforced — see preamble blockquote in TODOS.md for schema rules.
- Skill source: `skills/todos-manager/SKILL.md`. Install via `scripts/install-todos-manager.sh` to symlink to `~/.claude/skills/todos-manager/` and/or `~/.agents/skills/todos-manager/`.
- Subcommands: `--add` (new entry), `--init` (new project), `--convert` (add preamble + validate), `--audit` (format check), `--archive` (move `[x]` to TODOS-archive.md).

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

