# How to run the Live Integration Test Harness

`tpo test` drives one production `tpo tick` against a **live, disposable GitHub
sandbox repository**. It clones the sandbox, files a real `tpo:todo` issue,
publishes an embedded issue Plan, creates an empty local run anchor, runs the
production tick as a subprocess, follows the Hermes
kanban cards it registers, requires exactly one attributable pull request, and
then tears everything down fail-closed. Nothing is faked: `gh`, `git`, Hermes,
and the prompt client are all real.

## Prerequisites

- `uv sync` has run at the repo root.
- `git` >= 2.32 on PATH. The harness runs git with global and system config
  **disabled** and supplies `credential.helper=!gh auth git-credential` per
  network call, so the `gh` token is used even when your own credential helpers
  differ. Accepted cost: operator git config is not read at all, so `http.*`
  (proxies, custom CA), `url.*.insteadOf` (e.g. an SSH rewrite), `credential.*`,
  and `safe.directory` do not apply to the harness's own git calls — use the
  `https_proxy` / `GIT_SSL_CAINFO` environment variables instead.
- `gh` >= 2.44, authenticated with the `repo` scope, and `gh auth setup-git`
  run once. The harness injects `gh` credentials itself, but the **pipeline
  agent** spawned by the production tick pushes the feature branch with your
  normal git configuration, so git must be able to obtain credentials from `gh`.
- `TPO_GH_BIN` must be **unset**. Live runs reject an overridden `gh`
  (`gh_override_forbidden`) because it could hide or fake every remote step.
- `hermes login` with access to a kanban tenant named after the sandbox
  repository. The tenant is the repository name (`OWNER/mock-project` →
  tenant `mock-project`); the simplest setup is to name the sandbox repo after
  an existing tenant.
- The selected prompt client (`claude` or `codex`) installed and authenticated.
  Preflight checks only the client selected by `prompt_client`.

New cards invoke the installed `tpo-agent-supervisor` launcher. Install it with
TPO in its Python 3.12+ environment; the Hermes shell does not need a separate
Python interpreter for client launch settings. The supervisor delivers pinned
prompt bytes directly through stdin and owns launch, deadlines, exit collection,
and result validation. There is no Hermes prompt-marker extraction or unmanaged
shell client launch in a new card. Historical cards retain their old instructions;
start a new harness run to exercise current dispatch and admission-waiting
instructions. New cards use `run --wait`, which produces multi-line JSON status
output: one line every 60 seconds with `"final": false` (fields: status,
generation, elapsed_s, remaining_s, accepted_tasks), then a final report with
`"final": true`. Consumers act only on the final line. If it returns a nonterminal
status, they reconnect without changing card state. Existing workers may require
an explicit operator refresh or recovery through supported Kanban operations;
upgrading does not rewrite them.

The selected client must satisfy the
[client and platform prerequisites](howto-agent-supervisor.md#client-and-platform-prerequisites).
Linux process supervision requires `/proc` and pidfds; macOS requires available
`libproc` identity and audit-token signaling capabilities. Capability refusals
occur before attempt admission and are available from supervisor status.
Both platforms supervise only directly launched processes. Confirmed client
termination satisfies process cleanup; the client owns its descendants. Neither
cgroup v2 nor a systemd user manager is required.

Codex uses `--dangerously-bypass-approvals-and-sandbox`; Claude uses
`--dangerously-skip-permissions` with its configured tools. Clients and checks
run as the invoking OS user, without supervisor sandbox restrictions or storage
isolation. No user/global client configuration is edited.

After the client exits, the supervisor validates results against current Git
state. Manifest checkpoints require pinned verification commands executed as
direct argv in exact-commit snapshots, using the inherited environment and
available worktree virtual environment, plus a fresh review within the attempt's
remaining deadline. Hermes reports the structured outcome through supported
worker tools. A zero exit without valid result and checkpoint evidence cannot
complete a card. Preserve partial work; collection does not clean or commit the
original worktree to force acceptance.

## One-time sandbox setup

Create a dedicated, disposable repository (empty, or containing only a README /
minimal scaffold) and seed it once:

```bash
uv run tpo test --repo OWNER/NAME --init-sandbox
```

`init_sandbox` is the only harness code path allowed to push to the sandbox's
default branch; every push is a plain fast-forward. It pushes one
`chore(harness): seed sandbox` commit with the seed files:

| Seed file | Purpose |
|-----------|---------|
| `README.md` | Placeholder for the sandbox project |
| `.gitignore` | Ignores runtime state (`.hermes/`, `.serena/`, `uv.lock`), agent scratch (`.superpowers/`, `.code-review-graph/`), and Python artifacts (`__pycache__/`, `*.py[cod]`, `.venv/`) |
| `pyproject.toml` | `pytest` dev group and `testpaths` so the agent's tests run |
| `tests/__init__.py` | Test package marker |
| `docs/harness/SANDBOX.md` | Marker that this repo is a harness sandbox |

Behavior:

- **Empty repository** (no default branch and no refs): `git init -b main`,
  commit the seed files, push `main`, and set it as the GitHub default branch.
- **Non-empty repository**: clone the default branch and refuse with
  `sandbox_not_empty` when any tracked path is neither a seed file nor under
  `.github/**`. README-only and minimal-scaffold repos are seedable by design;
  a real project is not. Missing seed files are added and a `.gitignore` that
  does not carry all required runtime-state rules is replaced, then the result is pushed to the
  default branch, whatever its name.
- Returns `already_seeded` (no writes, no push) when every seed file is tracked
  and `.gitignore` already carries all required runtime-state rules.
- Per-run `sandbox_seed_check` requires only `pyproject.toml`,
  `tests/__init__.py`, `docs/harness/SANDBOX.md` tracked at HEAD and a
  `.gitignore` containing `.hermes/`, `.serena/`, and `uv.lock`; `README.md` is seeded but not required.

The repository can also be supplied through the `TPO_HARNESS_REPO` environment
variable; `--repo` takes precedence.

## Running

```bash
uv run tpo test --repo OWNER/NAME
uv run tpo test --repo OWNER/NAME --profile agent-skills --timeout 1800
uv run tpo test --repo OWNER/NAME --convergence-threshold 2
uv run tpo test --repo OWNER/NAME --keep --loop
```

- `--fixture` defaults to `happy-path` and is the only fixture
  (`unknown_fixture` otherwise).
- `--profile` defaults to `native-sdd`, the default phase profile
  ([ADR-0004](adr/0004-native-sdd-is-the-default-phase-profile.md)). All three
  bundled profiles (`native-sdd`, `agent-skills`, `gstack`) are on the live-safe
  allow-list; any other profile fails with `unsafe_terminal` before anything
  remote is touched. Unknown profiles, `Unverified` profile/client pairs, and
  missing locally verifiable Conditional Hermes skills are also rejected up
  front.
- A plan-gated (`requires_plan`) profile — `native-sdd` — is driven across
  **multiple ticks**; `gstack` and `agent-skills` complete in one. The fixture
  Plan carries a `json tpo-plan` manifest, so the plan gate is exercised live.
- `--timeout` is the overall watchdog (default 86400 s).
- `--convergence-threshold` (default 3) halts the run after N consecutive phase
  failures of the same error class (`dependency_error`, `hermes_error`,
  `claude_error`, `timeout`, `phase_failure`).
- `--loop` writes a numbered snapshot (`artifacts/happy-path-report.N.json`);
  it is meaningful only with `--keep` (not enforced — without `--keep` the
  snapshot is deleted with the workspace).

### Run flow

1. **Preflight** — profile and prerequisites, tools on PATH, `gh` auth and
   viewer login, push permission on the sandbox (`gh_permission`), and sandbox
   quiescence: another open `tpo:todo` + `ready-for-agent` issue fails the run
   with `sandbox_not_quiescent`. Kanban reachability is checked with
   `hermes kanban list --tenant <repo-name>`.
2. **Clone and verify** — clone the sandbox into the workspace, run
   `sandbox_seed_check` (seed files tracked, `.gitignore` ignores `.hermes/`;
   `sandbox_not_seeded` otherwise), ensure the `tpo:*` labels exist, and take a
   baseline snapshot of the remote heads.
3. **Issue, Plan, and anchor** — create
   `[harness <token>] Implement mock name normalization` through the validated
   request workflow behind `tpo todos create`. Each invocation gets a fresh
   transaction UUID; retries within its isolated workspace reuse that UUID and
   the production creation lock and reconciliation state. The workflow creates
   a triaged issue, binds its assigned number into the embedded Plan's
   schema-v1 `json tpo-plan` manifest, and publishes the final body and readiness
   labels. The harness verifies the authored body, title, open state, audit
   findings, and labels; ambiguous ownership or failed readiness prevents ticking
   and retains recovery artifacts. A matching title alone never proves ownership.

   No implementation Plan file is tracked or committed under `docs/`. After
   readiness, `create_run_anchor` uses `git commit-tree` with the current HEAD's
   tree and parent, signing disabled, and the sandbox identity. Its message
   carries `tpo-harness-anchor`, the transaction UUID, run token, and issue
   identity. A durable pending record precedes compare-and-swap `git update-ref`;
   a completed record permits only reuse of that verified anchor. Retries never
   stack anchors; an unborn or unexpectedly moved HEAD fails closed. Tracked
   content, the index, untracked files, and the remote default branch stay
   unchanged. Setup neither precreates the implementation branch nor pushes.
   The saved `run_base_sha` follows ticking, registration, discovery, and shutdown;
   serialized `base_sha` fields retain their existing names. The issue names
   `feat/harness-<token>`; pinned runs require that branch. In a kept workspace
   the selected branch is in `projects/<slug>/.hermes/pipeline_branch.txt`.
4. **Tick** — wait until GitHub's label-filtered listing shows the run's issue
   as the only ready `tpo:todo` issue (`issue_not_visible` after 60 s), then
   recheck the final issue before running production `tpo tick` as a subprocess
   with an isolated `TPO_CONFIG_FILE` (log at `artifacts/tick.log`). Recover its
   `tick_id` and expected phase keys. For a plan-gated profile, use the production
   registration loader, then require embedded Plan authority in the current
   schema-v6 registration with `plan_path=None`, the expected repository, issue,
   branch, `run_base_sha`, and
   Plan digest (`registration_invalid`, `unexpected_registration`,
   `registration_base_mismatch`, `registration_plan_mismatch`). Hash only the
   Plan document as UTF-8, normalize line endings to LF and exactly one trailing
   newline, and retain the assigned issue number. Issue fields and the folding
   wrapper are excluded. The issue manifest remains schema-v1; production
   registration readers also support legacy v2, v3, v4, and v5 registrations and
   legacy repository Plans without rewriting their identities. New v6 registrations
   require supervisor authority and pin the complete ordered phase definitions.
   The `tick_registered` event records `tick_id`, `phase_keys` and, for a pinned
   run, `pinned`, `worktree`, and `branch`.
5. **Poll / drive** — a non-plan profile polls the registered kanban cards once
   until every card is terminal, auto-completing gate cards behind their
   predecessors. A plan-gated run instead loops: poll the registered cards until
   the created worker prefix *settles* (gates are never auto-completed — the
   production reconcilers own them), classify against the full required worker
   list, then run another
   `tpo tick` under the same tick id. Each settled board emits
   `tick_completed` `{tick_no, status_map}`. A board identical to the previous
   tick's is *tolerated* once — one reconciler hop can legitimately change
   nothing and need the next tick — and only logged at warning level; the third
   consecutive identical board emits `tick_stalled` `{tick_no, status_map}` and
   fails the run with `tick_stalled`. The count is consecutive and resets
   whenever the board moves. Every tick re-asserts that `current_tick_id.txt`
   still names the run: a tick that registered a different run fails with
   `unexpected_selection`, while one that merely selected nothing counts as no
   progress and reports `tick_stalled`. The loop is bounded by
   a schedule-based tick budget; exhausting it fails
   with `tick_budget_exhausted`, which
   means the run is stuck rather than out of legitimate work. A tick whose
   subprocess exits non-zero — `tpo tick` isolates a project's crash but reports
   it — fails the run with `tick_crashed`, whose detail is the tail of
   `artifacts/tick.log`. The run is `delivered` when the registered delivery card
   (`phase_8_finish_branch` for new native-sdd runs, legacy `finish`) is `done`
   **and every required worker card is present and `done`**; any card that is `blocked`,
   `failed` or `archived` classifies the run as failed instead. There is no
   `human-gate` card: a `requires_plan` run defers `phase_9_human_review` and
   registers no card for it, so a board matching the older "finish done,
   human-gate blocked" description would be classified as *failed*, never
   delivered. `classify_pinned_run` reads card statuses only — the
   `finish-verified` marker is an internal evidence filename written by the
   production delivery reconciler, not a phase key or part of the harness verdict.
   A modern profile without a delivery role reaches sequence completion when all
   required workers are done; the separate PR invariant can then fail with
   `pr_missing` rather than waiting for an undeclared delivery phase.
   The pinned registration and authored Plan expectation are revalidated before
   and after later ticks and polling, preserving the same authority through
   implementation, review, and finish. Validation failures retain the tick
   identity so shutdown can account for workers.
6. **PR invariant** — exactly one open, unmerged pull request attributable to
   this run must exist and target the default branch (a `pr_invariant_failed`
   event with `pr_missing`, `pr_ambiguous`, `pr_closed`, `pr_merged`, or
   `pr_wrong_base` otherwise). A plan-pinned run adds `pr_wrong_head`: the PR
   must be raised from the branch the registration pinned, so a PR attributable
   to the issue but pushed from any other head is not this run's delivery. When
   discovery itself cannot classify every branch or PR the run fails with
   `pr_discovery_incomplete` instead; no `pr_invariant_failed` event is emitted
   for that case. An anchor-only branch proves cleanup ownership but fails
   delivery with `implementation_missing`; it is not implementation success.
7. **Shutdown** (always runs once the issue exists, even after a failure) —
   cancel the tick's kanban tasks, prove quiescence from the archived-inclusive
   snapshot, discover remote artifacts by run provenance (every non-default
   commit must descend from the run's empty anchor), close the issue, close the
   PR, and delete the branch with `git push --force-with-lease`. Anything that
   could not be verified or removed is printed as a leftover with the manual
   command that finishes it.
8. **Report** — `artifacts/reports/report.json` and `report.md`, plus a
   `[kanban]` summary line; the workspace is removed after a clean shutdown
   (also on exit 1) unless `--keep` was passed or cleanup did not complete.

## Output and exit codes

```
[kanban] tenant=mock-project tick_id=01ARZ3... profile=native-sdd ticks=4 repo=OWNER/mock-project issue=#12 pr=#13 phases={...} report=~/.hermes/tmp/harness-.../artifacts/reports/report.json keep=no (temp dir will be removed)
```

`ticks=<n>` counts the production `tpo tick` invocations the run consumed: `1`
for a non-plan profile, one per reconciler hop for a plan-gated one.

| Code | Meaning |
|------|---------|
| 0 | Every phase passed, the PR invariant held, and cleanup completed |
| 1 | Phase failure, overall timeout, convergence halt, PR invariant failure (`implementation_missing`, `pr_missing`, `pr_ambiguous`, `pr_closed`, `pr_merged`, `pr_wrong_base`, `pr_wrong_head`), `pr_discovery_incomplete` at the post-run check, or tick failure (`picked_none`, `failed_to_spawn`, `tick_timeout`, `tick_crashed`, `tick_failed`, plus the plan-pinned drive's `registration_invalid`, `registration_base_mismatch`, `registration_plan_mismatch`, `unexpected_selection`, `tick_stalled`, `tick_budget_exhausted`). The workspace is deleted after a clean shutdown. |
| 2 | Profile, preflight, or cleanup error (`cleanup_incomplete`, including when the shutdown discovery fails as well) — the workspace is retained under `~/.hermes/tmp/harness-*` (newest directory). A `HarnessCleanupError` message prints the retained path; `cleanup_incomplete` prints the remote leftovers with their manual commands. |

## `--keep` and manual cleanup

`--keep` retains the workspace **and** the remote artifacts: the kanban is still
cancelled and quiesced (workers must stop), but the issue stays open and the PR
and branch are left in place so you can inspect them. The next run then fails
preflight with `sandbox_not_quiescent` until you clean up by hand:

```bash
gh issue close <N> --repo OWNER/NAME
gh pr close <M> --repo OWNER/NAME
git push https://github.com/OWNER/NAME --delete <branch>
```

The same commands apply to any leftover printed at the end of a failed run.
After a run's PR has been **merged**, the harness will not delete its branch
(the provenance vacuity guard refuses to attribute a branch whose commits are
now on the default branch) — delete it manually.

## Workspace layout

Workspaces live under `~/.hermes/tmp/harness-*/` (not the OS temp root, whose
`/private/var/...` prefix the Hermes write guard blocks):

```
harness-xxxxxxxx/
  projects/<slug>/      # sandbox clone; <slug> is the repository name
    .hermes/            # per-run state (ignored by the sandbox .gitignore)
  state/                # isolated TPO state dir (TPO_CONFIG_FILE points here)
  artifacts/
    events.jsonl        # per-phase event log
    tick.log            # stdout/stderr of the production tick subprocess
    reports/            # report.json, report.md
    provenance/         # bare mirror used for branch attribution
    staging/            # bare staging repo for lease-protected branch deletion
```

The workspace is retained **only** on a cleanup or quiescence failure, an
interrupt, an unreconciled issue creation, or `--keep`. Ordinary exit-1 failures
(`picked_none`, `failed_to_spawn`, `tick_timeout`/`tick_crashed`/`tick_failed`, phase failure,
PR invariant) delete it after a clean shutdown — re-run with `--keep` to
inspect `artifacts/tick.log` or the reports. There is **no automatic reaping**
of retained `harness-*` directories; remove them yourself once the leftovers
are resolved.

## Verification

The harness's own tests are hermetic (no GitHub, no Hermes):

```bash
uv run pytest tests/test_harness.py tests/test_harness_e2e.py -q
```

A passing live run exits 0, logs `phase <key>: ... -> done` for every phase,
prints a `[kanban]` line with `pr=#M`, `ticks=<n>` and `phases={...: done, ...}`,
and its `report.json` (kept with `--keep`) records every phase `done`, the issue
and PR numbers, and no leftovers.

For a plan-gated run, `events.jsonl` additionally carries one `tick_registered`
(with `pinned`, `worktree` and `branch`) and one `tick_completed`
`{tick_no, status_map}` per settled board; a `tick_stalled` event marks the
third consecutive identical board that failed the run — the tolerated repeat
before it is a warning log line, not an event. A delivered run's final board has the
registered delivery card (`phase_8_finish_branch` for new native-sdd runs,
legacy `finish`) `done` and every required worker card present and `done` too.
The open, unmerged pull request the delivery card leaves behind is where the
run is supposed to stop, and
the post-run PR invariant is what checks it. Deferred cards retain the exact
profile key in reports and cannot be omitted to claim completion. No card is
expected to be `blocked`: a blocked card classifies the run as failed.

## Troubleshooting

| Code | Meaning / fix |
|------|---------------|
| `repo_missing` | No `--repo` and `TPO_HARNESS_REPO` unset |
| `invalid_repo` | Repository is not `OWNER/NAME` |
| `invalid_slug` | The repository name is not a valid tpo project slug (it doubles as the kanban tenant) |
| `gh_permission` | Viewer lacks WRITE/MAINTAIN/ADMIN on the sandbox |
| `gh_viewer_unknown` | `gh api user` returned no usable login; re-run `gh auth login` |
| `gh_override_forbidden` | `TPO_GH_BIN` is set; unset it |
| `sandbox_not_seeded` | Seed files or a required runtime-state ignore rule (`.hermes/`, `.serena/`, `uv.lock`) is missing; run `--init-sandbox` |
| `sandbox_not_empty` | `--init-sandbox` refused a repo tracking files outside the seed set / `.github/**` |
| `seed_incomplete` | `--init-sandbox` committed but a seed file is not a blob at HEAD; the init workspace is removed again, so re-run `--init-sandbox` (nothing was pushed) |
| `default_branch_unknown` | `--init-sandbox`: `gh` reports no default branch but `git ls-remote` advertises refs; set the default branch on GitHub |
| `default_branch_unset` | `--init-sandbox` pushed `main` but GitHub did not report it as default; set it manually |
| `default_branch_mismatch` | `--init-sandbox`: the cloned branch differs from the reported default branch |
| `workspace_exists` | `--init-sandbox`: `<workspace>/<slug>` already exists; remove it and retry |
| `sandbox_not_quiescent` | Another open `tpo:todo` + `ready-for-agent` issue exists (detail lists the numbers); close it |
| `issue_not_visible` | GitHub's label-filtered listing did not show the run's issue as ready within 60 s of creation; check `gh issue view <N> --repo <owner/name>` (labels present?) and re-run |
| `unknown_fixture` | Only `happy-path` exists |
| `unsafe_terminal` | Profile is not on the live-safe allow-list (`native-sdd`, `agent-skills`, `gstack`); a locally added profile is rejected before anything remote is touched |
| `picked_none` | The tick selected no issue; re-run with `--keep`, then read `artifacts/tick.log` and check the issue labels |
| `failed_to_spawn`, `tick_timeout`, `tick_failed` | Production tick problems; re-run with `--keep`, then read `artifacts/tick.log` |
| `tick_crashed` | `tpo tick` exited non-zero: at least one project's tick raised. The scan still ticked every project (crashes are isolated), and the detail is `rc=<n>` plus the tail of `artifacts/tick.log`, which carries the failing project's `tick failed: error_type=… : …` line and its traceback. Before this code existed the crash was invisible and the run was misreported as `tick_stalled` |
| `registration_invalid` | The run's `registration.json` was rejected by the production contract loader; the detail is the contract error |
| `registration_base_mismatch` | The run pinned a `base_sha` other than the harness's saved empty run anchor (`run_base_sha`) |
| `registration_plan_mismatch` | The registered `plan_hash` differs from the independently expected harness-authored embedded Plan document |
| `unexpected_selection` | A later tick registered a *different* run in `current_tick_id.txt`, or persisted none; the harness refuses to keep driving another run's cards, and because that run's workers are unaccounted for it closes the issue but skips branch and PR cleanup. A later tick that merely selected nothing (`picked_none`) is treated as no progress and reported as `tick_stalled` instead |
| `tick_stalled` | Three consecutive ticks settled on an identical, undelivered board — one no-progress tick is tolerated (several reconciler hops legitimately change nothing and need the next tick), a second in a row is a stall. The detail names the consecutive count. Re-run with `--keep` and read `artifacts/tick.log` plus the last `tick_stalled` event's `status_map` |
| `tick_budget_exhausted` | The plan-pinned run consumed `pinned_tick_budget(step_keys)` ticks without reaching a verdict; the budget is deliberately generous, so this means the run is stuck |
| `pr_missing`, `pr_ambiguous`, `pr_closed`, `pr_merged`, `pr_wrong_base`, `pr_wrong_head` | PR invariant failed (none, several, a non-open attributable PR, a PR whose base is not the default branch, or — plan-pinned only — a PR raised from a head other than the registered branch; a wrong-base PR is still cleaned up) |
| `pr_discovery_incomplete` | Provenance could not classify every branch/PR at the post-run check (exit 1; the workspace is removed after a clean shutdown). If the shutdown discovery fails too, cleanup is skipped for safety, the run ends with `cleanup_incomplete` (exit 2) and the leftovers list the manual commands |
| `cleanup_incomplete` | At least one remote operation failed; the detail names the retained workspace; finish the printed commands, then remove it |
| `issue_unverified` | Production creation or transaction reconciliation failed; inspect the retained UUID request and creation state before recovering any remote issue |
| `issue_readiness_drift`, `issue_request_drift`, `issue_request_path` | The issue no longer matches the authored ready issue, the saved request changed, or its parent path is unsafe; ticking stops and recovery artifacts are retained |
| `anchor_unborn_head`, `anchor_head_moved` | HEAD is unborn or no longer matches the recorded parent/anchor; setup refuses to create a retry anchor |
| `anchor_record_invalid`, `anchor_identity_mismatch`, `anchor_record_missing` | Anchor recovery evidence is invalid, belongs to another run, or is absent despite an existing anchor; preserve the clone for inspection |
| `implementation_missing` | The attributable PR branch contains only the empty run anchor; cleanup ownership does not satisfy delivery |

A missing `git` or `gh` fails preflight with a `Missing dependency: ...`
message; a missing `hermes` or prompt client raises its own dependency error
(`HermesDependencyError` / `AgentClientDependencyError`). Verify with `which`
and `hermes chat -q "echo hello"`.

## Architecture

The harness is a single module, `hermes_pipeline/harness.py`; `test_report.py`
turns `events.jsonl` into the reports and `cli.py` wires the `test` subcommand.

| Function | Role |
|----------|------|
| `resolve_sandbox_repo` | `--repo` / `TPO_HARNESS_REPO` → validated `SandboxRepo` (slug = tenant) |
| `preflight_check`, `github_preflight`, `_kanban_preflight` | Tools, `gh` auth/permission/quiescence, kanban reachability |
| `init_sandbox` | One-time seeding of the default branch |
| `clone_sandbox`, `sandbox_seed_check`, `take_baseline` | Clone, verify seed, snapshot remote heads |
| `create_harness_issue`, `_harness_create_request`, `verify_harness_issue`, `create_run_anchor` | Production embedded issue creation and readiness verification; one recoverable empty local anchor |
| `run_tick`, `recover_tick_registration` | Production `tpo tick` subprocess and its registration |
| `recover_pinned_registration`, `assert_pinned_registration_unchanged`, `assert_tick_id_unchanged` | Embedded registration recovered and revalidated through the production trust boundary; per-tick tick-id re-assertion |
| `drive_ticks`, `_drive_single_tick`, `_drive_pinned_ticks` | One-tick and bounded multi-tick drives; emit `tick_registered`, `tick_completed`, `tick_stalled` |
| `poll_pinned_run`, `classify_pinned_run`, `pinned_tick_budget` | Settle a pinned board without completing gates, classify it, bound the tick count |
| `poll_registered_phases`, `_auto_complete_gate_tasks`, `_ConvergenceMonitor` | Kanban poll loop and convergence halt |
| `discover_remote_artifacts`, `branch_has_run_provenance`, `verify_pull_request` | Provenance-based attribution and the one-open-PR invariant |
| `shutdown_run`, `cleanup_remote` | Fail-closed cancel → quiesce → discover → close/delete with lease |
| `run_harness` | Orchestration and exit-code mapping |

## Safety rules and accepted residuals

- Use a **dedicated disposable repository**. `--init-sandbox` refuses real
  projects, but the harness closes issues and PRs and deletes branches in
  whatever sandbox it is pointed at.
- Runs push feature branches only; the default branch is written solely by
  `--init-sandbox`, and never with `--force`.
- **Never create branches in the sandbox during an active run.** Attribution is
  by provenance (descent from the run's empty anchor). Accepted residual: an
  operator branch cut from the default branch mid-run and fast-forwarded by the
  agent is indistinguishable from a run branch and would be deleted.
- Destructive remote cleanup never runs while an agent may still be pushing: on
  an unconfirmed cancel, a quiescence timeout, or any discovery failure only the
  issue is closed and everything else becomes a leftover.
- Branch deletion is compare-and-swap (`--force-with-lease=<ref>:<sha>`): a
  branch that moved after discovery is reported, not deleted.
- An interrupt (`Ctrl-C`) starts no remote operation; the workspace is retained
  and recovery pointers are logged.
- Accepted residual: the production circuit breaker observes the harness's
  ticks. Its counter (`circuit.json`) lives in the isolated harness state. A
  multi-tick plan-gated run whose reconcile ticks pick nothing new can reach
  `circuit_breaker.no_progress_threshold` (default 3) before the harness's own
  `tick_stalled` / `tick_budget_exhausted` stops the run. The isolated config
  sets no `slack_channel`; without a valid project channel, no notification
  subprocess runs and these observations do not advance `last_alert_at`. If a
  valid channel is configured in the sandbox's `.hermes/project.toml`, alerts
  use a **real** `hermes send --to slack:<channel> -- <message>` subprocess.
  Configured alerts are deduped per `alert_dedup_hours` (default 24).

## Local verification and recovery

`tests/test_harness_embedded_cli.py::test_real_cli_pins_harness_embedded_plan_across_ticks`
invokes the real CLI entrypoint for `todos create`,
`plan validate --todo 42 --require-manifest`, `todos audit`, `doctor`, `tick`, and
`todos complete`. It controls external GitHub/Hermes, selection, and worker
responses while retaining production registration, prompt preparation, and
reconciliation. An independently hand-authored golden Plan checks published
bytes and the digest delivered through `PlanReference`, including LF/CRLF
normalization, repeated ticks, review/finish prompts, and completion preserving
the Plan. It also checks normalization behavior and no tracked Plan files.
`tests/test_harness_e2e.py` exercises orchestration and cleanup with a local bare
remote and a mocked tick; it does not independently prove production ticking.
These are provider-free checks. Live GitHub/Hermes behavior requires a separate
live harness run and is not established by these tests. Earlier live Codex and
Claude evidence applies only to the revision it exercised; it does not qualify
the schema-v6 scheduler or arbitrary custom profiles. The live-safe profile
allow-list still applies.

Retained clones carry `.hermes/todo-create-input/<uuid>.json` and, on partial
creation, `.hermes/todo-create/<uuid>.json`; the production
`.hermes/todo-create.lock` coordinates retries. The Git directory contains
`harness-anchor.json` (identity, parent, anchor, and pending/completed state) and
its lock. Keep these alongside the report and tick log when ownership or cleanup
is uncertain. Do not infer ownership from the issue title or synthesize another
anchor after HEAD movement. Roll back this harness change by reverting it; no
persisted-schema migration is required. Preserve uncertain remote resources and
recovery workspaces during rollback.

## Known limitations

- The plan gate is exercised live by the default `native-sdd` profile: the
  fixture Plan carries a `json tpo-plan` manifest and the harness drives the run
  across ticks to its human merge gate. Under `gstack` and `agent-skills`
  (`requires_plan` false) the gate is still not exercised.
- A plan-gated run stops at the human merge gate by design: the merge itself,
  and therefore post-merge closeout, is never exercised live.
- Only the `happy-path` fixture exists.
- Loop snapshots do not carry across CLI invocations, so cross-invocation
  auto-diff is unavailable.
- The outbox retry path (`drain_outbox`) does not carry `tick_id`/fixture
  metadata on a queued-and-retried card (pre-existing gap).

## Related

- [CLI reference: `tpo test`](reference-cli.md#tpo-test)
- [Harness production-code coverage checklist](checklist-harness-production-coverage.md)
- [How to: Eval Suite](howto-eval-suite.md) — Live API selection agent tests
- [Original (offline) harness plan](superpowers/plans/2026-07-14-mock-integration-test-harness.md) — historical
