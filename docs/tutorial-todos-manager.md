# Getting started with the todos-manager skill

In this tutorial, you'll install the todos-manager skill, initialize TODOS.md for a new project, add entries with schema enforcement, and archive a completed task. You'll see the preview gate, the ID computation, and the enforced format in action.

**Time: ~10 minutes**

## What you'll need

- The `todo-pipeline-orchestrator` repo cloned
- Claude Code installed

## What you'll build

A working TODOS.md with three entries: one added, one completed, and one archived. By the end, you'll understand the full lifecycle of a TODO entry — from creation to archive.

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
3. **What** — what needs to be done
4. **Why** — why the task matters (10–200 characters)
5. **Decisions** — key choices: Priority, Effort, Phase, Branch, Test Coverage, Security Review
6. **Optional fields** — Pros, Cons, Context, Depends on, Assumptions

The skill computes the next ID automatically. Since this is the first entry, it will be `TODO-1`.

**Preview gate** — before writing, you see the full assembled entry. This is your safety net. Type `y` to confirm, `edit` to go back (the ID isn't burned), or `cancel` to abort.

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

## Step 5: Archive a completed TODO

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
- Two entries (TODO-1 archived, TODO-2 pending)
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

**Audit for compliance:**
- Run `todos-manager --audit` to check all entries for missing required fields, invalid status markers, and broken dependency references.
