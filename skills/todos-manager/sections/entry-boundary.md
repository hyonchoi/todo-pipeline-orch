# TODOS.md Entry Boundary Parsing

## Purpose

Defines how to identify and extract individual TODO entries from TODOS.md or TODOS-archive.md. This spec is shared by `--archive` and `--revise` to prevent DRY violations.

## Entry Header Pattern

An entry starts with a markdown list item containing a status marker followed by a TODO-ID:

```
- [ ] **TODO-<n>: <Title>** — <Summary>
- [→] **TODO-<n>: <Title>** — <Summary>
- [x] **TODO-<n>: <Title>** — <Summary>
- [~] **TODO-<n>: <Title>** — <Summary>
```

Matching regex: `^(- \[[ ]|→|x|~\] \*\*TODO-\d+:\S)`.

## Entry Body

An entry continues with indented sub-bullets (lines starting with `  - ` followed by content) until:
- the next entry header (matching the pattern above), or
- end of file

All indented sub-bullets between the current entry header and the next entry header (or EOF) belong to the current entry.

Non-indented blank lines or non-entry lines (e.g., section headers, paragraph text) between entries are not part of either entry.

## Entry Extraction Algorithm

1. Scan the file line by line.
2. When a line matches the entry header pattern, mark it as the start of a new entry.
3. All subsequent lines that are indented sub-bullets (`  - `) or blank lines between sub-bullets belong to that entry.
4. The entry ends when the next entry header is found or the file ends.
5. Trim trailing blank lines from the entry text (but preserve internal blank lines between sub-bullets if they exist).

## Example

Given:
```markdown
- [ ] **TODO-1: Foo** — Bar
  - **What:** Do the foo thing
  - **Why:** Because bar

- [→] **TODO-2: Baz** — Quux
  - **What:** Build baz
  - **Why:** Need quux
  - **Decisions:** Priority `P1`
```

Entry 1 boundaries: lines 1-3 (header + 2 sub-bullets)
Entry 2 boundaries: lines 5-8 (header + 3 sub-bullets)
