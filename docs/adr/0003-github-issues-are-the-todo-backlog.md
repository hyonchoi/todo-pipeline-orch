# GitHub Issues are the TODO backlog

The TODO backlog lives in GitHub Issues on the project's github.com origin. A TODO is an open issue carrying the `tpo:todo` label, and its canonical ID is `TODO-<issue-number>`. Decisions live in the issue body as H3 sections; labels are mirrors, and the body wins on conflict. Dependencies use GitHub native issue dependencies. `TODOS.md` and `TODOS-archive.md` are retired; legacy IDs survive as `legacy-id:TODO-<n>` labels, a `### Legacy ID` body section, and `docs/migration/todos-to-issues.md`.

This supersedes the TODO half of ADR-0001's former tracked-file contract.
Selection authority is the identity-bound issue snapshot (repository, number,
title, normalized body) hashed into
`.hermes/runs/<tick-id>/registration.json`. For new TODOs, the sole execution
authority is the embedded Plan extracted from that snapshot and materialized as
a verified private run artifact. Existing `### Plan` repository paths remain
legacy-compatible and are pinned at the base commit. Live issue drift after
registration is a human `needs_input` boundary consistent with ADR-0002; it is
never auto-repaired.
