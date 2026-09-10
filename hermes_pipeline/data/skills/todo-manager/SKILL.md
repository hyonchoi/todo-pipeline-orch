---
name: todo-manager
description: Deprecated name for issue-planner. Use when an older invocation requests todo-manager; explain the migration and stop.
---

# TODO Manager (deprecated)

This skill has been renamed to `issue-planner`. This installed copy is a
deprecation notice and does not create issues or execute the legacy workflow.

Install the replacement with `tpo skills install issue-planner --target codex`
or `tpo skills install issue-planner --target claude`, using `--scope project`
if appropriate. Then invoke `issue-planner` with a finalized implementation
plan in the active session. Stop here; do not silently switch workflows.

Existing installed copies retain their old behavior until explicitly upgraded
with `tpo skills install todo-manager --target codex --reinstall` (or the
corresponding Claude target and original scope). Installing `issue-planner`
does not modify an existing `todo-manager` installation.

The legacy writer resources and installer namespace remain available for
recovery. Preserve private requests, receipts, and journals. If a transaction
was interrupted, use `tpo skills recover todo-manager --target codex --finish`
or `--rollback`, with its original target and scope; do not delete or rewrite
the journal or rename its namespace. Identity drift requires reconciliation.
