# todo-pipeline-orchestrator

Pipeline watcher and TODOS manager orchestration toolkit, packaged as a uv-managed Python project.

## Status

Early scaffolding. See [docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md) for the
modularization plan covering:

1. Turning `pipeline_watcher.py` into a reinstallable/upgradeable uv/pip package.
2. A TODOS.md authoring/management skill (gstack-format based, with predefined key decisions).

## Requirements

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
```

## Run

```bash
uv run main.py
```
