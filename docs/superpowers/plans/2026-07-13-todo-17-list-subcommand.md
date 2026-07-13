# TODO-17 `--list` Subcommand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--list` subcommand to the todos-manager skill that displays all active TODO entries from TODOS.md in a formatted markdown table.

**Architecture:** Pure skill change — one new workflow section in SKILL.md. Parses TODOS.md (and optionally TODOS-archive.md) using regex, extracts status/ID/title/summary, displays as a markdown table. Read-only, no files modified.

**Tech Stack:** SKILL.md (prompt-based skill), regex parsing, markdown table output.

## Global Constraints

- Skill-only change — no Python code, no new files beyond SKILL.md edit.
- Output is plain-text markdown rendered in chat.
- Scope: `--list` and `--all` flag only. No filtering, search, sort, or status toggle.
- Read-only — no files modified.
- SKILL.md total size should remain reasonable; the `--list` section target is ~30 lines.
- Branch: `debug/todos-manager`.

---

### Task 1: Add `--list` workflow section to SKILL.md

**Files:**
- Modify: `skills/todos-manager/SKILL.md` — add new workflow section after `--archive`, add to "When to use" list

**Interfaces:**
- Consumes: none (standalone workflow)
- Produces: new `--list` subcommand available in skill

- [ ] **Step 1: Add `--list` to "When to use" section**

Edit the "When to use" subsection (line ~18 area) to include `--list`:

```markdown
### When to use

- Adding a new entry to an existing TODOS.md file (`--add`)
- Initializing TODOS.md in a new project (`--init`)
- Converting an existing TODOS.md to enforced format (`--convert`)
- Auditing TODOS.md for format compliance (`--audit`)
- Listing active TODO entries (`--list`)
- Archiving completed TODOs to TODOS-archive.md (`--archive`)
```

- [ ] **Step 2: Add `--list` workflow section**

Insert a new workflow subsection after `--archive` (before the `---` that precedes "Audit Report Format"). The exact text to insert:

```markdown
### `--list`: List active TODO entries

1. **Validate context:** Does TODOS.md exist? If not, print "TODOS.md not found. Run `todos-manager --init` first." and exit.
2. **Scan TODOS.md** for all lines matching `- (\[[ →x~]\])` containing `TODO-(\d+)`.
3. **If no entries found in TODOS.md:**
   - If `--all` was passed: skip the active table (do not exit) and continue to step 6 to show archived entries.
   - If `--all` was NOT passed: print "No active TODOs found." and exit.
4. **For each match**, extract:
   - Status marker: `[ ]` → Pending, `[→]` → In Progress, `[x]` → Done, `[~]` → On Hold
   - ID: `TODO-<n>`
   - Title: text between `TODO-<n>: ` and the closing `**` bold delimiter (strip `**` markup)
   - Summary: text after ` — ` on the header line. If ` — ` is not present, display `[no summary]`.
   - If any field cannot be extracted from a matching line, display `[not set]` in the corresponding column.
5. **Display output** as a formatted markdown table (entries sorted by ID ascending):
   ```
   ### Active TODOs

   | ID | Status | Title | Summary |
   |----|--------|-------|---------|
   | TODO-1 | Pending | Example title | One-line summary |
   ```
6. **If `--all` flag is present**, also scan TODOS-archive.md (if exists):
   - Parse with the same rules (steps 2-4)
   - Display as a separate table section labeled "Archived TODOs" below the active table
   - If TODOS-archive.md does not exist or contains no entries, skip the archived section silently
7. **Print summary line:**
   - Without `--all`: "Showing N active entries."
   - With `--all`: "Showing N active entries. M archived entries."

Report only — no files modified.
```

- [ ] **Step 3: Update subcommand count in workflow intro**

Update the workflow intro line (approximately "The skill supports five subcommands.") to read:

```markdown
The skill supports six subcommands. Each has its own workflow below.
```

- [ ] **Step 4: Verify SKILL.md consistency**

Read the full SKILL.md and verify:
- The `--list` section appears in the correct position (after `--archive`, before the audit report format divider).
- "When to use" includes `--list`.
- The subcommand count is updated.
- No duplicate sections or orphaned references.
- Total file length is reasonable (should be ~450 lines, up from 416).

- [ ] **Step 5: Commit**

```bash
git add skills/todos-manager/SKILL.md
git commit -m "$(cat <<'EOF'
feat(todos-manager): add --list subcommand for displaying active TODOs

Adds --list workflow section to SKILL.md with --all flag support
for archived entries. Read-only, output as markdown table.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
