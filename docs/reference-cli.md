# CLI Reference

Complete reference for `tpo` subcommands.

The examples below use the installed `tpo` command. In a source checkout,
contributors can run the same commands as `uv run tpo ...`.

- `tpo <command> [args]` — Production pipeline orchestration (tick, init, doctor, ...)
- `tpo test [args]` — Live integration test harness (GitHub sandbox repository)

## tpo Global Flags

| Flag | Description |
|------|-------------|
| `--version` | Print version and exit |
| `--verbose` | Increased log detail: selection results, lock state, agent call summaries |
| `--debug` | Full debug logging: circuit breaker transitions, subprocess output |

Global flags apply before the subcommand: `tpo --verbose tick`.

## Subcommands

### `tick`

Run one pipeline tick: discover active projects, select TODOs, register kanban phases.

```bash
tpo tick              # scan all active projects
tpo tick myproject    # tick one project
```

**Flow per project:**
1. Load pipeline contract from `.hermes/pipeline.toml`
2. Check prior tick outcomes; observe circuit breaker
3. Check in-flight phase state and circuit breaker progress
4. Run Hermes agent selection over the eligible `tpo:todo` GitHub issues
5. Register kanban phases with `--parent` dependency chains; profiles may also define blocked, nonspawnable gates

Without a project argument, scans all subdirectories of the global
`projects_dir` for a `.hermes/pipeline.toml` contract; the backlog itself is
read from each project's GitHub Issues. Per-project locks isolate failures — one
project's held lock does not block others. Scan order rotates each tick for
fairness.

---

### `approve`

Legacy helper for existing ship-gate sidecars: bump version in PR, squash-merge to main, complete the ship gate.

```bash
tpo approve myproject --todo TODO-5
tpo approve myproject --todo TODO-5 --force --force
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--todo` | Yes | TODO to ship (e.g. `TODO-5` or `5`) |
| `--force` | No | Pass twice to bypass ONLY the SHA-staleness guard (audited) |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | Shipped successfully |
| 2 | Unexpected subprocess error |
| 3 | Refused by a guard (dirty tree, SHA mismatch, no ship sidecar) |

**Guards:**
- Dirty working tree: always refuses, cannot be bypassed
- PR head SHA changed since review: refuses unless `--force --force` (logged to `approve_audit.log`)
- CI not green: refuses; re-run once checks pass (bump commit already pushed)
- No legacy ship sidecar or gate task: refuses

---

### `init`

Write the default pipeline execution contract (`.hermes/pipeline.toml`) for a project.

```bash
tpo init myproject
tpo init myproject --force
tpo init myproject --assignee pipeline
tpo init myproject --profile agent-skills
tpo init myproject --profile native-sdd
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--force` | No | Overwrite an existing contract |
| `--assignee` | No | Set the assignee field (e.g. `--assignee pipeline`) |
| `--profile` | No | Pipeline workflow profile (`gstack`, `agent-skills`, or `native-sdd`). Default: `gstack`. Determines which `phases.yaml` and capabilities the contract uses. See [profile selection](howto-agent-skills-profile.md) and [native SDD](howto-native-sdd-profile.md). |

Capabilities are computed from `phases.yaml` at write time, not hardcoded.

---

### `doctor`

Verify a project's pipeline execution contract against `phases.yaml`, require
Hermes >= 0.19.0, check the GitHub backlog (auth, repository identity, label
vocabulary, Plan readiness), and inspect run registrations.

```bash
tpo doctor myproject
```

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | Clean; contract, GitHub checks, and the active registration all verified (legacy Plan warnings may still be present) |
| 1 | Drift: contract capability mismatch, registration or issue drift, or any `WARNING:`/`INVALID:` line from the GitHub checks |
| 2 | Missing/invalid contract, unsupported Hermes version, unknown project, missing Hermes profile, or an `Unverified` prerequisite |

If the contract assignee is non-default (e.g. `pipeline`), verifies the Hermes profile exists.

After the prerequisite and Hermes version lines, the GitHub checks print, in order:

| Line | Meaning |
|------|---------|
| `GitHub auth: ok` / `WARNING: GitHub auth unavailable (<code>)` | `gh auth status` against the project |
| `Repository: <owner>/<repo>` / `INVALID: repository identity: ...` | The github.com `origin` remote |
| `Label vocabulary: ok` / `INVALID: missing <labels>; Fix: tpo todos labels sync <project>` | Every pipeline label exists |
| `Plan readiness: eligible=N blocked=N (<reason>=N ...)` | Selectable `tpo:todo` issues and blocked reasons grouped by prefix (`dependency_incomplete`, `branch_invalid`, `plan_invalid`, ...) |
| `Runs: active=N delivered=N abandoned=N [unsupported=N]` | Run registrations under `.hermes/runs/`; `unsupported` counts malformed or schema v1 registrations |
| `tick <id> → #N` | Each active run and the issue it pins; a run that is not the current tick prints a `WARNING` and a `Fix` line (complete or abandon it, then `git worktree remove --force <worktree> && git branch -D <branch>`) |
| `tick <id> → #N (active; no current tick)` | An active run while no `current_tick_id.txt` exists |
| `tick <id>: unsupported or malformed registration` | One line per registration counted as `unsupported` |

Every GitHub check is offline-tolerant: a failure prints one `WARNING` line and
the remaining checks still run, but the exit code becomes 1.

For the current registration, `doctor` then prints `Issue authority: pinned`
when the pinned issue still matches, `ISSUE DRIFT: <code>` (`issue_drift`,
`issue_closed`, `issue_on_hold`, `issue_identity_mismatch`) when it does not,
`REGISTRATION UNSUPPORTED: schema_version 1 ...` for a run registered under the
retired TODOS.md schema, or `Current tick <id>: no registration (no TODO
selected)` when the last tick picked nothing. It compares the registered
repository, worktree, branch, base commit, and Plan bytes read from that
immutable base and reports the current head and clean/dirty lifecycle state
separately, so a valid later implementation or closeout commit is not
misreported as authority drift. See
[recovering runs and issue state](howto-debugging-and-recovery.md#recovering-runs-and-issue-state).

---

### `todos`

Manage the GitHub Issues backlog. All three subcommands run `gh` against the
project's `origin` and exit 2 for an unknown project.

#### `todos complete`

Run the idempotent issue-close state machine by hand for a delivered TODO
(closeout normally does this on the next tick after the PR merges):

```bash
tpo todos complete myproject --todo TODO-5 --pr 71
tpo todos complete myproject --todo 5 --pr 71 --date 2026-08-29 --force
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--todo` | Yes | Issue to close (`TODO-5` or `5`) |
| `--pr` | Yes | Merged pull request number |
| `--date` | No | Completion date `YYYY-MM-DD`; defaults to today (UTC) |
| `--force` | No | Proceed although the PR is not merged, a run for the issue is still active, or the issue was already closed against a different PR |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | `completed` — issue closed, marker comment present, `tpo:in-progress` removed |
| 1 | GitHub failure (`Error: <code>`) |
| 2 | Refused (PR not merged, run still active) or usage error |
| 3 | `pending` — GitHub has not yet reflected the close; retry |

An issue closed as `not_planned` is always refused.

#### `todos labels sync`

Create any missing pipeline labels (`tpo:*`, triage, and mirror labels) in the
project's repository. Existing labels are left untouched, including color and
description.

```bash
tpo todos labels sync myproject
```

Prints `created: <label>` per label, or `labels up to date (<N> names present;
color/description not compared)`. Exit codes: 0
synced or up to date, 1 GitHub failure (partial `created:` lines are printed
first; `gh_truncated` means the label list hit the 1000 cap), 2 unknown project.

#### `todos audit`

Check TODO issue bodies against the backlog contract and, with `--fix`,
normalize the mirror labels (priority/effort/phase/review) to match the body.

```bash
tpo todos audit myproject
tpo todos audit myproject --todo 5
tpo todos audit myproject --fix --dry-run
tpo todos audit myproject --fix
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--todo` | No | Audit one issue (`TODO-5` or `5`), open or closed; default is every open `tpo:todo` issue |
| `--fix` | No | Apply mirror-label changes; closed issues and non-TODO issues are skipped |
| `--dry-run` | No | With `--fix`: print the changes as `would fix` without applying them |

Findings print as `TODO-<N>: <finding>` using the vocabulary
`missing-section:<Name>`, `duplicate-section:<Name>`, `plan:missing`,
`plan:duplicate`, `plan:invalid:<code>`, `branch:invalid`, `branch:default`,
`decision:<Name>:<value>`, `label:missing:<label>`, `label:extra:<label>`,
`state:closed`, and `not-a-todo`. The summary is
`audit: issues=N findings=N fixable=N`, extended with `skipped=N applied=N`
under `--fix`. Only `label:*` findings are fixable; `plan:missing` and
`state:closed` are informational. A `gh` failure while fixing one issue prints
`unfixed TODO-<N>: <code>`; the command continues to the next issue and exits 1.

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | No actionable finding (after `--fix`: nothing left unfixed) |
| 1 | Actionable findings, skipped or failed fixes, or a GitHub failure |
| 2 | Usage (`--dry-run` without `--fix`) or unknown project |

See [Manage TODOs as GitHub Issues](howto-github-issues-todos.md) for the
finding table with remediation.

---

### `plan validate`

Validate a TODO's Plan attachment and optional `tpo-plan` manifest. Without
`--plan`, the Plan path is read from the issue's `### Plan` section:

```bash
tpo plan validate myproject --todo TODO-42
tpo plan validate myproject --todo TODO-42 --require-manifest
tpo plan validate myproject --todo 42 --plan docs/pipeline/TODO-42-plan.md --require-manifest
```

**Arguments:**
| Arg | Required | Description |
|-----|----------|-------------|
| `project` | Yes | Project slug |
| `--todo` | Yes | TODO to validate (`TODO-42` or `42`) |
| `--plan` | No | Repository-relative Plan candidate to validate instead of the issue's Plan |
| `--require-manifest` | No | Reject a valid legacy Plan that has no `json tpo-plan` block |

Success prints `Plan has a valid manifest for TODO-N: <k> tasks` (exit 0) or,
for manifest-free Markdown, `Plan is valid legacy Markdown for TODO-N; warning:
no tpo-plan manifest` (exit 0 unless `--require-manifest`). Failures print
`Plan validation failed for TODO-N: <code>` and exit 1, where `<code>` is
`plan_invalid:missing` or `plan_invalid:duplicate` (not exactly one Plan value
in the issue), `attachment_<code>` (the path is not an existing regular file
inside the repository, or unreadable), a manifest validation code,
`--require-manifest requires a tpo-plan block`, bare `unreadable` (the Plan
file could not be read), or a `gh` error code. When the issue is closed the
line ends with
`; warning: issue is closed (<reason>)`. Exit 2 for an unknown project.

See the [Plan template](templates/tpo-plan.md).

---

### `install-profile`

Install the bundled pipeline Hermes profile for unattended kanban execution.

```bash
tpo install-profile
tpo install-profile --force
```

Creates a `pipeline` profile cloned from the active Hermes profile, then overlays the bundled `SOUL.md`. With `--force`, deletes an existing `pipeline` profile first.

After install, set the assignee: `tpo init myproject --assignee pipeline`.

---

### `config`

Read and write the global configuration:

```bash
tpo config init
tpo config get prompt_client
tpo config set prompt_client claude
tpo config set prompt_client codex
```

`prompt_client` accepts exactly the lowercase, case-sensitive values `claude`
and `codex`. If the field is absent, the effective default is `claude`.
`tpo config get prompt_client` reports both the effective value and whether it
came from the default or the active config file. `TPO_CONFIG_FILE` can select
an alternate complete config file, but there is no individual environment
override such as `PIPELINE_PROMPT_CLIENT`.

One global `prompt_client` applies to every project under `projects_dir`.
Mixed-client fleets need separate project roots.

| Setting | Selects |
|---|---|
| Global `prompt_client` | Prompt vocabulary and external-client delegation guidance; it does not install skills or choose the Hermes assignee/profile/model |
| Contract `profile` | Bundled phase and skill workflow |
| Contract `assignee` | Hermes profile and agent identity |
| Hermes configuration | Models and provider authentication |

Profile prerequisites come from the package metadata used by `tpo doctor`:

| Profile | Referenced skill | Distribution owner | Claude discovery / invocation | Codex discovery / invocation | Support |
|---|---|---|---|---|---|
| `gstack` | `ai-coding-agents` | hermes | `Hermes skill registry` / `claude -p` | `Hermes skill registry` / `codex exec` | Conditional |
| `gstack` | `autoplan` | gstack | `.claude/skills` / `/autoplan` | `.codex/skills` / `$autoplan` | Conditional |
| `gstack` | `writing-plans` | superpowers | `~/.claude/plugins/cache/claude-plugins-official/superpowers/*/skills` / `/writing-plans` | `~/.config/codex/plugins/cache/openai-curated-remote/superpowers/*/skills` / `$superpowers:writing-plans` | Conditional |
| `gstack` | `subagent-driven-development` | superpowers | `~/.claude/plugins/cache/claude-plugins-official/superpowers/*/skills` / `/subagent-driven-development` | `~/.config/codex/plugins/cache/openai-curated-remote/superpowers/*/skills` / `$superpowers:subagent-driven-development` | Conditional |
| `gstack` | `review` | gstack | `.claude/skills` / `/review` | `.codex/skills` / `$review` | Conditional |
| `gstack` | `cso` | gstack | `.claude/skills` / `/cso` | `.codex/skills` / `$cso` | Conditional |
| `gstack` | `qa` | gstack | `.claude/skills` / `/qa` | `.codex/skills` / `$qa` | Conditional |
| `gstack` | `document-release` | gstack | `.claude/skills` / `/document-release` | `.codex/skills` / `$document-release` | Conditional |
| `gstack` | `document-generate` | gstack | `.claude/skills` / `/document-generate` | `.codex/skills` / `$document-generate` | Conditional |
| `gstack` | `ship` | gstack | `.claude/skills` / `/ship` | `.codex/skills` / `$ship` | Conditional |
| `agent-skills` | `agent-skills:spec-driven-development` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:planning-and-task-breakdown` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:incremental-implementation` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:test-driven-development` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:code-review-and-quality` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:code-reviewer` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:security-and-hardening` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:security-auditor` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `agent-skills` | `agent-skills:ship` | agent-skills plugin | Unverified external plugin mechanism | Unverified external plugin mechanism | Unverified |
| `native-sdd` | `ai-coding-agents` | hermes | `Hermes skill registry` / `claude -p` | `Hermes skill registry` / `codex exec` | Conditional |

`Conditional` requires the named external skill to be installed and
discoverable by the selected client. Hermes-owned registry prerequisites are
checked locally against the assigned Hermes profile; remote worker prerequisites
remain operator-provisioned. `Unverified` is unsupported and does not become
supported merely by setting `prompt_client`; `tpo doctor` reports `UNSUPPORTED`
and exits 2 when the selected profile contains any such row, and `tpo tick`
refuses to select or register work for that unsupported profile/client pair.

See [agent client release qualification](release-qualification-agent-clients.md)
for the evidence protocol.

---

## tpo test

Run the live integration test harness: clones a disposable GitHub sandbox
repository, files a real `tpo:todo` issue, commits a Plan, runs the production
`tpo tick` as a subprocess, follows its Hermes kanban cards, requires exactly one
attributable open pull request, and tears everything down fail-closed. The kanban
tenant is the sandbox repository name. Requires `gh` >= 2.44 authenticated with
`repo` scope, `git` >= 2.32, `hermes login`, and `TPO_GH_BIN` unset. See
[howto-live-integration-test-harness.md](howto-live-integration-test-harness.md).

```bash
tpo test --repo OWNER/NAME --init-sandbox     # one-time seeding
tpo test --repo OWNER/NAME
tpo test --repo OWNER/NAME --profile agent-skills
tpo test --repo OWNER/NAME --convergence-threshold 2
tpo test --repo OWNER/NAME --keep --loop
```

**Arguments:**
| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `--repo` | Yes (or `TPO_HARNESS_REPO`) | — | Sandbox repository as `OWNER/NAME`. Must be a dedicated disposable repo; the harness closes issues/PRs and deletes branches in it. |
| `--init-sandbox` | No | — | Seed the sandbox default branch once (README, `.gitignore` ignoring `.hermes/`, `pyproject.toml`, `tests/__init__.py`, `docs/harness/SANDBOX.md`) and exit. Refuses repositories tracking files outside the seed set and `.github/**`. |
| `--fixture` | No | `happy-path` | Fixture name. Only `happy-path` is implemented. |
| `--profile` | No | `gstack` | Bundled phase profile to test. Only live-safe profiles (`gstack`, `agent-skills`) are accepted; `native-sdd` fails with `unsafe_terminal`. Unknown profiles and profile/client pairs with `Unverified` prerequisites fail before workspace creation. |
| `--timeout` | No | `86400` | Overall run timeout in seconds. Cooperatively stops polling, then reclaims and archives the tick's Hermes tasks before remote cleanup. |
| `--convergence-threshold` | No | `3` | Consecutive same-class phase failures before the run halts. |
| `--keep` | No | — | Preserve the workspace and the remote artifacts (issue, PR, branch). The next run fails `sandbox_not_quiescent` until they are cleaned up manually. |
| `--loop` | No | — | Write a numbered report snapshot in the workspace's `artifacts/` directory. Snapshots do not carry across separate CLI invocations; meaningful only with `--keep` (not enforced). |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | All phases passed, the PR invariant held, and cleanup completed |
| 1 | Phase failure, convergence halt, overall timeout, PR invariant failure (`pr_missing`/`pr_ambiguous`/`pr_closed`/`pr_merged`/`pr_discovery_incomplete`), or tick failure (`picked_none`, `failed_to_spawn`, `tick_timeout`, `tick_failed`). The workspace is deleted after a clean shutdown; re-run with `--keep` to inspect it. |
| 2 | Profile or preflight error (`unsafe_terminal`, `Unverified` prerequisites, missing dependency, `repo_missing`, `invalid_repo`, `invalid_slug`, `gh_permission`, `gh_override_forbidden`, `sandbox_not_seeded`, `sandbox_not_quiescent`, unknown fixture) or `cleanup_incomplete` — the workspace is retained under `~/.hermes/tmp/harness-*` (newest directory); `HarnessCleanupError` messages print the path, `cleanup_incomplete` prints the remote leftovers |

**Preflight and run behavior:**
- Resolves the selected profile and rejects `Unverified` prerequisites and non-live-safe terminal phases before external checks.
- Verifies locally discoverable Conditional Hermes skills against the `pipeline` assignee before workspace creation.
- Verifies `gh` auth, the viewer login, push permission, and that no other open `tpo:todo` + `ready-for-agent` issue exists in the sandbox.
- Runs `hermes kanban list --tenant <repo-name>` before phase execution. Timeouts after 15 s.
- Clones the sandbox, verifies the seed (`sandbox_seed_check`), ensures labels, creates the issue `[harness <token>] Implement mock name normalization`, commits the Plan locally, and runs the production tick (log at `artifacts/tick.log`).
- Kanban cards are created by the production tick in the tenant named after the repository (never suffixed with tick ID).
- Shutdown cancels the tick's tasks, proves kanban quiescence, discovers artifacts by run provenance, closes the issue and PR, and deletes the branch with `git push --force-with-lease`; unfinished operations are printed as leftovers with manual commands.
- Prints a `[kanban]` summary line after report generation:
  ```
  [kanban] tenant=<repo-name> tick_id=01ARZ3... profile=gstack repo=OWNER/NAME issue=#12 pr=#13 phases={phase_1 kickoff: done, ...} report=~/.hermes/tmp/harness-.../artifacts/reports/report.json keep=no (temp dir will be removed)
  ```

**Timeout and exceptional poll cleanup:**
- The harness first signals the poll thread to stop and waits for it to exit.
- It then reclaims each running Hermes task and requires Hermes to confirm that
  the worker is gone and every run has ended.
- Tasks are archived child first according to their validated parent
  relationships, then the harness confirms that the complete task set is
  archived and quiescent.
- If polling does not stop, task relationships are malformed, or any reclaim,
  termination, archive, or final-state confirmation is inconclusive, cleanup
  fails closed with `HarnessCleanupError` and retains the workspace for
  inspection. The workspace is eligible for automatic removal only after
  cleanup is confirmed.

**`KanbanPreflightError`** — `RuntimeError` subclass raised when the kanban preflight fails. Two triggers:
- `subprocess.TimeoutExpired` after 15 s → actionable timeout message
- Non-zero exit from `hermes kanban list --tenant <tenant>` → authentication/tenant access failure

---

## Environment Variables

Machine-level defaults live in the global config file. Create it with
`tpo config init`, then edit it directly or use `tpo config set <key> <value>`.
The generated file includes active defaults for `projects_dir`, `state_dir`,
`log_file_subpath`, `log_retention_days`, and `slack_channel`.
Individual `PIPELINE_*` environment variables do not override config entries.

| Variable | Default | Description |
|----------|---------|-------------|
| `TPO_CONFIG_FILE` | unset | Path to an alternate complete config file |
| `XDG_CONFIG_DIR` | `~/.config` | Base directory for the default `tpo/config.yaml` path |
| `HERMES_HOME` | `~/.hermes` | Base for the legacy fallback config path `tpo.yaml` |

## See Also

- [Getting-started tutorial](tutorial-getting-started.md) — End-to-end walkthrough
- [How to approve and ship a TODO](howto-approve-and-ship.md) — The full ship workflow
- [How to debug ticks and recover runs](howto-debugging-and-recovery.md) — Using `--verbose`, `--debug`, run markers, and issue-state recovery
- [Manage TODOs as GitHub Issues](howto-github-issues-todos.md) — Filing, triage, audit, and completion of `tpo:todo` issues
- [Circuit breaker explanation](explanation-circuit-breaker.md) — How no-progress tracking works
- [Pipeline contract explanation](explanation-pipeline-contract.md) — Why versioned contracts exist
