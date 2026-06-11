---
name: todos-manager
description: "TODOS.md 항목 추가 및 관리 — gstack 형식 기반, TODO-<n> 안정 ID 자동 부여, 핵심 결정 사항 사전 정의"
version: 2.0.0
author: hyonchoi
license: MIT
metadata:
  hermes:
    tags: [todos, gstack, planning, pipeline]
    related_skills: [gstack-plan-eng-review, gstack-office-hours]
---

## Purpose

The **todos-manager** skill automates the addition and management of TODOS.md entries in gstack-format projects. It enforces stable TODO-<n> ID assignment, captures key decisions upfront (Assigned To, Estimate, Rationale), and provides a workflow with a preview/confirm gate before writing to disk.

### When to use

- Adding a new entry to an existing TODOS.md file
- Batch-importing entries from a plan document or PR
- Ensuring consistent TODO-<n> ID sequencing across team checkpoints
- Capturing pre-defined decisions (assignee, estimate, rationale) before workflow execution
- (Future) Syncing TODOS.md with gstack project metadata (PRD, design doc, eng-review)

### Prerequisite state

- Project has a canonical `TODOS.md` file at the repo root (or `docs/gstack/TODOS.md`)
- TODOS.md follows the gstack schema (see ## TODOS.md Schema)
- User has write access to TODOS.md and `.claude/gstack/` metadata

---

## TODOS.md Schema

### File location and format

TODOS.md is stored at the repo root. Each entry occupies a single markdown list item (`- [ ] ...`), with metadata in YAML frontmatter blocks or inline comments.

### Entry structure

```markdown
- [ ] TODO-<n>: <Title>
  - **Assigned To:** <name or @handle>
  - **Estimate:** <Xh or Xd>
  - **Rationale:** <One-line why this task matters>
  - **Status:** `active` | `blocked` | `done` | `deferred`
  - **Depends on:** [TODO-<m>, TODO-<k>]
  - **Notes:** <Optional multi-line context>
```

### Entry template

When prompting the user to add an entry, use this YAML template:

```yaml
# New TODOS.md Entry Template
title: ""
assigned_to: ""
estimate: "1h"
rationale: ""
status: "active"
depends_on: []
notes: ""
```

### Example: complete entry

```markdown
- [ ] TODO-42: Refactor pipeline-watcher.py into uv modules
  - **Assigned To:** @hyonchoi
  - **Estimate:** 3d
  - **Rationale:** Unblock downstream tasks (modularization, testing, CI/CD integration)
  - **Status:** active
  - **Depends on:** TODO-40 (design review finalized)
  - **Notes:** Target modules: `orchestrator`, `state`, `rpc`. See design doc in PRD.
```
