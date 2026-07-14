# Auto-Research Phase for --add (Step 4.5)

## Purpose

After the user provides a title and summary, silently research the codebase to
derive all todo fields. Only ask targeted questions for gaps that research
couldn't resolve. Never ask what can be determined.

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

To prevent unbounded file reads on large codebases, the auto-research phase must enforce
hard limits during signal collection:

- **Max 20 files read** during the research phase (includes TODOS.md, TODOS-archive.md, design docs, and implied source files)
- **Max 10 grep/search invocations** to match keywords across files
- If the cap is hit before all signals are collected, **stop researching immediately** and treat any field still undetermined as a gap
- Fall through to the gap-detection question flow rather than continuing to read additional files
- Document which signals were skipped due to cap exhaustion (e.g., "source file inspection skipped — budget exhausted")

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
Priority:        <derived or answered>          [Confidence: high/medium/low]
Effort:          <derived or answered>          [Confidence: high/medium/low]
Phase:           <derived>                      [Confidence: high/medium/low]
Branch:          <derived>                      [Confidence: high/medium/low]
Test Coverage:   <derived>                      [Confidence: high/medium/low]
Security Review: <derived>                      [Confidence: high/medium/low]
Depends on:      <derived or answered, or "(none)">
======== END SYNTHESIS ========

These are pre-fills — confirm or edit each in the next step.
```

Confidence rule: fields answered directly by the user (via gap questions) are
always `high`. Derived fields are `high` if backed by an exact match (design
doc found, related TODO with same keywords, explicit blocking keyword in
summary), `medium` if inferred from a pattern (branch naming convention,
recent commit phase reference), and `low` if defaulted with no supporting
signal (e.g. Priority defaulted to `P2`, Security Review defaulted to
`not-required` with no keyword match). Never mark a defaulted field `high`.
