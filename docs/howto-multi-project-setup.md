# Multi-Project Setup

`tpo tick` scans your projects directory and runs selection for
every active project in one cron execution. This howto covers setting up
multiple projects for the scan loop.

## Prerequisites

- All projects live under a single directory (default: `~/projects`).
- Each project has a pipeline contract at `.hermes/pipeline.toml` (`tpo init <project>`)
  and a github.com `origin` remote. The backlog itself lives in GitHub Issues
  (`tpo:todo` label; canonical ID `TODO-<issue-number>`) — see
  [issue tracker](agents/issue-tracker.md#tpo-backlog-items) and
  [ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md).
  Directories with `.hermes/` or a legacy `TODOS.md` but no contract are skipped
  with a WARNING suggesting `tpo init`.
- Project directory names are valid slugs (alphanumeric, dot, dash, underscore; no leading dash or dot).

## Configuration

### Setting the Projects Directory

If your projects live outside `~/projects`, set `projects_dir` in the global
config file:

```bash
tpo config init
tpo config set projects_dir /path/to/your/projects
```

For one-off runs, point `TPO_CONFIG_FILE` at an alternate complete config file.
`.hermes/config.toml` is per-project and does not set the global scan directory.

### Per-Project Configuration

Create `.hermes/project.toml` in a project directory:

```bash
mkdir -p ~/projects/myproject/.hermes
cat > ~/projects/myproject/.hermes/project.toml << 'EOF'
[active]
enabled = true

[notifications]
slack_channel = "project__myproject"
EOF
```

### Archiving a Project

To pause selection for a project without deleting its contract:

```bash
mkdir -p ~/projects/myproject/.hermes
cat > ~/projects/myproject/.hermes/project.toml << 'EOF'
[active]
enabled = false
EOF
```

The next tick will skip this project.

### Slack Channel Resolution

Alerts for each project go to the Slack channel determined by:
1. `project.toml`'s `[notifications] slack_channel`
2. Global config `slack_channel`
3. `#alert` (hardcoded fallback)

## Cron Setup

Replace the per-project cron entry with a single global entry. Use Hermes cron
(recommended) or system crontab:

```bash
# Hermes cron: one entry for all projects
hermes cron set pipeline-tick '*/5 * * * *'
```

The old `scripts/install-cron.sh` is deprecated — it still registers `tpo auto`
(which was removed in v0.2.0). Use Hermes cron instead.

## Per-Project State

Each project keeps its own state under `<project>/.hermes/`; `~/.hermes/` holds
only global configuration. The legacy automatic migration of global state into
the first project was removed.

## Debugging

To debug a specific project's selection:
1. Set all other projects to `enabled = false` in their `.hermes/project.toml`
2. Run `tpo tick --debug`
3. Restore other projects' `enabled = true`

## Error Isolation

If one project's issue tracker is unreachable (recorded as a `tracker_error`
decision) or an error occurs during selection,
the error is logged and the scan continues to the next project. One project's
failure does not block the others.

## Related

- [Multi-project scan tutorial](tutorial-multi-project-scan.md) — step-by-step walkthrough with two projects
- [How the scan loop works](explanation-multi-project-scan.md) — per-project locking and discovery decisions
- [Issue tracker conventions](agents/issue-tracker.md) — labels and eligibility for `tpo:todo` issues
