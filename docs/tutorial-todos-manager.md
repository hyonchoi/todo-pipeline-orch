# Getting started with the todos-manager skill

In this tutorial, you'll install the todos-manager skill, initialize TODOS.md for a new project, add entries with schema enforcement, archive a completed task, and revise an entry with missing fields. You'll see the preview gate, the ID computation, the enforced format, and the AI-pre-filled revision workflow in action.

**Time: ~15 minutes**

## What you'll need

- The `todo-pipeline-orchestrator` repo cloned
- Claude Code installed

## What you'll build

A working TODOS.md with four entries: one added with full fields, one added with a dependency, one completed and archived, and one revised with AI-pre-filled gaps. By the end, you'll understand the full lifecycle of a TODO entry — from creation through revision to archive.

---

## Step 1: Install the skill

From the repo root, run the install script:

```bash
bash scripts/install-todos-manager.sh
```

This creates symlinks in `~/.claude/skills/todos-manager/` and `~/.agents/skills/todos-manager/` pointing to `skills/todos-manager/SKILL.md`. The skill source lives at `skills/todos-manager/SKILL.md` in the repo — git-tracked and platform-neutral.

**Verify:**

```bash
ls -la ~/.claude/skills/todos-manager/SKILL.md
# Should show a symlink to .../skills/todos-manager/SKILL.md
```

---

## Step 2: Initialize TODOS.md

The `--init` subcommand creates TODOS.md with a schema preamble and a companion archive file. In a new Claude Code session, invoke the skill:

```bash
todos-manager --init
```

**What you'll see:**

```bash
cat TODOS.md
```

The file starts with `# TODOS` followed by a blockquote preamble that documents every schema rule — entry format, status markers, required fields, ID assignment. This isn't decoration. It's the contract the skill enforces.

A `TODOS-archive.md` is also created with a minimal header:

```bash
cat TODOS-archive.md
# TODOS Archive
# Completed TODOs, archived via `todos-manager --archive`.
```

You now have a blank project ready for entries.

---

## Step 3: Add your first TODO

Invoke `todos-manager --add`. The skill guides you through the fields:

1. **Title** — a concise name for the task (10–200 characters)
2. **Summary** — a one-line description after the em dash
3. **Auto-research** — the skill silently reads TODOS.md, git log, design docs, CLAUDE.md, and source files implied by the title. It derives `What`, `Why`, `Decisions`, and optional fields from what it finds. You'll see a synthesis block showing each field with a confidence tag (high/medium/low).
4. **Gap questions** — if research couldn't resolve any field, the skill asks one question at a time. Since this is the first entry with no design doc matches, you'll be asked about `Why` and `What`.
5. **Confirm or edit** — accept the pre-filled fields or adjust them.
6. **Preview gate** — before writing, you see the full assembled entry. Type `y` to confirm, `edit` to go back (the ID isn't burned), or `cancel` to abort.

```
======== PREVIEW ========
- [ ] **TODO-1: Set up database connection pool** — Avoid opening new connection per request
  - **What:** Set up a PostgreSQL connection pool for the API server.
  - **Why:** Opening a new connection per request adds latency and risks hitting connection limits under load.
  - **Decisions:** Priority `P1`, Effort `S`, Phase `4 (Development)`, Branch `feature/db-pool`, Test Coverage `필요`, Security Review `불필요`
======== END PREVIEW ========
Proceed? [y / edit / cancel]
```

Confirm with `y`. The entry appears in TODOS.md.

**Output:**
```
✓ Entry added as TODO-1.
```

---

## Step 4: Add a second TODO with a dependency

Add another entry that depends on the first:

```bash
todos-manager --add
```

When prompted for `Depends on:`, enter `TODO-1`. The skill validates that TODO-1 exists in TODOS.md before allowing the entry to be written.

**Output:**
```
✓ Entry added as TODO-2.
```

Now TODOS.md has two entries. You can see the enforced format in action — both entries have the required fields, and TODO-2 references TODO-1 as a dependency.

---

## Step 5: Add a third TODO with incomplete fields

Add an entry intentionally leaving some fields blank. This simulates a real-world scenario where you create a TODO quickly without enough context to fill in all details:

```bash
todos-manager --add
```

When prompted, provide a title and summary but accept minimal values for optional fields — or skip optional fields like `Pros`, `Cons`, and `Context` entirely. The skill writes the entry with whatever you confirmed.

**Output:**
```
✓ Entry added as TODO-3.
```

Now TODOS.md has three entries. TODO-3 has required fields but is missing optional context — exactly the situation `--revise` is designed for.

---

## Step 6: Revise TODO-3 to fill missing fields

Invoke `todos-manager --revise` to fill gaps in TODO-3 using AI-pre-filled suggestions:

```bash
todos-manager --revise
```

When prompted for the TODO ID, enter `TODO-3`. The skill scans the entry for missing or weak fields, then silently researches the codebase to pre-fill gaps. You'll see a **revision synthesis block** that shows every field:

- Fields that were already good show with `(unchanged)`
- New fields derived by auto-research show with `[Confidence: high/medium/low]`

```
======== REVISION SYNTHESIS ========
Status:          [ ] pending                        (unchanged)
What:            Implement rate-limiting middleware  (unchanged)
Why:             Prevent API overload under load     (unchanged)
Priority:        P1                                    [Confidence: high]
Effort:          M                                     [Confidence: medium]
Phase:           4 (Development)                       [Confidence: medium]
Branch:          feature/rate-limit                    [Confidence: high]
Test Coverage:   required                              [Confidence: high]
Security Review: not-required                          [Confidence: high]
Pros:            Production stability, graceful degradation [Confidence: medium]
Cons:            Migration effort, import path updates [Confidence: medium]
Context:         docs/gstack/api-rate-limiting.md      [Confidence: high]
======== END SYNTHESIS ========

Confidence: high = derived from strong codebase signal, medium = inferred from context, low = best guess.
These are pre-fills — confirm or edit each in the next step.
```

**Confirm or edit** — reply `confirm` to accept all as-is, or list edits like `Effort: L` to change individual fields.

**Preview gate** — you'll see the before and after of the full entry. Type `y` to confirm.

**Output:**
```
✓ TODO-3 revised. Updated fields: Priority, Effort, Phase, Branch, Test Coverage, Security Review, Pros, Cons, Context.
```

Verify the result:
```bash
tail -10 TODOS.md
```

TODO-3 now has the enriched fields. The revision synthesis used codebase signals — like existing design docs and git history — to pre-fill values you didn't have to type. This is the `--audit` + `--revise` loop in action: audit to find gaps, revise to fill them.

---

## Step 7: Archive a completed TODO

Mark TODO-1 as done by changing its status marker to `[x]` in TODOS.md, then run:

```bash
todos-manager --archive
```

**What it does:**
- Scans TODOS.md for `[x]` entries
- Appends them to TODOS-archive.md (newest first by ID)
- Removes them from TODOS.md

**Output:**
```
✓ Archived 1 entries to TODOS-archive.md.
```

Check the result:
```bash
cat TODOS-archive.md
```

TODO-1 is now in the archive. The next `todos-manager --add` will still compute `max(all IDs) + 1` across both files, so the next entry becomes `TODO-3` — not `TODO-1`.

---

## What you built

A working TODOS.md with:
- A schema-enforced preamble that documents the entry format
- Three entries (TODO-1 archived, TODO-2 pending, TODO-3 revised with AI-pre-filled fields)
- An archive file with completed work
- Stable IDs computed across both TODOS.md and TODOS-archive.md

### Next steps

**Manage TODOS.md in your real project:**
- See [How to manage TODOS.md with the todos-manager skill](howto-todos-manager.md) for each subcommand with options, validation rules, and troubleshooting.

**Reference:**
- [TODOS Manager skill](../skills/todos-manager/SKILL.md) — full schema, ID assignment rules, acceptance scenarios, error messages
- [Install TODOS Manager](../scripts/install-todos-manager.sh) — symlinks the skill to user-level directories

**Convert an existing TODOS.md:**
- Run `todos-manager --convert` to add the preamble and validate entries against the enforced schema.

**Audit and revise entries:**
- Run `todos-manager --audit` to check all entries for missing required fields, invalid status markers, and broken dependency references.
- Run `todos-manager --revise` to fill missing or weak fields in any entry using AI-pre-filled suggestions.
