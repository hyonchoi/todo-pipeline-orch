# `--convert` Mode B: Header-based Format Conversion

Use this section when `--convert` detects a header-based TODOS.md (`## Open` / `## Completed` sections with `### Title` entries, no canonical `- [ ] TODO-N` entries).

4b. **Parse entries:**
    - Split the file by `## ` section headers. Each section groups entries.
    - Within each section, find all `### Title` lines. Each marks a new entry.
    - For each entry, collect all `**FieldName:** value` lines until the next `###`, `##`, or EOF.
    - Store: raw title, section name, and a map of field names to values.

5b. **Derive status** for each entry (first matching rule wins):
    - `**Completed:**` field present with non-empty value → `[x]`
    - Title ends with ` — Completed` → `[x]` (strip the suffix from the title)
    - Entry is in `## Completed` section → `[x]`
    - Entry is in `## Open` section → `[ ]`
    - Section name contains "WIP", "Blocked", or "In Progress" → `[→]`
    - Section name contains "Hold", "Deferred", or "Parking" → `[~]`
    - Unknown section → `[ ]` (flag for user review)

6b. **Assign IDs:**
    - Scan TODOS.md + TODOS-archive.md for ALL `TODO-(\d+)` references.
    - Compute `base_id = max(all_ids) + 1`. If no IDs found, `base_id = 1`.
    - Assign IDs sequentially to parsed entries in document order (top to bottom).

7b. **Convertibility gate:** For each entry, check if it has enough content:
    - **Convertible:** entry has both `**What:**` AND `**Why:**` fields
    - **Not convertible:** missing `**What:**` OR `**Why:**` — insufficient context for a meaningful TODO
    - Convertible entries proceed to transformation
    - Non-convertible entries are collected for `TODOS-reference.md`

8b. **Transform fields** for each convertible entry:
    - `**Resolution:**` → `**Resolved design:**` (rename label, preserve value)
    - `**Depends on / blocked by:**` → `**Depends on:**` (rename label, preserve value)
    - All other known fields (**What:**, **Why:**, **Pros:**, **Cons:**, **Context:**, **Assumptions:**, **Completed:**) → direct copy
    - Unknown `**Field:**` labels → preserve as-is
    - If `**Decisions:**` is absent → insert: `- **Decisions:** <<USER-REVIEW>> Priority, Effort, Phase, Branch not yet determined`
    - Build the header line: `- [STATUS] **TODO-<n>: <Title>** — <Summary>` where Summary is the first sentence of `**What:**` (text up to first `. ` or end of field). If `**What:**` is absent for summary, use the first sentence of `**Why:**`. If neither exists, use `No summary available`.

9b. **Backup:** Copy current TODOS.md to `TODOS.md.backup.<YYYY-MM-DD>` (e.g., `TODOS.md.backup.2026-07-13`). If today's backup already exists, skip.

10b. **Preview gate:** Display a structured preview:
    ```
    ======== CONVERSION PREVIEW ========

    File: TODOS.md
    Format detected: Header-based (### entries)
    Entries to convert: <count convertible>
    Base ID: TODO-<base_id> (assigned <base_id> through <last_id>)

    --- Entry mapping ---
    ### Old Title  →  TODO-X: New Title  [status]
    ...

    --- Field transformations ---
      - **Resolution:** → **Resolved design:** (N entries)
      - **Depends on / blocked by:** → **Depends on:** (N entries)
      - **Decisions:** added as default (N entries need user review)

    --- Status derivation ---
      [x] done:    N entries
      [ ] pending: N entries
      [→] WIP:     N entries
      [~] on hold: N entries

    --- Non-convertible entries → TODOS-reference.md ---
      ### Entry Title (missing: What/Why)
      ...

    --- Converted output (first 3 entries shown, use --full for all) ---
    [formatted canonical entries]

    ======== END PREVIEW ========

    Proceed? [y / edit / cancel / --full]
      y      → Apply conversion to TODOS.md
      edit   → Specify entries to skip or modify
      cancel → Abort. No files modified.
      --full → Show all N converted entries in preview
    ```

    - **`y`** → Proceed to step 11b.
    - **`edit`** → Prompt which entries to skip/modify, re-show preview.
    - **`cancel`** → Print "Conversion cancelled. No files modified." and exit.
    - **`--full`** → Show all N entries, then re-prompt for y/edit/cancel.

11b. **Apply conversion on confirm:**
    - Remove `## Open`, `## Completed`, and other section headers used only for grouping.
    - Remove all `### Title` header-based entries from TODOS.md.
    - Preserve any existing canonical `- [ ] TODO-<n>` entries (if hybrid file).
    - Insert all converted entries in canonical format at the end of the file.
    - If there are non-convertible entries, write them to `TODOS-reference.md`:
      ```markdown
      # TODOS Reference

      Entries that could not be auto-converted (missing required fields).
      Use these as reference when adding entries via `todos-manager --add`.

      Generated: <ISO-8601 timestamp> from TODOS.md conversion.

      [original entry text for each non-convertible entry]
      ```
    - Count entries with `<<USER-REVIEW>>` markers.
    - **Confirm:** "✓ Converted N entries to canonical format. M entries saved to TODOS-reference.md. Z entries need user review for <<USER-REVIEW>> markers."
