# Harness Runtime Artifact Isolation

**Date:** 2026-07-30  
**Status:** Approved for planning

## Problem

`run_harness()` currently uses one temporary directory for two responsibilities:

1. the mock Git project that external agents modify; and
2. harness-owned telemetry, reports, and orchestration state.

This makes a retained `--keep` fixture noisy and misleading:

- `events.jsonl` appears at the mock project root;
- `reports/` appears as an untracked project directory;
- compatibility and terminal-only files remain under the project's `.hermes/`;
- agent scratch directories and Python caches obscure the delivered project.

Ignoring these paths in the mock project's `.gitignore` hides some Git status
noise but does not establish correct ownership or produce a clean retained
fixture.

## Goals

- Make the retained mock repository contain only project-owned files.
- Preserve raw events and generated reports for harness diagnosis.
- Preserve production parity while the pipeline is running.
- Stop creating obsolete compatibility state.
- Keep `--keep`, loop reporting, phase filtering, and report generation
  behavior understandable and testable.

## Non-goals

- Change production pipeline state ownership.
- Change phase semantics, prompt rendering, Hermes task registration, or
  report contents.
- Clean arbitrary files created by external agents.
- Redesign the general configuration system.

## Directory Model

Each harness invocation owns a workspace rather than treating the workspace
itself as the mock project:

```text
harness-<suffix>/
  project/                 # Git repository external agents modify
    .git/
    .hermes/
      pipeline.toml        # Project-owned execution contract
      ...                  # Runtime state while the run is active
    TODOS.md
    ...
  artifacts/               # Harness-owned diagnostic output
    events.jsonl
    reports/
      report.json
      report.md
```

`create_mock_project()` receives `workspace / "project"`. All project-facing
operations use that path. The event monitor and report generator use paths
under `workspace / "artifacts"`.

The CLI continues to print the report path and retained workspace path. A
retained run therefore exposes both the final project and its diagnostics
without mixing their ownership.

## Runtime State Lifecycle

The project keeps `.hermes/pipeline.toml` because it is the current execution
contract read at tick start.

The fixture factory stops creating `.hermes/todo_id_counter`. Canonical
`NEXT_TODO_ID` metadata in `TODOS.md` owns ID allocation; the counter is only
a legacy compatibility cache and is not needed to bootstrap this fixture.

During execution, the harness may create production-parity state under
`project/.hermes`, including:

- `pipeline_branch.txt`;
- `tpo-config.yaml`;
- empty `outcomes/`;
- `pipeline_checkpoints/`;
- `ready_for_review/`.

After report generation and all final status reads, retained runs prune only
known terminal harness state:

- `pipeline_branch.txt`;
- `tpo-config.yaml`;
- `outcomes/`;
- empty `pipeline_checkpoints/`;
- empty `ready_for_review/`.

The cleanup is allowlisted. It does not recursively remove unknown `.hermes`
content, and it preserves non-empty outcome, checkpoint, or review directories
so failure evidence is not lost.

Agent scratch directories and Python bytecode are excluded from this cleanup:
they are not owned by the harness, and deleting arbitrary agent-created
content would hide useful evidence. Because the Git project now lives below
the workspace, such project-local output remains visible as project output
rather than being confused with harness telemetry.

## Error Handling

Workspace cleanup follows the existing `keep_dir` contract:

- without `--keep`, the entire workspace is removed in the existing finalizer;
- with `--keep`, the workspace remains and the allowlisted terminal-state
  cleanup runs after reports and final status have been produced.

Failure to prune one terminal artifact is reported as a warning and does not
replace the harness result. The warning names the exact path. Non-empty
evidence directories are deliberately retained without warning.

## Loop Mode

Numbered loop reports remain harness-owned. They are stored under
`workspace / "artifacts"` alongside `events.jsonl` and `reports/`, rather than
inside the project. Report comparison behavior and numbering remain
unchanged.

## Tests

Tests will establish the new boundary before implementation:

1. `create_mock_project()` does not create `.hermes/todo_id_counter`.
2. A harness run passes `workspace/project` to project-facing operations.
3. Events and reports are written under `workspace/artifacts`, outside the
   Git repository.
4. Loop reports are stored under the artifacts directory.
5. Retained-run cleanup removes allowlisted files and empty directories.
6. Retained-run cleanup preserves `.hermes/pipeline.toml`, unknown files, and
   non-empty evidence directories.
7. The retained mock project's Git status contains no harness telemetry or
   report paths.

Focused harness tests run first, followed by the complete locked project test
suite.

## Documentation

The mock harness how-to and CLI/reference material that describe retained
directory layouts will be updated to show the `project/` and `artifacts/`
boundary. Examples will use `~/.hermes/tmp`, matching the current runtime
location.

## Acceptance Criteria

- A newly retained happy-path harness workspace has separate `project/` and
  `artifacts/` directories.
- No `events.jsonl` or `reports/` entry exists inside the mock Git repository.
- The fixture factory does not seed `.hermes/todo_id_counter`.
- `.hermes/pipeline.toml` remains available to the pipeline.
- Known terminal state is pruned only after its consumers finish.
- Unknown or non-empty failure evidence is never deleted by retained-run
  cleanup.
- Harness-focused and complete project tests pass.
