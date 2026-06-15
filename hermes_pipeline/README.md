# hermes-pipeline

Autonomous TODOS.md pipeline orchestrator. See
`../docs/gstack/hyonchoi-main-design-20260610-195349.md` for the full design,
`../docs/superpowers/plans/2026-06-11-todo-pipeline-orchestrator.md` for the
v0.1 modularization plan, and
`../docs/superpowers/plans/2026-06-13-hermes-centric-selection.md` for the
v0.2 Hermes-centric selection plan (LLM-driven decision agent, immutable
decisions, outcome sidecars, circuit breaker, eval suite). Pipeline state
transitions live in `../docs/hermes-state-machine.md`.

## Install (dev)

    uv sync
    uv pip install -e ./hermes-pipeline

## Run

    pipeline-watch --help
