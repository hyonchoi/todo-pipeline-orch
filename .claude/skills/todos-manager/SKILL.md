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
