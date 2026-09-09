# Agent Instructions

Project-specific architecture, tooling, workflow, and reporting instructions
for coding agents working in this repository.

This file is the canonical, hand-maintained source for repository instructions.
`CLAUDE.md` imports it; edit this file when updating shared guidance. Neither
repository file is generated or a symlink.

## Project

`todo-pipeline-orchestrator` is a Python 3.12+ package that orchestrates a
GitHub Issues TODO backlog through Hermes agent selection and Kanban
execution. The CLI is `tpo`, the distribution is `hermes-pipeline`, and the
Python import package is `hermes_pipeline`. `native-sdd` is the default profile
for contracts written by `tpo init`; see ADR-0004 and the profile guides for
other supported workflows.

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
  Keep automated tests deterministic and provider-free; optional local live
  Hermes/provider checks follow the guidance below.
- Distinguish mocked/provider-free evidence from live Hermes, OAuth, quota, or
  finite external-resource validation in the completion report.

### Optional local Hermes and agent smoke tests

- When useful, available, and authorized, use the existing local Hermes/agent
  environment for optional smoke tests. These checks require no special setup:
  do not install tools or reconfigure authentication or the environment just to
  run them. Skip when unavailable, unauthorized, or unnecessary for the change.
- The [installed-Hermes registration contract test](README.md#contributing)
  is one existing opt-in check. It uses temporary test state and dry-run
  dispatch without model/provider execution; it does not establish live agent
  or provider behavior. Keep any live checks within the authorized scope and
  the security and external-process boundaries below.
- In the PR description's **Validation** section, report each optional check as
  **passed**, **failed**, **inconclusive**, or **skipped**, with its command (if
  run), scope, and limitations or reason. Distinguish provider-free checks from
  live agent/provider execution; do not infer live success from a dry run.
- Update the PR description when results arrive later. Reporting these results
  alone does not require amending commits or qualification records.
- These optional checks never gate releases. Formal agent-client release
  qualification remains governed by
  [its protocol](docs/release-qualification-agent-clients.md), including its
  separate evidence and release-finalization requirements; optional smoke
  reports do not replace that qualification.

## Change discipline

- Trace the current call path before changing behavior. Treat historical plans
  and generated review documents as context, not proof of current behavior.
- Preserve backward compatibility unless the task explicitly requires a
  breaking change.
- Do not add dependencies unless the standard library and existing dependencies
  cannot reasonably solve the problem.

## Backlog management

- The backlog is GitHub Issues carrying `tpo:todo`
  (`docs/adr/0003-github-issues-are-the-todo-backlog.md`). Create executable
  TODOs with `tpo todos create <project> --request-file <request.json>` using
  the private request-file contract in `docs/howto-github-issues-todos.md`.
  Review and explicitly approve the canonical preview before creation;
  automation may use `--yes --approved-repo OWNER/REPO` only after that approval.
  The "TPO TODO" issue form or `gh issue create --web --template "TPO TODO"`
  remains an alternative for filing issues.
- The issue number is the ID (`TODO-<issue-number>`); legacy `TODO-<n>` IDs
  from the retired `TODOS.md` live in `legacy-id:` labels and are never reused.
- The label vocabulary and issue body contract are defined in
  `docs/agents/issue-tracker.md` and `hermes_pipeline/github_issues.py`.
  Decisions live in the body; labels are mirrors. Bootstrap labels with
  `tpo todos labels sync <project>` and normalize with
  `tpo todos audit <project> --fix`.
- `Plan` is the execution-authority field
  (`docs/adr/0001-plan-is-the-execution-authority.md`). Do not make another
  attachment field implicitly actionable. Change a TODO's Plan only through
  an explicitly approved, diff-confirmed edit, and validate Plan changes with
  `tpo plan validate <project> --todo N --require-manifest` (omit
  `--require-manifest` for a legacy manifest-free Plan).

## Version and changelog synchronization

`pyproject.toml` is the sole version manifest. Every pull request must add
release intent under `.changeset/`:

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

- `docs/gstack/` is the canonical gstack project-document directory. If
  `~/.gstack/projects/todo-pipeline-orchestrator` is needed and absent, point it
  to `docs/gstack` with a symlink.
- Finalized Markdown produced under `docs/gstack/**` or
  `docs/superpowers/**` is a project artifact and should be included with the
  associated finalized work. This includes approved office-hours documents and
  plans finalized by plan-review/autoplan/writing-plan workflows.

## Skill routing

Use skills available in the current harness when their documented triggers
apply, through that harness's supported invocation mechanism. Do not assume
gstack, superpowers, or a particular Skill tool is installed.

For issue, triage, and domain conventions, follow `docs/agents/issue-tracker.md`,
`docs/agents/triage-labels.md`, and `docs/agents/domain.md`.

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

If a standard gate is skipped or blocked, say why.
