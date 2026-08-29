# How to debug pipeline ticks and recover runs

This guide covers the tools you use when a tick doesn't behave the way you expect or when a run's issue registration needs operator attention.

- **`--verbose`** — add informational details (selection results, lock state) without noise
- **`--debug`** — surface internal state (agent call summaries, circuit breaker transitions, kanban payloads)
- **Run recovery** — handle unsupported registrations, abandon a run, and clear stale issue labels

## Prerequisites

- `tpo` installed and configured (see the [getting-started tutorial](tutorial-getting-started.md))
- `tpo config get projects_dir` points at a directory containing projects with `.hermes/pipeline.toml`
- `gh` >= 2.44 authenticated against each project's github.com `origin`

## Using `--verbose` for targeted log detail

The `--verbose` flag enables the `pipeline.verbose` logger, which outputs informational details at key points in the tick flow. Use this when you want to know what the pipeline is doing without the noise of full debug output.

```bash
uv run tpo --verbose tick my-project
```

**What you'll see (in addition to default INFO output):**

| Source | Message |
|--------|---------|
| Lock acquisition | `acquiring tick lock: lock_file=<path> tick_id=<id>` |
| Selection result | `selection result: picked=TODO-N rationale=...` |
| Lock release | `tick lock released: tick_id=<id>` |

These messages come from the `pipeline.verbose` logger, which is off by default and only active when `--verbose` is passed.

**Common use:** Verify which TODO was selected and why, without seeing the full agent payload.

## Using `--debug` for full diagnostics

The `--debug` flag lowers the root log level from INFO to DEBUG, surfacing internal state at multiple strategic points across the pipeline.

```bash
uv run tpo --debug tick my-project
```

**What you'll see (in addition to --verbose output):**

| Source | Message |
|--------|---------|
| Agent call | `agent request: prompt_sha=... prompt_chars=...` |
| Agent call | `agent response: prompt_sha=... response_chars=...` |
| Lock acquisition | `tick lock acquired: lock_file=<path> holder_pid=<pid>` |
| Selection | `selection decision: picked=TODO-N candidates=... rationale=...` |
| Circuit breaker | `circuit breaker observe: picked=... counts_as_no_progress=... state=...` |
| Circuit breaker | `circuit breaker: sending slack alert after N consecutive no-progress ticks` |
| Circuit breaker | `circuit breaker: backed off to N min interval` |
| Circuit breaker | `circuit breaker: resuming from backoff (was backed_off=True)` |
| Kanban | `kanban registration payload (raw JSON, truncated): ...` |

**Important:** Selection-agent prompt and response bodies are never logged.
Debug output reports only the prompt SHA and character counts. Kanban payloads
remain truncated at 500 characters.

**Common use:** Troubleshoot why a specific TODO was or wasn't selected, or why the circuit breaker tripped.

## Recovering runs and issue state

The backlog is GitHub Issues (`tpo:todo`; see
[issue tracker](agents/issue-tracker.md#tpo-backlog-items)). Run state
lives in `<project>/.hermes/runs/<tick-id>/`; the issue is the mirror TPO keeps
consistent. These are the operator-facing recovery paths.

### `REGISTRATION UNSUPPORTED` from `tpo doctor`

```
REGISTRATION UNSUPPORTED: schema_version 1; finish or abandon this run before upgrading
```

The active run was registered under the retired `TODOS.md` schema (v1). TPO
does not migrate it. Either let the run finish on the previous release, or
abandon it (below) and let the next tick select from GitHub Issues.

### Abandoning a run

Touch the `abandoned` marker in the run directory. The registration is kept for
audit but no longer pins its issue, so the next tick can select again:

```bash
touch ~/projects/my-project/.hermes/runs/<tick-id>/abandoned
```

Then remove the pinned worktree and branch, using the `worktree` and `branch`
values from that run's `registration.json` (`tpo doctor` prints both commands
on its `Fix (tick <id>):` line):

```bash
git worktree remove --force <worktree>
git branch -D <branch>
```

The counterpart marker `issue-closed` is written by closeout when the PR merged
and the issue was closed (`delivered`); never create it by hand.

### Stale `tpo:in-progress` label

TPO adds `tpo:in-progress` to the pinned issue while a run is active and removes
it at closeout. If a run was abandoned or its state directory was lost, the
label can linger and block re-selection. Remove it manually:

```bash
gh issue edit <N> --remove-label tpo:in-progress
```

Only do this once `tpo doctor <project>` reports no active run (no
`current_tick_id.txt`) for that issue.

### Pausing a TODO

Add `tpo:on-hold` to the issue. It is excluded from selection, and if the issue
is already registered the next tick flags `issue_on_hold` and the run stops at a
`needs_input` boundary. Remove the label to resume.

### Tracker error decisions

When `gh` or the `origin` remote is unusable, the tick persists a decision whose
rationale is `tracker_error: <code>` instead of calling the selection agent:

| Code | Meaning | Counts as no-progress |
|------|---------|-----------------------|
| `gh_missing` | `gh` not on PATH (or `TPO_GH_BIN` invalid) | no (config fault) |
| `gh_auth` | `gh auth status` failed | no (config fault) |
| `origin_identity_invalid` | `origin` is not a github.com remote | no (config fault) |
| other `gh` codes | transient API/network failure | yes |

Registered-run drift is reported by `tpo doctor` as `ISSUE DRIFT: <code>` with
`issue_drift`, `issue_closed`, `issue_on_hold`, or `issue_identity_mismatch`
(each a manual `needs_input` boundary), or as
`WARNING: issue check unavailable (<code>)` when the check itself could not run.

## Verification

After using any of these tools, verify the result:

- **`--verbose`/`--debug`:** Check the log output includes the expected detail level. Run `uv run tpo tick my-project` (no flag) and confirm no verbose or debug output appears.
- **Run recovery:** `tpo doctor` does not read the `abandoned` marker. Run the next `tpo tick <project>`, then `tpo doctor <project>`; it should print `Issue authority: pinned` (or no active run) instead of `REGISTRATION UNSUPPORTED` / `ISSUE DRIFT`.

## Troubleshooting

### Native-SDD recovery

Run `tpo doctor <project>` first. It reports the Hermes >= 0.19.0 requirement,
installed project-skill parity, and manifest/legacy/invalid Plan readiness.
Then inspect `.hermes/runs/<tick-id>/registration.json` and the affected cards
with `hermes kanban show <task-id> --json`.

If the expected repository, branch, worktree, base SHA, TODO hash, Plan hash,
PR head, or remote head differs from observed state, preserve both sides and
resolve the cause manually. TPO never resets, cleans, deletes, force-pushes, or
repairs a drifted worktree or branch. A fifth unsuccessful review-fix round is
also a manual `needs_input` boundary; automation does not create round six.

**"verbose output not showing up"**

- Make sure you pass `--verbose` before the subcommand: `uv run tpo --verbose tick my-project`
- The flags are global root-level arguments, positioned before the subcommand.

**"debug output not showing up"**

- Same as `--verbose`: use `uv run tpo --debug tick my-project`
- Debug logging is only available in the `tick` subcommand (the only subcommand that logs at DEBUG level)

**"`tpo tick my-project` exits 2 before selecting"**

- Run `tpo config get projects_dir`, then check that the reported directory contains `my-project/.hermes/pipeline.toml` (otherwise run `tpo init my-project`)
- Check that `git remote get-url origin` is a github.com URL and `gh auth status` succeeds
- Check that the project slug is valid (alphanumeric, dot, dash, underscore — no spaces or special characters)
