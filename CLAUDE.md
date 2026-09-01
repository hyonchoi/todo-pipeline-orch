# AI Agent Instructions

Project-specific instructions for Claude Code in this repository.

## Project

`todo-pipeline-orchestrator` — a uv-managed Python package modularizing `pipeline_watcher.py`
that runs pipeline ticks against a GitHub Issues backlog. See
[docs/pipeline-modularization-plan.md](docs/pipeline-modularization-plan.md) for the historical plan.

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

## Backlog management

- The backlog is GitHub Issues carrying `tpo:todo` (ADR-0003). The issue number is the TODO ID (`TODO-<issue-number>`); there is no `TODOS.md`.
- File TODOs through the "TPO TODO" issue form (`gh issue create --web --template "TPO TODO"`); scripted creation renders the body with `render_issue_body` and passes `--body-file`. See `docs/howto-github-issues-todos.md`.
- Label vocabulary, body contract, and eligibility rules: `docs/agents/issue-tracker.md` (`## TPO backlog items`). Bootstrap labels with `tpo todos labels sync <project>`; decisions live in the body and labels are mirrors — normalize with `tpo todos audit <project> --fix`.
- `Plan` is the execution authority (ADR-0001): never edit a TODO's Plan except through an explicitly approved, diff-confirmed change validated with `tpo plan validate <project> --todo N --require-manifest` (omit `--require-manifest` for a legacy manifest-free Plan).

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
