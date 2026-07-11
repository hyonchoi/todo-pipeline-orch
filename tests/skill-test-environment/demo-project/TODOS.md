# TODOS

> **Format rules (enforced by `todos-manager` skill):**
> - Entry header: `- [ ] **TODO-<n>: <Title>** — <Summary>`
> - Status: `[ ]` pending, `[→]` in progress, `[x]` done, `[~]` on hold
> - Required fields: **What:**, **Why:**, **Decisions:**
> - Optional fields: **Pros:**, **Cons:**, **Context:**, **Depends on:**, **Assumptions:**, **Completed:**, **Resolved design:**
> - ID: sequential, immutable. Next = max(all IDs in TODOS.md + TODOS-archive.md) + 1
> - Completed entries: archived to `TODOS-archive.md` via `todos-manager --archive`

- [ ] TODO-1: Implement CLI argument parsing — Add argparse-based CLI for pipeline commands
  - **What:** Add argparse-based CLI for pipeline commands
  - **Why:** Current script is invoked with hardcoded paths
  - **Decisions:** Priority `P0`, Effort `S`, Phase `1 (Setup)`, Branch `feature/cli`, Test Coverage `필요`, Security Review `불필요`

- [→] TODO-2: Add rate limiting to API calls — Implement exponential backoff for external API calls
  - **What:** Implement exponential backoff for external API calls
  - **Why:** Prevent hitting rate limits during bulk operations
  - **Decisions:** Priority `P1`, Effort `M`, Phase `3 (Feature)`, Branch `feature/rate-limit`, Test Coverage `필요`, Security Review `불필요`

- [x] TODO-3: Set up project scaffolding — Create uv project structure with pyproject.toml
  - **What:** Create uv project structure with pyproject.toml
  - **Why:** Need proper dependency management
  - **Decisions:** Priority `P0`, Effort `S`, Phase `1 (Setup)`, Branch `main`, Test Coverage `불필요`, Security Review `불필요`
  - **Completed:** v0.1.0, 2026-06-15

- [~] TODO-4: Explore Slack integration — Investigate Slack bot webhook for pipeline notifications
  - **What:** Investigate Slack bot webhook for pipeline notifications
  - **Why:** Team wants real-time alerts on pipeline failures
  - **Decisions:** Priority `P2`, Effort `L`, Phase `5 (Exploration)`, Branch `feature/slack`, Test Coverage `필요`, Security Review `필요`

- [ ] TODO-6: Entry with missing optional fields — Test that entries without Pros/Cons/Context are valid
  - **What:** Test that entries without Pros/Cons/Context are valid
  - **Why:** Optional fields should not cause validation failures
  - **Decisions:** Priority `P3`, Effort `S`, Phase `2 (Design)`, Branch `feature/minimal`, Test Coverage `불필요`, Security Review `불필요`

- [ ] TODO-7: Entry with dependency references — Add entry that depends on TODO-1 and TODO-4
  - **What:** Add entry that depends on TODO-1 and TODO-4
  - **Why:** Dependencies must be validated against existing IDs
  - **Depends on:** `TODO-1`, `TODO-4`
  - **Decisions:** Priority `P1`, Effort `M`, Phase `4 (Development)`, Branch `feature/deps`, Test Coverage `필요`, Security Review `불필요`
