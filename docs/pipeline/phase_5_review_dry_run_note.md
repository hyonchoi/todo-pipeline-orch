# Phase 5 Review — Manual Dry-Run Note (Design T2)

> **Per the implementation plan's Critical Open Question (T2):** One manual validation MUST happen on a feature branch before this reaches production ticks. It cannot be unit-tested because it depends on live `/review` behavior inside `hermes chat -q`.

## Required Dry-Run

Run `phase_5_review` manually against a recent feature branch and confirm the gstack `/review` skill proceeds **autonomously** (applies fixes without hanging on a confirmation prompt) inside the hermes subprocess.

### Procedure

1. Pick a recent feature branch with changes (e.g., a completed TODO branch).
2. Run the `phase_5_review` phase manually (or trigger a pipeline tick that picks it).
3. Observe the hermes subprocess output: verify the `/review` skill applies its fixes without pausing for user input.
4. If it hangs, the 2400s timeout will trigger the timeout-restore path — verify the worktree is restored clean and the phase fails cleanly.

### Outcome Checklist

- [ ] `/review` skill runs autonomously inside `hermes chat -q` (no confirmation prompts)
- [ ] Fixes are applied to working tree
- [ ] Tests pass → fixes committed + artifacts written → outcome `review_clean`
- [ ] OR: Tests fail → worktree restored to pre-review HEAD → artifacts written → outcome `review_reverted_test_failure` (phase completes)
- [ ] OR: Timeout/error → worktree restored → artifacts written → phase fails cleanly (RuntimeError)

### Documentation

Document the dry-run result in `docs/pipeline/` or the TODO's review notes.

**Do NOT enable the phase for unattended runs until this passes.**

---

*This note is part of the Task 7 deliverable for the 2026-07-07-phase-5-review plan.*
