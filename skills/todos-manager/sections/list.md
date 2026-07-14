# `--list`: List Active TODO Entries

1. **Validate context:** Does TODOS.md exist? If not, print "TODOS.md not found. Run `todos-manager --init` first." and exit.
2. **Scan TODOS.md** for entry header lines: `- [ ]`, `- [→]`, `- [x]`, or `- [~]` followed by `**TODO-<n>: ...`.
3. **If no entries found in TODOS.md:**
   - If `--all` was passed: skip the active table (do not exit) and continue to step 6 to show archived entries.
   - If `--all` was NOT passed: print "No active TODOs found." and exit.
4. **For each matched entry header line**, extract:
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
   - Apply the same scan and extraction rules as steps 2 and 4 (entry matching and field extraction) to TODOS-archive.md
   - Display as a separate table section labeled "Archived TODOs" below the active table
   - If TODOS-archive.md does not exist or contains no entries, skip the archived section silently
7. **Print summary line:**
   - Without `--all`: "Showing N active entries."
   - With `--all`: "Showing N active entries. M archived entries."

Report only — no files modified.
