---
name: todo-manager
description: Convert the latest finalized plan-mode implementation plan into one researched, previewed, validated GitHub TODO issue through the tpo CLI. Use after a coding plan has been finalized and the user wants it filed as executable backlog work.
---

# TODO Manager

Create one ready-for-agent GitHub TODO from finalized plan-mode output. The
deterministic `tpo todos create` command validates and persists the result; do
not compose or mutate the GitHub issue directly.

## Select the Plan

1. In the current conversation, select the latest complete
   `<proposed_plan>...</proposed_plan>` block emitted as finalized plan-mode
   output. Ignore drafts, summaries, quoted examples, and incomplete blocks.
   Stop if no finalized block exists and ask the user to finalize the Plan.
2. Strip only that block's opening and closing tags. Preserve all inner
   Markdown as `plan_markdown`; do not rewrite, summarize, or append a manifest.
   Do not submit `<proposed_plan>` tags or any other wrapper.
3. Never place credentials, tokens, authorization data, provider responses, or
   secrets in the request. If selected content or research contains one,
   redact the value and stop for user direction.

## Build the Request

Create a schema-v1 JSON request with exactly `schema_version`, a new canonical
lowercase UUIDv4 `transaction_id`, `title`, `fields`, `plan_markdown`, and
`tasks`. Do not add `Plan`, `Legacy ID`, labels, an issue number, or a TODO ID.

Derive one task per explicit implementation task in the selected Plan, in the
same order. Each task has exactly `id`, `title`, `instructions`,
`acceptance_criteria`, `verification`, and `commit_message`. Use stable IDs
such as `task-01`. Map only statements that the Plan supplies. Do not invent
task boundaries, acceptance criteria, verification commands, or commit
messages; if any required value is absent or ambiguous, ask the user to
finalize the Plan instead of guessing.

Research the repository and current issue context to fill every field required
by `tpo todos create`: `Summary`, `What`, `Why`, `Pros`, `Cons`, `Context`,
`Assumptions`, `Spec`, `Reference`, `Branch`, `Priority`, `Effort`, `Phase`,
`Test Coverage`, `Security Review`, and `UI Review`. Treat the Plan as the
authority for implementation intent, verify repository-specific claims from
current files, and preserve uncertainty as an explicit question. Resolve
uncertainty with the user before preview. Do not include raw tool output or
secret-bearing data.

Resolve one literal `PROJECT` slug and its canonical `owner/repo` GitHub
identity before asking for approval. Do not substitute either target later.

Pass the complete JSON on standard input to the bundled
`scripts/write_request.py PROJECT_ROOT UUID`. It exclusively creates
`.hermes/todo-create-input/<uuid>.json` as a contained, non-symlink regular
file with mode `0600`; never create the input with an ordinary redirect or
replace an existing path. Keep this file until the CLI reports completion; it
is the durable recovery input. Reuse it byte for byte on retries rather than
minting another transaction.

## Preview, Approve, and Create

Run the command with the resolved literal project slug and without `--yes`,
supplying EOF at its confirmation prompt, so it validates the request and
prints the canonical preview without mutating GitHub:

```sh
tpo todos create PROJECT --request-file REQUEST </dev/null
```

Show the complete output, including the CLI's `Project`, canonical
`Repository`, full title, and full body, to the user. Verify those targets equal
the resolved literal `PROJECT` and `owner/repo`, then ask for the exact reply
`create`. Edits or any target substitution invalidate approval and require
rebuilding, revalidating, and showing a new complete preview. Never invoke
`--yes` before that approval.

After the exact approval, reuse the identical literal `PROJECT`, request path,
and canonical repository binding and run:

```sh
tpo todos create PROJECT --request-file REQUEST --approved-repo OWNER/REPO --yes
```

This is the external mutation boundary. Do not infer approval from earlier
plan approval or similar words. If the CLI reports a partial or uncertain
outcome, keep the request and issue unchanged and report the error. On an
approved retry, first rerun the same command without `--issue`; its transaction
marker discovery is the normal recovery path. Add `--issue N` only when the
partial issue number and its matching transaction marker were independently
confirmed:

```sh
tpo todos create PROJECT --request-file REQUEST --approved-repo OWNER/REPO --yes
# Only after independent issue/marker confirmation:
tpo todos create PROJECT --request-file REQUEST --approved-repo OWNER/REPO --issue N --yes
```

Do not close, delete, hand-edit, or recreate a partial issue. Delete the local
request only after the CLI reports successful completion.
