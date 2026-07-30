# Harness Residual Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two residual final-review findings without reopening unrelated TODO-41 scope.

**Architecture:** Build cleanup order from each task's Hermes `show --json` parent relationships, then topologically process children before parents. Update the harness troubleshooting section to describe the selected external client rather than Claude alone.

**Tech Stack:** Python 3.12+, pytest, uv, Hermes Kanban CLI

## Global Constraints

- Never infer dependency order from Kanban list order.
- Archive every child before each parent.
- Return `False` without archiving if task relationships are malformed, cyclic, or reference an in-scope task whose topology cannot be resolved.
- Preserve confirmed worker termination and fail-closed workspace retention.
- Troubleshooting must cover both `prompt_client = "claude"` and `prompt_client = "codex"`.
- Touch only `hermes_pipeline/kanban_tasks.py`, `tests/test_kanban_tasks.py`, and `docs/howto-mock-integration-test-harness.md`.

---

### Task 1: Derive child-first cleanup and neutralize troubleshooting

**Files:**
- Modify: `hermes_pipeline/kanban_tasks.py`
- Modify: `tests/test_kanban_tasks.py`
- Modify: `docs/howto-mock-integration-test-harness.md`

**Interfaces:**
- Consumes: Hermes `kanban show <task_id> --json` payloads containing `task`, `parents`, and `runs`
- Produces: `cancel_todo_kanban_tasks(tenant: str, tick_id: str) -> bool` with topology-derived child-first reclaim/archive order

- [ ] **Step 1: Add a failing multi-task topology regression**

Create a three-task chain whose list order is deliberately parent, child,
middle. Mock `kanban show --json` so the middle names the parent and the child
names the middle. Assert reclaim and archive commands occur child, middle,
parent. Assert the test fails because current code only reverses list order.

- [ ] **Step 2: Add failing malformed and cyclic topology regressions**

Cover a non-list `parents` value, an in-scope parent reference with no matching
task, and a two-task cycle. Assert cleanup returns `False` and no archive
command runs.

- [ ] **Step 3: Run RED**

```bash
rtk uv run --locked pytest -q tests/test_kanban_tasks.py -k 'cancel and (child_first or topology)'
```

Expected: the child-first command assertion fails and invalid topology is not
rejected.

- [ ] **Step 4: Implement topology-derived ordering**

Read each in-scope task with the existing `hermes kanban show <id> --json`
boundary. Validate `task`, `parents`, and `runs`. Build edges only among tasks
for the selected tick. Topologically sort so every child precedes its in-scope
parents. Return `False` on malformed data, a missing in-scope reference, or a
cycle. Use that single order for reclaim and archive loops; retain the existing
termination confirmation and final archived snapshot checks.

- [ ] **Step 5: Update client-neutral troubleshooting**

Replace `HermesCallError / ClaudeCallError` with client-neutral external-agent
failure wording. Show `hermes chat -q "echo hello"` plus the configured
client's `claude --version` or `codex --version` command.

- [ ] **Step 6: Run GREEN and relevant suites**

```bash
rtk uv run --locked pytest -q tests/test_kanban_tasks.py -k 'cancel'
rtk uv run --locked pytest -q tests/test_kanban_tasks.py tests/test_harness.py
rtk uv run --locked ruff check hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py
rtk git diff --check
```

Expected: all commands pass.

- [ ] **Step 7: Commit atomically**

```bash
git add hermes_pipeline/kanban_tasks.py tests/test_kanban_tasks.py docs/howto-mock-integration-test-harness.md
git commit -m "Fix child-first harness cleanup ordering"
```

- [ ] **Step 8: Run exact-tip locked verification**

```bash
rtk uv run --locked pytest -q
```

Expected: full suite passes with no new skips or warnings.
