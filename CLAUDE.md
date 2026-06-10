# CLAUDE.md

Project-specific instructions for Claude Code in this repository.

## Project

`todo-pipeline-orchestrator` — a uv-managed Python package modularizing `pipeline_watcher.py`,
plus a TODOS.md management skill. See [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md)
for the full plan.

## Tooling

- Python >= 3.14, managed via `uv`.
- Use `uv sync` / `uv run` / `uv add` for dependency and execution management.
