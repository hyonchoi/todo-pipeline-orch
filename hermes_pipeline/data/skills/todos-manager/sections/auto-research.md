# Auto-Research Phase for --add (Step 4.5)

## Purpose

After the user provides a title and summary, silently research the codebase to
derive all todo fields. Only ask targeted questions for gaps that research
couldn't resolve. Never ask what can be determined. Run
`sections/document-attachments.md` first so explicit paths and all bounded
attachment sources are handled before general research consumes the shared
budget.

## Research signals — collect silently before any output

| Signal | What to read |
|--------|-------------|
| `TODOS.md` | Keyword-match related entries → candidate `Depends on`, existing Priority patterns |
| `TODOS-archive.md` | Prior similar work → informs `Effort` estimate |
| `git log --oneline -20` | Recent activity, branch naming conventions, phase references |
| `docs/gstack/` or `docs/superpowers/` | Design docs matching title keywords → `Why`, `What`, `Context` |
| `CLAUDE.md` | Phase definitions, branch naming rules |
| Relevant source files implied by title | Confirms scope → `What` boundaries, `Effort` sizing |

## Research budget cap

To prevent unbounded file reads on large codebases, the combined auto-research
and document-attachment discovery operation must enforce hard limits during
signal collection. Read `sections/document-attachments.md` when discovering
attachments; it shares these counters and must not start a second budget.

- **Max 20 files read** across research and attachment discovery (includes TODOS.md, TODOS-archive.md, design docs, attachment candidates, and implied source files)
- **Max 10 grep/search invocations** across research and attachment discovery
- If the cap is hit before all signals are collected, **stop researching immediately** and treat any field still undetermined as a gap
- Fall through to the gap-detection question flow rather than continuing to read additional files
- Document which signals or attachment sources were skipped due to cap exhaustion (e.g., "source file inspection skipped — budget exhausted")

## Field derivation rules

| Field | How to derive |
|-------|--------------|
| `Why` | Matching design doc rationale → related TODOS `Why` fields → git commit messages on same area |
| `What` | Title/summary + scope implied by related files found |
| `Pros` | Inverse of `Why` (what improves) + design doc benefits sections |
| `Cons` | Related TODOS `Cons` + design doc risk language + migration cost if existing code changes |
| `Priority` | Default `P2`; upgrade to `P1` if a related TODO is `[→]` or a design doc is APPROVED; upgrade to `P0` if summary contains "blocking" or "broken" |
| `Effort` | `S` = single-file change; `M` = multi-file or new module; `L` = new subsystem |
| `Phase` | Match CLAUDE.md phase list via current branch name or latest commit phase reference |
| `Branch` | Follow naming convention observed in last 5 branches (`git branch --sort=-committerdate`) |
| `Test Coverage` | `required` if `What` implies new logic or new function; `not-required` if docs-only or config-only |
| `Security Review` | `required` if title/summary contains: auth, token, secret, permission, credential, API key; else `not-required` |
| `UI Review` | `required` if title/summary contains: ui, frontend, design, visual, layout, component, css, style, dashboard, artifact, page, screen, modal, form, navigation, button, icon, animation; else `not-required` |
| `Depends on` | TODO-<n> IDs found in matching design docs, or `[→]` TODOs on related topics |
| `Context` | Path to matching design doc if found |

## Gap detection — only ask for these

After derivation, identify fields that are still empty or ambiguous. Ask gap
questions **one at a time**, in this priority order:

1. `Why` — if no design doc or related TODO rationale found
   → Ask: "Why does this matter? What breaks or stays slow without it?"
2. `What` — if scope is still vague after file search
   → Ask: "What's the minimal deliverable? What's explicitly out of scope?"
3. `Priority` — if no blocking signal found (no `[→]` TODO, no APPROVED doc, no urgency keyword)
   → Offer: `[P0] Blocking now / [P1] This sprint / [P2] Backlog / [P3] Someday`
4. `Effort` — if file scope is ambiguous
   → Offer: `[S] Hours / [M] 1–3 days / [L] Week+`
5. `Depends on` — only if the title explicitly references another task and no ID was found

Accept the user's first answer without pushing back — this is not an interrogation.

## Synthesis block

After all gaps are resolved, show:

```
======== AUTO-RESEARCH SYNTHESIS ========
Why:             <derived or answered>          [Confidence: high/medium/low]
What:            <derived or answered>          [Confidence: high/medium/low]
Pros:            <derived>
Cons:            <derived>
Context:         <path to design doc, or "(none found)">
Plan:            <none detected, suggested path and reason, or numbered unresolved choices>
Priority:        <derived or answered>          [Confidence: high/medium/low]
Effort:          <derived or answered>          [Confidence: high/medium/low]
Phase:           <derived>                      [Confidence: high/medium/low]
Branch:          <derived>                      [Confidence: high/medium/low]
Test Coverage:   <derived>                      [Confidence: high/medium/low]
Security Review: <derived>                      [Confidence: high/medium/low]
UI Review:       <derived or answered>          [Confidence: high/medium/low]
Depends on:      <derived or answered, or "(none)">
Spec:            <path and state>
Reference:       <paths and state>
======== END SYNTHESIS ========

These are pre-fills — confirm or edit each in the next step.
```

The attachment rows use the `suggested`, `unresolved`, `none detected`, or
`preserved` states from `sections/document-attachments.md`. They participate in
the same synthesis confirmation and the subsequent full-entry preview.

## Plan selection during `--add`

Resolve the Plan row in the existing combined synthesis confirmation; do not
add a separate yes/no prompt or write anything before the user accepts the
final preview with `y`.

- With zero qualified Plans, display `Plan: none detected`. Omit `Plan:` from
  the assembled entry and preview. The user may create this non-actionable
  TODO without choosing a Plan.
- With one candidate, display the suggested normalized path and its relevance
  reason. `confirm` accepts that one candidate; the user may instead edit it
  to supply a different path or `none`.
- With multiple candidates, display numbered paths and relevance reasons, and
  mark `Plan` unresolved. A plain `confirm` rejects only the unresolved Plan
  row and asks the user to select a number, provide `Plan: <path>`, or enter
  `none`; it preserves the other confirmed synthesis fields.
- For a manual `Plan: <path>` response, run the shared path normalization and
  validation in `sections/document-attachments.md` without rerunning
  attachment discovery or general research. Keep a valid normalized path for
  the preview; report the shared validation error and request only a corrected
  Plan value when invalid.
- A `none` response resolves the Plan row by omitting it. Only a resolved
  selected or manual Plan is emitted as `**Plan:** <normalized path>` in the
  final entry preview.

If the shared research budget is exhausted, disclose the skipped discovery
source in the synthesis. Continue with the qualified records already found and
apply the same zero, one, or multiple-candidate rule; do not spend more budget
to resolve Plan selection.

Confidence rule: fields answered directly by the user (via gap questions) are
always `high`. Derived fields are `high` if backed by an exact match (design
doc found, related TODO with same keywords, explicit blocking keyword in
summary), `medium` if inferred from a pattern (branch naming convention,
recent commit phase reference), and `low` if defaulted with no supporting
signal (e.g. Priority defaulted to `P2`, Security Review or UI Review
defaulted to `not-required` with no keyword match). Never mark a defaulted
field `high`.
