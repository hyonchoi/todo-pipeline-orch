# How to Use the Native SDD Profile

The `native-sdd` profile starts at implementation. It keeps normal TODO
selection, but a selected entry must contain exactly one valid `Plan:` path
before the pipeline may create tasks.

## Prepare the TODO

Store an implementation-ready Plan inside the project and attach it to the
TODO with a project-relative path:

```markdown
- [ ] **TODO-42** Example change
  - **Plan:** docs/plans/TODO-42.md
```

The resolved path must remain inside the project and identify a readable
regular file. Missing, duplicate, absolute, escaping, symlink-escaping, or
unreadable Plan targets fail before tick state or kanban tasks are created.

## Initialize and verify

```bash
tpo init <project> --profile native-sdd
tpo doctor <project>
```

The only skill prerequisite is Hermes `ai-coding-agents`. The selected worker
client must still be installed and callable as `claude -p` or `codex exec`, but
no gstack, superpowers, or client-side workflow skill is used.

## Phase sequence

1. `phase_4_development` creates the branch and uses a fresh native implementer
   subagent for each ordered Plan task. Each task completes a red-green-refactor
   cycle and becomes exactly one atomic commit.
2. `phase_5_review` independently reviews `main...HEAD`, applies valid findings,
   and creates at most one separate review-fix commit.
3. `phase_8_finish_branch` runs repository checks, adds only required release or
   PR metadata, pushes, and creates or updates the pull request without merging.
4. `phase_9_human_review` is an unassigned terminal gate with a sticky
   `needs_input` block. A human reviews and merges the pull request.

The recorded `.hermes/pipeline_branch.txt` keeps later ticks in merge-aware PR
handoff until GitHub reports the pull request merged.
