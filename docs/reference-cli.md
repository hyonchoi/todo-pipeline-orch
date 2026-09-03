# CLI Reference

Complete reference for `tpo` subcommands.

The examples below use the installed `tpo` command. In a source checkout,
contributors can run the same commands as `uv run tpo ...`.

- `tpo <command> [args]` — Production pipeline orchestration (tick, init, doctor, ...)
- `tpo test [args]` — Mock integration test harness

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
repository, worktree, branch, and base commit. For an embedded source it
verifies the Plan from the pinned issue snapshot and its private run artifact;
for a legacy-path source it verifies Plan bytes read from the immutable base
commit. It reports the current head and clean/dirty lifecycle state separately,
so a valid later implementation or closeout commit is not misreported as
authority drift. See
[recovering runs and issue state](howto-debugging-and-recovery.md#recovering-runs-and-issue-state).

---

### `todos`

Manage the GitHub Issues backlog. All four subcommands run `gh` against the
project's `origin` and exit 2 for an unknown project.

#### `todos create`

Preview, create, or resume one validated issue with an embedded Plan:

```bash
tpo todos create myproject --request-file <project>/.hermes/todo-create-input/<uuid>.json
tpo todos create myproject --request-file <project>/.hermes/todo-create-input/<uuid>.json --approved-repo owner/repo --yes
tpo todos create myproject --request-file <project>/.hermes/todo-create-input/<uuid>.json --approved-repo owner/repo --issue 42 --yes
```

The request at `<state-dir>/todo-create-input/<uuid>.json` has exact mode
`0600` inside an exact-mode-`0700` directory and has exactly `schema_version`,
`transaction_id`,
`title`, `fields`, `plan_markdown`, and `tasks`. Without `--yes`, TPO prints the
complete canonical preview and accepts only `create`. With `--yes`,
`--approved-repo` must exactly match that preview. `--issue` resumes a
previously confirmed partial issue; normally rerun without it so the durable
transaction marker can discover the issue.

One creator at a time may use a configured state directory. TPO retains the
approved request until the issue is observed with its embedded manifest,
derived labels, `ready-for-agent`, and no `needs-triage`. It never closes or
deletes a partial issue.

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
`missing-section:<Name>`, `duplicate-section:<Name>`, `plan:missing`, `plan:legacy_path`,
`plan:duplicate`, `plan:invalid:<code>`, `branch:invalid`, `branch:default`,
`decision:<Name>:<value>`, `label:missing:<label>`, `label:extra:<label>`,
`state:closed`, and `not-a-todo`. `plan:legacy_path` is informational and is
never rewritten. The summary is
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

Validate a TODO's embedded or legacy-path Plan. Without `--plan`, the source is
resolved from the issue snapshot:

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
| `--plan` | No | Legacy repository-relative Plan candidate to validate instead of the issue source |
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

### `skills`

Transactionally manage the bundled `todo-manager` skill:

```bash
tpo skills install todo-manager --target codex --scope user [--reinstall]
tpo skills uninstall todo-manager --target claude --scope project --yes [--force]
tpo skills recover todo-manager --target codex --scope user --finish
tpo skills recover todo-manager --target codex --scope user --rollback
```

`--target` is required and accepts `codex` or `claude`; scope defaults to
`user`. Codex destinations are `.agents/skills/todo-manager`, and Claude
destinations are `.claude/skills/todo-manager`, below the home directory or
Git top level. Adjacent lock, journal, and receipt files make each single-target
operation recoverable. A pending journal blocks new mutations. Recovery can
roll back only before the recorded commit point; afterward use `--finish`.

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

Run the mock integration test harness: bootstraps a temporary git project, executes
pipeline phases, and generates a structured findings report. Runs against the real
`hermes kanban` adapter (tenant `mock-project`) — the `--kanban null` no-network mode
was removed along with `runner.py`/`watcher.py` in v0.5.6; the harness now always
requires `hermes login` and access to the `mock-project` tenant.

```bash
tpo test --fixture happy-path
tpo test --fixture happy-path --profile gstack
tpo test --fixture happy-path --phase phase_2_autoplan
tpo test --fixture happy-path --convergence-threshold 2
```

**Arguments:**
| Arg | Required | Default | Description |
|-----|----------|---------|-------------|
| `--fixture` | Yes | — | Fixture name. Only `happy-path` is implemented. |
| `--profile` | No | `gstack` | Bundled phase profile to test. Unknown profiles and profile/client pairs with `Unverified` prerequisites fail before workspace creation or kanban registration. |
| `--phase` | No | — | Run only the named executable phase from the selected profile (e.g. `phase_2_autoplan`). Gate phases are rejected because they cannot execute independently. |
| `--timeout` | No | `86400` | Overall run timeout in seconds. Cooperatively stops polling, then reclaims and archives the tick's Hermes tasks. |
| `--convergence-threshold` | No | `3` | Consecutive same-class phase failures before circuit breaker halts the run. |
| `--keep` | No | — | Preserve the temporary directory after the run for inspection. |
| `--loop` | No | — | Write a numbered report snapshot in the current workspace's `artifacts/` directory. Snapshots do not carry across separate CLI invocations, so cross-invocation auto-diff is unavailable. Requires `--keep`. |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | All phases passed |
| 1 | Phase failure, convergence halt, or timeout |
| 2 | Profile, prerequisite, preflight, or setup error (including unknown/unsupported profile, missing Conditional skill, missing dependency, or unreachable `mock-project` tenant) |

**Kanban preflight behavior:**
- Resolves the selected profile and rejects `Unverified` prerequisites before external checks.
- Verifies locally discoverable Conditional Hermes skills against the `pipeline` assignee before workspace creation.
- Runs a preflight check (`hermes kanban list --tenant mock-project`) before phase execution. Timeouts after 15 s.
- Creates a kanban card in the fixture's `mock-project` tenant (never suffixed with tick ID).
- Card body includes `tick_id`, `fixture_name`, and `state_dir` metadata for debug tracing.
- On convergence halt, clears the active task with `outcome="abandoned"`.
- Prints a `[kanban]` summary line after report generation:
  ```
  [kanban] tenant=mock-project tick_id=01ARZ3... phases={phase_1 kickoff: done, ...} report=~/.hermes/tmp/harness-.../artifacts/reports/report.json keep=no (temp dir will be removed)
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
