# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either — resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies** — the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only — the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me` — the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.

## TPO backlog items

Backlog entries selectable by the pipeline are GitHub issues carrying the `tpo:todo` label. The canonical ID is `TODO-<issue-number>`; the number is assigned by GitHub, so file the issue before authoring a Plan manifest that names it.

- **Bootstrap once per repo**: create the label vocabulary before using the form — GitHub silently drops form labels that do not exist on the repo. Existing labels are left untouched, including color/description. Run `tpo todos labels sync <project>`.
- **Create**: the "TPO TODO" web form is canonical (`.github/ISSUE_TEMPLATE/tpo-todo.yml`); from the CLI, `gh issue create --web --template "TPO TODO"` opens it (`--template` takes the form *name*; the terminal flow cannot render YAML forms). Scripted, non-interactive creation must render the body with `render_issue_body` and pass `--body-file <file>` plus explicit `--label` flags, as the migration script does. Form-created issues get `tpo:todo` + `needs-triage`; issues migrated from TODOS.md were already triaged and carry `ready-for-agent`.
- **Body contract**: one `### <Section>` H3 per field, in `KNOWN_SECTIONS` order (`hermes_pipeline/github_issues.py`). `_No response_` means absent. One parser (`parse_issue_body`) reads both form-created and migrated issues; `Legacy ID` is migration-only and not in the form. `Depends on` is **not** a body field — use native issue dependencies as described under Wayfinding operations. Do not use `###` headings inside field text — the parser treats them as new sections.
- **Decisions** (Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review) live in the body. Labels are mirrors, normalized by `tpo todos audit <project> --fix`; the body wins on conflict.
- **Eligibility**: `tpo:todo` and `ready-for-agent` are both required. `tpo:on-hold`, `tpo:in-progress`, and any pending-triage label (`needs-triage`, `needs-info`, `ready-for-human`, `wontfix`) block selection. Non-label gates: the issue must be open (`status_closed`); open native dependencies block (`dependency_incomplete:<n>`; `dependency_unknown` when the summary is unavailable); exactly one `Branch` section (`branch_invalid`); and under a plan-gated profile (`requires_plan`) exactly one valid `Plan` (`plan_invalid:*`). `Plan` is optional when filing but required before a plan-gated profile can select the issue; it must be committed at `HEAD` (`plan_invalid:untracked` otherwise) and carry a `json tpo-plan` manifest (`plan_invalid:manifest_required` otherwise — manifest-free Plans run only under non-plan profiles). Every profile claims the selected issue with `tpo:in-progress`; automatic closeout releases it only under plan-gated (`requires_plan`) profiles. For other profiles the claim stays until `tpo todos complete <project> --todo N --pr N` after the merge, and `in_progress_stale` is the expected signal for a delivered-but-uncompleted issue.
- **Demotion**: when run registration fails on a permanent content fault (`plan_invalid`, `branch_invalid`, `branch_exists`), TPO moves the issue from `ready-for-agent` to `needs-info`; fix the body and re-add `ready-for-agent`. Infrastructure or operator-resolvable codes (`authority_untracked`, `authority_invalid`, `branch_mismatch`, git and worktree errors) never demote.

| Label | Meaning |
| --- | --- |
| `tpo:todo` | Managed TODO entry |
| `tpo:on-hold` | Paused; must not be selected |
| `tpo:in-progress` | Claimed by an active pipeline run |
| `needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix` | Triage vocabulary (see `triage-labels.md`) |
| `priority:P0` … `priority:P3` | Mirror of Priority |
| `effort:S` / `effort:M` / `effort:L` | Mirror of Effort |
| `test-coverage:required` / `test-coverage:not-required` | Mirror of Test Coverage |
| `security-review:required` / `security-review:not-required` | Mirror of Security Review |
| `ui-review:required` / `ui-review:not-required` | Mirror of UI Review |
| `phase:<n>-<slug>` | Mirror of Phase, e.g. `4 (Development)` → `phase:4-development` |
| `legacy-id:TODO-<n>` | Pre-migration TODOS.md ID (migrated issues only) |
