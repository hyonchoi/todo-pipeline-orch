# How to manage TODOs as GitHub Issues

This guide shows how to file, prepare, triage, audit, pause, and complete
pipeline TODOs. The backlog lives in GitHub Issues on the project's github.com
`origin` ([ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md)); a TODO is
an open issue carrying the `tpo:todo` label, and its canonical ID is
`TODO-<issue-number>`. The full label vocabulary, body contract, and eligibility
rules are in [issue tracker conventions](agents/issue-tracker.md#tpo-backlog-items).

## Prerequisites

- `tpo` installed and the project registered under `projects_dir` with a
  pipeline contract (`tpo init <project>`); see the
  [getting-started tutorial](tutorial-getting-started.md).
- `gh` >= 2.44 on `PATH`, authenticated against the project's `origin`
  (`gh auth status`).
- Write access to the repository (labels and issue edits).
- The "TPO TODO" form is repository-local: copy
  `.github/ISSUE_TEMPLATE/tpo-todo.yml` from this repository into
  `<project>/.github/ISSUE_TEMPLATE/` and commit it (or use the scripted
  `render_issue_body` + `--body-file` path). `tpo todos audit` reads the
  project's copy for the allowed Phase options.

## Bootstrap the label vocabulary (once per repository)

GitHub silently drops form labels that do not exist on the repository, so
create the vocabulary before anyone files a TODO:

```bash
tpo todos labels sync <project>
```

The command creates every missing `tpo:*`, triage, and mirror label
(`priority:*`, `effort:*`, `test-coverage:*`, `security-review:*`,
`ui-review:*`). Existing labels are left untouched, including color and
description; rerunning prints `labels up to date (<N> names present;
color/description not compared)`. Exit codes: 0 synced or up to
date, 1 GitHub failure (partial progress is printed as `created: <label>`
lines), 2 unknown project. `tpo doctor <project>` reports
`INVALID: missing <labels>; Fix: tpo todos labels sync <project>` until the
vocabulary is complete.

## File a TODO

Use the "TPO TODO" issue form (`.github/ISSUE_TEMPLATE/tpo-todo.yml`). From the
CLI, open it in the browser:

```bash
gh issue create --web --template "TPO TODO"
```

`--template` takes the form *name*; the terminal `gh issue create` flow cannot
render YAML forms. The form applies `tpo:todo` + `needs-triage` and renders one
`### <Section>` per field, which `parse_issue_body` consumes. Do not put `###`
headings inside field text.

For scripted creation, render the body with `render_issue_body`
(`hermes_pipeline/github_issues.py`) and pass it with `--body-file <file>` plus
explicit `--label` flags, as the migration script does.

`Depends on` is not a body field. Record dependencies as native issue
dependencies (below) after both issues exist.

## Before you file: Plan readiness

`Plan` is optional when filing but required before a plan-gated profile can
select the issue ([ADR-0001](adr/0001-plan-is-the-execution-authority.md)).
Attach documents by role and check each candidate before you paste its path:

- **Roles.** `Plan` is the execution authority and the sole field that makes a
  TODO actionable: ordered implementation work, concrete change targets, and
  verification or acceptance steps. `Spec` is the authoritative outcome
  contract. `Reference` is supplementary, non-authoritative context
  (comma-separated). A Plan without a Spec is actionable; a Spec without a Plan
  is not. One document may serve as both Plan and Spec, but it is not
  duplicated as a Reference.
- **Candidates.** A finalized gstack office-hours or plan-eng-review document or
  a Superpowers implementation plan is a first-class Plan candidate. Do not
  qualify a document merely because of its filename, recency, location, or
  author.
- **Relevance.** Strongest to weakest: an explicit TODO ID or task context in
  the document, a close subject-and-scope match with the TODO's title and
  summary (generic planning words such as "plan", "spec", "task", "review" do
  not count), and substantial overlap with concrete change targets. A generic
  subject substring alone is not relevance.
- **Paths.** Store repository-relative POSIX paths to existing regular files
  inside the repository. Never attach absolute paths, directories, files under
  `.git`, `.worktrees`, archives, generated or vendored trees, or symlinks that
  resolve outside the repository. Write the path in canonical form:
  `tpo plan validate` accepts `./`, `//`, and `..` forms, but `tpo doctor`
  reports such an issue as `plan_invalid:non_canonical`.
- **Confirm before you attach.** Discovery suggests; a human decides. Never
  replace an existing `Plan`, `Spec`, or `Reference` value because a new
  candidate was found.

The Plan manifest's `todo_id` must equal `TODO-<issue-number>`, so file the
issue first, then write the Plan (see the [Plan template](templates/tpo-plan.md))
and validate it against the issue:

```bash
tpo plan validate <project> --todo <N>
tpo plan validate <project> --todo <N> --require-manifest
tpo plan validate <project> --todo <N> --plan docs/pipeline/TODO-<N>-plan.md --require-manifest
```

Without `--plan`, the Plan path is resolved from the issue body
(`plan_invalid:missing` / `plan_invalid:duplicate` when there is not exactly one
`### Plan` value). `--plan` validates a repository-relative candidate before you
put it in the issue. `--require-manifest` rejects a legacy Markdown Plan with no
`json tpo-plan` block; without it a manifest-free Plan passes with
`warning: no tpo-plan manifest` and compiles to a single development card. A
closed issue appends `warning: issue is closed (...)`. See the
[CLI reference](reference-cli.md#plan-validate) for the failure codes.

## Triage to `ready-for-agent`

Only issues carrying both `tpo:todo` and `ready-for-agent` are selectable; any
pending-triage label (`needs-triage`, `needs-info`, `ready-for-human`,
`wontfix`) blocks selection. When the TODO is fully specified:

```bash
gh issue edit <N> --remove-label needs-triage --add-label ready-for-agent
```

The label roles are described in [triage labels](agents/triage-labels.md).
`tpo doctor <project>` prints `Plan readiness: eligible=N blocked=N (...)` with
the blocked reasons grouped by prefix (for example `status_closed`,
`dependency_incomplete`, `branch_invalid`, `plan_invalid`), so you can see
whether a triaged issue is actually selectable.

## Audit issue bodies and normalize labels

Decisions (Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI
Review) live in the issue body; labels are mirrors, and the body wins on
conflict.

```bash
tpo todos audit <project>                    # every open tpo:todo issue
tpo todos audit <project> --todo <N>         # one issue, open or closed
tpo todos audit <project> --fix --dry-run    # print the label changes
tpo todos audit <project> --fix              # apply them
```

Findings are printed as `TODO-<N>: <finding>`:

| Finding | Meaning | Fixed by `--fix` |
|---|---|---|
| `missing-section:<Name>` | A required `### <Name>` section is absent | no — edit the body |
| `duplicate-section:<Name>` | A known section appears more than once | no — edit the body |
| `plan:missing` | No `### Plan` value (informational; blocks only plan-gated profiles) | no |
| `plan:duplicate` | More than one Plan value | no — edit the body |
| `plan:invalid:<code>` | The Plan path or manifest fails `tpo plan validate` | no — fix the Plan |
| `branch:invalid` | Not exactly one Branch, or not a valid git ref name | no — edit the body |
| `branch:default` | Branch equals the repository default branch | no — edit the body |
| `decision:<Name>:<value>` | A decision value is outside the label vocabulary or phase options | no — edit the body |
| `label:missing:<label>` | The body implies a mirror label the issue lacks | yes |
| `label:extra:<label>` | A mirror label contradicts the body | yes (removed) |
| `state:closed` | The issue is closed (informational; `--todo` only) | no |
| `not-a-todo` | `--todo` named an issue without `tpo:todo` | no |

The summary line is `audit: issues=N findings=N fixable=N`, extended with
`skipped=N applied=N` under `--fix`. `--fix` only touches mirror-prefix labels
and skips closed issues and non-TODO issues. Exit codes: 0 no actionable
finding (`plan:missing` and `state:closed` are informational), 1 actionable
findings remain or a fix was skipped or failed, 2 usage (`--dry-run` without
`--fix`) or unknown project.

## Pause a TODO and record dependencies

Add `tpo:on-hold` to pause an issue; remove it to resume:

```bash
gh issue edit <N> --add-label tpo:on-hold
gh issue edit <N> --remove-label tpo:on-hold
```

A paused issue is excluded from selection; if it is already registered, the
next tick stops the run at a `needs_input` boundary (see
[pausing a TODO](howto-debugging-and-recovery.md#pausing-a-todo)).

Dependencies are GitHub native issue dependencies. Add an edge with the
blocker's numeric **database id** (not its `#number`):

```bash
blocker_id=$(gh api repos/<owner>/<repo>/issues/<blocker> --jq .id)
gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id="$blocker_id"
```

An issue with an open blocker is blocked (`dependency_incomplete:<n>`;
`dependency_unknown` when GitHub cannot report the summary) until every blocker
is closed.

## Complete a TODO

Normally you do nothing: once the pull request merges, the next tick's closeout
comments on the issue, closes it, removes `tpo:in-progress`, and writes the
`issue-closed` run marker (`delivered`). The closeout is idempotent and retries
on GitHub propagation lag.

Run the same state machine by hand when a run was lost or you merged outside
the pipeline:

```bash
tpo todos complete <project> --todo <N> --pr <PR> [--date YYYY-MM-DD] [--force]
```

| Exit | Meaning |
|---|---|
| 0 | `completed` — issue closed, marker comment present, label removed |
| 1 | GitHub failure (`Error: <code>`) |
| 2 | Refused or usage: the PR is not merged, or a run for the issue is still active |
| 3 | `pending` — GitHub has not yet reflected the close; rerun later |

`--force` closes the issue although the PR is not merged, although a run for
the issue is still active, or although the issue was already closed against a
different PR (`completion_conflict`). It never overrides an issue closed as
`not_planned`. `--date` defaults to today (UTC).

## Look up a legacy `TODO-<n>` ID

Issues migrated from `TODOS.md` carry a `legacy-id:TODO-<n>` label and a
`### Legacy ID` section; legacy IDs are never reused as issue numbers.

```bash
gh issue list --state all --label legacy-id:TODO-43 --json number,title
```

The mapping table is in the [migration notes](migration/todos-to-issues.md).

## Recovery

Stale `tpo:in-progress` labels, abandoned runs, `ISSUE DRIFT`, and
`REGISTRATION UNSUPPORTED` are covered in
[How to debug pipeline ticks and recover runs](howto-debugging-and-recovery.md#recovering-runs-and-issue-state).

## Related

- [Issue tracker conventions](agents/issue-tracker.md#tpo-backlog-items) — label vocabulary, body contract, eligibility
- [Triage labels](agents/triage-labels.md) — the five triage roles
- [CLI reference](reference-cli.md#todos) — `todos complete`, `todos labels sync`, `todos audit`
- [Plan template](templates/tpo-plan.md) — the `json tpo-plan` manifest
- [ADR-0001](adr/0001-plan-is-the-execution-authority.md) and [ADR-0003](adr/0003-github-issues-are-the-todo-backlog.md)
