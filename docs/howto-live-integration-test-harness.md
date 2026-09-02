# How to run the Live Integration Test Harness

`tpo test` drives one production `tpo tick` against a **live, disposable GitHub
sandbox repository**. It clones the sandbox, files a real `tpo:todo` issue,
commits a Plan, runs the production tick as a subprocess, follows the Hermes
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
| `.gitignore` | Ignores `.hermes/` (per-run state never lands in a PR), agent scratch (`.superpowers/`, `.code-review-graph/`), and Python artifacts (`__pycache__/`, `*.py[cod]`, `.venv/`) |
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
  does not ignore `.hermes/` is replaced, then the result is pushed to the
  default branch, whatever its name.
- Returns `already_seeded` (no writes, no push) when every seed file is tracked
  and `.gitignore` already carries the required rule.
- Per-run `sandbox_seed_check` requires only `pyproject.toml`,
  `tests/__init__.py`, `docs/harness/SANDBOX.md` tracked at HEAD and a
  `.gitignore` containing `.hermes/`; `README.md` is seeded but not required.

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
- `--profile` defaults to `gstack`. Only profiles on the live-safe allow-list
  (`gstack`, `agent-skills`) are accepted; every other profile fails with
  `unsafe_terminal` before anything remote is touched. Unknown profiles,
  `Unverified` profile/client pairs, and missing locally verifiable Conditional
  Hermes skills are also rejected up front.
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
3. **Issue and plan** — create the issue
   `[harness <token>] Implement mock name normalization` carrying the mirror
   labels, then commit the Plan document locally (`docs/harness/<token>-plan.md`);
   it lands in the PR branch, never on the default branch. The issue names the
   branch `feat/harness-<token>`, but the agent may choose another name; in a
   kept workspace the actual branch is in `projects/<slug>/.hermes/pipeline_branch.txt`.
4. **Tick** — wait until GitHub's label-filtered listing shows the run's issue
   as the only ready `tpo:todo` issue (the listing lags a fresh create by
   seconds; `issue_not_visible` after 60 s), then run the production `tpo tick`
   as a subprocess with an isolated `TPO_CONFIG_FILE` (log at
   `artifacts/tick.log`) and recover its registration (`tick_id`, expected
   phase keys).
5. **Poll** — follow the registered kanban cards until every card is terminal,
   auto-completing gate cards behind their predecessors.
6. **PR invariant** — exactly one open, unmerged pull request attributable to
   this run must exist and target the default branch (a `pr_invariant_failed`
   event with `pr_missing`, `pr_ambiguous`, `pr_closed`, `pr_merged`, or
   `pr_wrong_base` otherwise). When discovery itself cannot classify every
   branch or PR the run fails with `pr_discovery_incomplete` instead; no
   `pr_invariant_failed` event is emitted for that case.
7. **Shutdown** (always runs once the issue exists, even after a failure) —
   cancel the tick's kanban tasks, prove quiescence from the archived-inclusive
   snapshot, discover remote artifacts by run provenance (every non-default
   commit must descend from the run's plan commit), close the issue, close the
   PR, and delete the branch with `git push --force-with-lease`. Anything that
   could not be verified or removed is printed as a leftover with the manual
   command that finishes it.
8. **Report** — `artifacts/reports/report.json` and `report.md`, plus a
   `[kanban]` summary line; the workspace is removed after a clean shutdown
   (also on exit 1) unless `--keep` was passed or cleanup did not complete.

## Output and exit codes

```
[kanban] tenant=mock-project tick_id=01ARZ3... profile=gstack repo=OWNER/mock-project issue=#12 pr=#13 phases={...} report=~/.hermes/tmp/harness-.../artifacts/reports/report.json keep=no (temp dir will be removed)
```

| Code | Meaning |
|------|---------|
| 0 | Every phase passed, the PR invariant held, and cleanup completed |
| 1 | Phase failure, overall timeout, convergence halt, PR invariant failure (`pr_missing`, `pr_ambiguous`, `pr_closed`, `pr_merged`, `pr_wrong_base`), `pr_discovery_incomplete` at the post-run check, or tick failure (`picked_none`, `failed_to_spawn`, `tick_timeout`, `tick_failed`). The workspace is deleted after a clean shutdown. |
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
(`picked_none`, `failed_to_spawn`, `tick_timeout`/`tick_failed`, phase failure,
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
prints a `[kanban]` line with `pr=#M` and `phases={...: done, ...}`, and its
`report.json` (kept with `--keep`) records every phase `done`, the issue and PR
numbers, and no leftovers.

## Troubleshooting

| Code | Meaning / fix |
|------|---------------|
| `repo_missing` | No `--repo` and `TPO_HARNESS_REPO` unset |
| `invalid_repo` | Repository is not `OWNER/NAME` |
| `invalid_slug` | The repository name is not a valid tpo project slug (it doubles as the kanban tenant) |
| `gh_permission` | Viewer lacks WRITE/MAINTAIN/ADMIN on the sandbox |
| `gh_viewer_unknown` | `gh api user` returned no usable login; re-run `gh auth login` |
| `gh_override_forbidden` | `TPO_GH_BIN` is set; unset it |
| `sandbox_not_seeded` | Seed files or the `.hermes/` ignore rule are missing; run `--init-sandbox` |
| `sandbox_not_empty` | `--init-sandbox` refused a repo tracking files outside the seed set / `.github/**` |
| `seed_incomplete` | `--init-sandbox` committed but a seed file is not a blob at HEAD; the init workspace is removed again, so re-run `--init-sandbox` (nothing was pushed) |
| `default_branch_unknown` | `--init-sandbox`: `gh` reports no default branch but `git ls-remote` advertises refs; set the default branch on GitHub |
| `default_branch_unset` | `--init-sandbox` pushed `main` but GitHub did not report it as default; set it manually |
| `default_branch_mismatch` | `--init-sandbox`: the cloned branch differs from the reported default branch |
| `workspace_exists` | `--init-sandbox`: `<workspace>/<slug>` already exists; remove it and retry |
| `sandbox_not_quiescent` | Another open `tpo:todo` + `ready-for-agent` issue exists (detail lists the numbers); close it |
| `issue_not_visible` | GitHub's label-filtered listing did not show the run's issue as ready within 60 s of creation; check `gh issue view <N> --repo <owner/name>` (labels present?) and re-run |
| `unknown_fixture` | Only `happy-path` exists |
| `unsafe_terminal` | Profile is not live-safe; `native-sdd` is excluded because it is multi-tick |
| `picked_none` | The tick selected no issue; re-run with `--keep`, then read `artifacts/tick.log` and check the issue labels |
| `failed_to_spawn`, `tick_timeout`, `tick_failed` | Production tick problems; re-run with `--keep`, then read `artifacts/tick.log` |
| `pr_missing`, `pr_ambiguous`, `pr_closed`, `pr_merged`, `pr_wrong_base` | PR invariant failed (none, several, a non-open attributable PR, or a PR whose base is not the default branch; a wrong-base PR is still cleaned up) |
| `pr_discovery_incomplete` | Provenance could not classify every branch/PR at the post-run check (exit 1; the workspace is removed after a clean shutdown). If the shutdown discovery fails too, cleanup is skipped for safety, the run ends with `cleanup_incomplete` (exit 2) and the leftovers list the manual commands |
| `cleanup_incomplete` | At least one remote operation failed; the detail names the retained workspace; finish the printed commands, then remove it |
| `issue_unverified`, `issue_ambiguous` | The created issue could not be reconciled by title/token after a `gh` failure; close duplicates by hand |

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
| `create_harness_issue`, `reconcile_created_issue`, `commit_plan` | Live issue with mirror labels; Plan committed locally |
| `run_tick`, `recover_tick_registration` | Production `tpo tick` subprocess and its registration |
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
  by provenance (descent from the run's plan commit). Accepted residual: an
  operator branch cut from the default branch mid-run and fast-forwarded by the
  agent is indistinguishable from a run branch and would be deleted.
- Destructive remote cleanup never runs while an agent may still be pushing: on
  an unconfirmed cancel, a quiescence timeout, or any discovery failure only the
  issue is closed and everything else becomes a leftover.
- Branch deletion is compare-and-swap (`--force-with-lease=<ref>:<sha>`): a
  branch that moved after discovery is reported, not deleted.
- An interrupt (`Ctrl-C`) starts no remote operation; the workspace is retained
  and recovery pointers are logged.

## Known limitations

- The plan gate (`Plan` field and manifest) is not exercised live: `gstack` and
  `agent-skills` have `requires_plan` false. `native-sdd` is excluded
  (`unsafe_terminal`) because it is multi-tick.
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
