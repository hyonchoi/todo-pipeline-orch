# Audit Report Format, Error Messages, Observability

## Audit Report Format

```markdown
## TODOS.md Audit Report

Schema version: 2.0
Scanned: TODOS.md (N entries), TODOS-archive.md (M entries)
ID range: 1-max_id

Issues found: K
- TODO-X: Missing required field **Decisions:**
- TODO-Y: Invalid dependency reference `TODO-Z` (not found)
- TODO-W: Status marker `[->]` — expected `[→]`

NEXT_TODO_ID: 24 (valid)
ID gap check: OK (max=23)
```

`--audit` reports and repairs section-layout issues before entry schema findings. It repairs missing, malformed, duplicated, misplaced, stale, or conflicting `NEXT_TODO_ID` metadata by writing exactly one `NEXT_TODO_ID: <n>` line under `## Metadata`. The report states whether the value was valid, inserted, or corrected.

---

## Error Messages

### T8 convention: Path + remediation verb

Each error message **names the absolute file path** and a one-line action verb. Examples:

```
Error: /path/to/TODOS.md does not exist.
Remediation: Create the file or run `todos-manager --init`.

Error: Title must be 10–200 characters.
Remediation: Edit your input and re-enter the title.

Error: **What:** field is empty.
Remediation: Describe what needs to be done (required).

Error: **Why:** field must be 10–200 characters.
Remediation: Provide a rationale for why this task matters.

Error: **Decisions:** field is missing.
Remediation: Set key decisions: Priority, Effort, Phase, Branch, Test Coverage, Security Review, UI Review.

Error: Dependency TODO-99 does not exist in TODOS.md or TODOS-archive.md.
Remediation: Check the list of valid IDs or remove TODO-99 from the depends_on list.

Error: Status marker "[->]" is not recognized.
Remediation: Use one of: [ ] pending, [→] in progress, [x] done, [~] on hold.

Error: NEXT_TODO_ID must be a positive base-10 integer.
Remediation: Run `todos-manager --audit` to repair the tracked metadata.
```

Attachment audit findings identify the TODO, field, stored path, and exact
defect. Emit one finding and its remediation per invalid path; do not rewrite
the attachment value. Examples:

```text
- TODO-12 **Plan:** `docs/missing-plan.md` does not exist.
  Remediation: Choose an existing document file.

- TODO-13 **Spec:** `docs/specs` is a directory, not a regular file.
  Remediation: Choose an existing document file.

- TODO-14 **Plan:** `../outside-plan.md` resolves outside the repository.
  Remediation: Choose a file inside the repository root.

- TODO-15 **Spec:** `docs/external-spec.md` is a symlink that resolves outside the repository.
  Remediation: Choose a file inside the repository root.

- TODO-16 **Reference:** `docs/research,notes.md` contains a literal comma.
  Remediation: Use one comma-separated Reference path per value; rename paths containing commas.
```

### Error & Rescue Map

| Error | Root Cause | Remediation |
|-------|-----------|-------------|
| TODOS.md not found | First-run on new project | Run `todos-manager --init` |
| Title is empty or too short | Invalid input | Re-enter title (10–200 characters) |
| **What:** is empty | Missing required field | Re-enter What description |
| **Why:** is too short or too long | Invalid input | Re-enter Why (10–200 characters) |
| **Decisions:** is missing | Missing required field | Provide key decisions with backtick-delimited values |
| Dependency TODO-<n> does not exist | Invalid reference | Verify TODO-<n> exists in TODOS.md or archive |
| Invalid status marker | Typo in marker | Use one of: [ ], [→], [x], [~] |
| Invalid NEXT_TODO_ID | Missing, malformed, duplicated, misplaced, stale, or conflicting tracked metadata | Run `todos-manager --audit` or continue with `--add` to reconcile it |

---

## Observability

The skill logs the following to `.claude/gstack/todos-manager.log`:

```
[2026-06-11T10:30:45Z] todos-manager: start
[2026-06-11T10:30:45Z] todos-manager: next_todo_id - read 9 from ## Metadata
[2026-06-11T10:30:45Z] todos-manager: next_id = TODO-9
[2026-06-11T10:30:50Z] todos-manager: user_input - title="Refactor state module"
[2026-06-11T10:30:55Z] todos-manager: auto-research - derived Why from design doc
[2026-06-11T10:30:57Z] todos-manager: auto-research - gap: Priority (no blocking signal found)
[2026-06-11T10:31:00Z] todos-manager: preview - gate reached
[2026-06-11T10:31:02Z] todos-manager: user_action - confirm="edit"
[2026-06-11T10:31:05Z] todos-manager: user_input - title="Refactor state module (v2)"
[2026-06-11T10:31:15Z] todos-manager: preview - gate reached (retry 2)
[2026-06-11T10:31:17Z] todos-manager: user_action - confirm="y"
[2026-06-11T10:31:17Z] todos-manager: write - inserted at line 42 and advanced NEXT_TODO_ID to 10
[2026-06-11T10:31:17Z] todos-manager: done - TODO-9 committed
```
