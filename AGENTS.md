# Agent Instructions

Project-specific instructions for coding agents working in this repository.
Keep changes narrow, preserve unrelated worktree changes, and follow existing
architecture and style. Ask only when ambiguity would materially change the
result; otherwise state a reasonable assumption and continue.

## Project

`todo-pipeline-orchestrator` is a Python 3.12+ package that orchestrates
schema-enforced `TODOS.md` workflows through Hermes agent selection and Kanban
execution. The installed CLI and Python package are named `tpo` and
`hermes-pipeline`, respectively.

Start with these sources when relevant:

- `README.md` for supported user workflows and CLI behavior.
- `docs/ARCHITECTURE.md` for the current runtime design.
- `docs/adr/` for binding architectural decisions.
- `docs/agents/` for issue, triage, and domain conventions.
- `docs/pipeline-modularization-plan.md` for historical modularization context;
  verify current behavior in code before relying on the plan.

## Tooling and verification

- Use `uv sync`, `uv run`, and `uv add`; do not use bare `pip` for project
  dependency management.
- Run focused tests while iterating, then the full relevant gates before
  finishing.
- The standard full gates are:

  ```bash
  uv run pytest
  uv run ruff check .
  ```

- CI runs pytest across supported Python versions and Ruff. There is no
  separate formatter, type-checker, or compile gate configured; do not claim
  those checks passed unless you actually ran an applicable command.
- For CLI or packaging changes, also run the narrow smoke checks relevant to
  the change, such as `uv run tpo --version` or `uv build`.
- Prefer regression tests for bug fixes and behavior tests for public changes.
  Keep tests deterministic and provider-free unless live Hermes/provider
  validation is explicitly required.
- Distinguish mocked/provider-free evidence from live Hermes, OAuth, quota, or
  finite external-resource validation in the completion report.

## Change discipline

- Implement every approved plan in a dedicated linked Git worktree under
  `.worktrees/<task-slug>`. Create or select that worktree before running the
  first TDD red test or making any implementation change.
- Resolve the repository and existing worktrees with `git worktree list`. Do
  not implement an approved plan in the primary checkout.
- If the active checkout is already the correct linked worktree under
  `.worktrees/`, continue there; do not create a nested worktree.
- Preserve unrelated changes and do not remove the implementation worktree
  unless the user explicitly requests cleanup.
- Inspect the active branch, worktree, and dirty state before editing. Preserve
  unrelated tracked and untracked changes.
- Trace the current call path before changing behavior. Treat historical plans
  and generated review documents as context, not proof of current behavior.
- Prefer the smallest complete change. Do not reformat, rename, reorganize, or
  refactor unrelated code.
- Preserve backward compatibility unless the task explicitly requires a
  breaking change.
- Do not add dependencies unless the standard library and existing dependencies
  cannot reasonably solve the problem.
- Use test-driven development for every implementation request, including direct
  implementation requests and implementation following Plan mode or a reviewed
  plan. Divide the work into atomic TDD tasks. For each task, first add or change
  an automated test that expresses the required behavior and run it to establish
  the expected failure (red); make the smallest production change that passes
  the test (green); then refactor without changing behavior and rerun the
  task-focused checks. Do not write production code before its failing test.
- Treat each completed red-green-refactor task as an authorized commit boundary.
  Stage only the files or hunks belonging to that task and create one atomic
  commit before starting the next task; no additional commit request is needed.
  Do not commit an incomplete task or known failing tests unless the user
  explicitly requests a checkpoint commit. An explicit user instruction not to
  commit overrides this automatic authorization.
- For documentation, metadata, or other changes that cannot meaningfully execute
  in a unit test, first add or identify the narrowest automated validation that
  can fail on the intended contract, then follow the same red-green-refactor and
  commit cycle. If no automated red test is technically possible, stop and ask
  the user before implementing that task.
- Do not push, merge, delete worktrees, or open/update a pull request unless the
  user requests that action.

## `TODOS.md` management

- Use the `todos-manager` skill for every `TODOS.md` mutation: add, initialize,
  convert, audit, archive, list, or revise.
- The canonical skill source is
  `hermes_pipeline/data/skills/todos-manager/SKILL.md`. Follow it rather than
  manually reproducing its parsing or mutation workflow.
- `TODOS.md` owns schema rules and the tracked `NEXT_TODO_ID`. Assigned
  `TODO-<n>` IDs are stable and must not be renumbered or reused.
- `Plan:` is the execution-authority field. Do not make another attachment
  field implicitly actionable.
- Install skill copies with `tpo skills install --target all`, or use a scoped
  target when the task requires it. When changing the bundled skill or install
  behavior, verify source behavior, install parity, and the skill test fixtures
  that cover the affected workflow.

## Version and changelog synchronization

Every pull request must add release intent under `.changeset/`:

- Use `uv run python scripts/release_changesets.py add --bump patch|minor|major
  --summary "..."` and put the pull request's user-facing changelog text in
  the generated Markdown file.
- Use `uv run python scripts/release_changesets.py add --empty` only when the
  pull request intentionally has no release or changelog impact.

Do not manually bump versions or add generated release sections to
`CHANGELOG.md`. The Version Packages pull request consumes pending
`.changeset/*.md` files, updates `pyproject.toml`, regenerates `uv.lock`, and
updates `CHANGELOG.md`. Consumed fragments remain available in git history.

Verify release metadata with:

```bash
uv run python scripts/release_changesets.py check
```

## Documentation and generated plans

- Update user-facing documentation only when the requested change warrants it.
  Put release notes in the pull request's changeset fragment; do not edit the
  generated `CHANGELOG.md` directly. Avoid documentation churn for
  internal-only edits.
- `docs/gstack/` is the canonical gstack project-document directory. If
  `~/.gstack/projects/todo-pipeline-orchestrator` is needed and absent, point it
  to `docs/gstack` with a symlink.
- Finalized Markdown produced under `docs/gstack/**` or
  `docs/superpowers/**` is a project artifact and should be included with the
  associated finalized work. This includes approved office-hours documents and
  plans finalized by plan-review/autoplan/writing-plan workflows.
- Do not treat draft planning documents as runtime authority.

## Security and external-process boundaries

- Never hardcode or persist credentials, tokens, authorization headers/codes,
  raw OAuth/provider bodies, raw exception strings from providers, or complete
  rejected payloads.
- Do not weaken TLS, authentication, validation, path-containment checks, or
  subprocess argument boundaries.
- Use bounded timeouts and preserve cancellation/recovery semantics when
  changing Hermes or Kanban subprocess flows. A quota or license-capacity
  failure is an external gate, not automatically a code failure.

## Completion report

Finish with a concise report containing:

- Summary
- Files changed
- Tests/checks actually run and their outcomes
- Assumptions, if any
- Remaining risks or unverified live gates, if any
- Suggested Conventional Commit message

Do not claim checks that were not run. If a standard gate is skipped or blocked,
say why.
