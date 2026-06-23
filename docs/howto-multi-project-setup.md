# Multi-Project Setup

`pipeline-watch tick` scans your projects directory and runs selection for
every active project in one cron execution. This howto covers setting up
multiple projects for the scan loop.

## Prerequisites

- All projects live under a single directory (default: `~/projects`).
- Each project has a `TODOS.md` file.
- Project directory names are valid slugs (alphanumeric, dot, dash, underscore; no leading dash or dot).

## Configuration

### Setting the Projects Directory

If your projects live outside `~/projects`, set the environment variable:

```bash
export PIPELINE_PROJECTS_DIR=/path/to/your/projects
```

Or set it in `~/.hermes/config.toml`:

```toml
projects_dir = "/path/to/your/projects"
```

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

To pause selection for a project without deleting `TODOS.md`:

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
2. `PIPELINE_SLACK_CHANNEL` environment variable
3. `#alert` (hardcoded fallback)

## Cron Setup

Replace the per-project cron entry with a single global entry. Use Hermes cron
(recommended) or system crontab:

```bash
# Hermes cron: one entry for all projects
hermes cron set pipeline-tick '*/5 * * * *'
```

The old `scripts/install-cron.sh` is deprecated — it still registers `pipeline-watch auto`
(which was removed in v0.2.0). Use Hermes cron instead.

## State Migration

On the first run of `pipeline-watch tick` (no project argument), state files
in `~/.hermes/` (`current_tick_id.txt`, `circuit.json`, `outcomes/`) are
migrated to `<project>/.hermes/`. This is a one-time operation.

**Important:** Auto-migration only runs when exactly one project is discovered.
If multiple projects exist, the tick warns and skips migration — you need to
manually decide which project owned the global state. With multiple projects,
each project starts with a fresh state directory.

## Debugging

To debug a specific project's selection:
1. Set all other projects to `enabled = false` in their `.hermes/project.toml`
2. Run `pipeline-watch tick --debug`
3. Restore other projects' `enabled = true`

## Error Isolation

If one project's `TODOS.md` is malformed or an error occurs during selection,
the error is logged and the scan continues to the next project. One project's
failure does not block the others.

## Related

- [Multi-project scan tutorial](tutorial-multi-project-scan.md) — step-by-step walkthrough with two projects
- [How the scan loop works](explanation-multi-project-scan.md) — why single global lock, state migration decisions
- [How to troubleshoot state migration](howto-troubleshoot-state-migration.md) — fixing migration issues
