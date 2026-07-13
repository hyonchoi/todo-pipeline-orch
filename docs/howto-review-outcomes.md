# How to Handle Phase 5 Review Outcomes

Phase 5 (code review) runs the gstack `/review` skill autonomously via `hermes chat -q`. Unlike other phases, the LLM prompt does not own the lifecycle — the code in `review_phase.py` owns the pre-review snapshot, post-review test run, and commit-or-restore decision.

## Review Flow

```
Phase 5 starts
    |
    +-- capture_pre_review_state()
    |   saves HEAD SHA and pre-review diff
    |
    +-- diff is empty?
    |       |
    |       +-- Yes → write artifacts, commit, skip hermes → review_skipped_no_diff
    |       |
    |       +-- No → continue
    |
    +-- hermes chat -q (gstack /review)
    |   applies fixes to working tree
    |
    +-- finalize_review()
        |
        +-- hermes error/timeout → restore worktree → review_timeout
        |
        +-- hermes success → run_pytest()
            |
            +-- pass → commit fixes → review_clean
            |
            +-- fail → restore worktree → review_reverted_test_failure
```

## Outcomes

| Outcome | Cause | Worktree state | Next step |
|---------|-------|---------------|-----------|
| `review_clean` | Hermes succeeded, pytest passed | Fixes committed | Pipeline proceeds to next phase |
| `review_reverted_test_failure` | Hermes succeeded, pytest failed | Restored to pre-review HEAD | Pipeline proceeds; the review found nothing actionable or broke tests |
| `review_timeout` | Hermes subprocess timed out (default: 2400s) | Restored to pre-review HEAD | Investigate why the review hung; consider increasing timeout in phases.yaml |
| `review_skipped_no_diff` | No code changes vs main | Artifacts committed | Normal for docs-only TODOs; pipeline proceeds |

## Artifacts

Every review writes to `docs/pipeline/`:

| File | Content | Always present? |
|------|---------|----------------|
| `{todo_id}-review-findings.md` | Review output summary | Yes |
| `{todo_id}-review-outcome.json` | JSON: `{"todo_id": "...", "outcome": "..."}` | Yes |
| `{todo_id}-pre-review.diff` | Diff of HEAD vs merge-base before review | Yes |
| `{todo_id}-post-review.diff` | Diff of HEAD vs merge-base after review fixes | Only on `review_clean` |

## Inspecting a Review Outcome

```bash
# Check the outcome
cat docs/pipeline/TODO-5-review-outcome.json

# Read the findings
cat docs/pipeline/TODO-5-review-findings.md

# Compare what the review changed (if review_clean)
git diff docs/pipeline/TODO-5-post-review.diff
```

## Handling `review_reverted_test_failure`

This outcome means the review applied fixes but the test suite failed. The worktree was deterministically restored to the pre-review HEAD, so the branch is in the same state as before the review.

The pipeline proceeds to the next phase (CSO security review). The review findings are still in `docs/pipeline/` for a human to inspect after merge.

If you want to address the review findings before shipping:
1. Manually apply the fixes from `docs/pipeline/TODO-5-review-findings.md`
2. Run `uv run pytest` to verify
3. Commit the changes

## Handling `review_timeout`

The review subprocess exceeded the timeout (default: 2400s, configurable in phases.yaml). The worktree was restored.

Options:
- Increase the timeout in `hermes_pipeline/data/phases.yaml` for `phase_5_review`
- Check if the review was stuck on a large diff. The pre-review diff at `docs/pipeline/TODO-5-pre-review.diff` shows the scope.

## Safety Properties

- **No partial commits.** On `review_reverted_test_failure` and `review_timeout`, the worktree is restored via `git reset --hard <head_sha>` + `git clean -fd`. Only the review artifacts (findings, outcome, diffs) are committed.
- **Secret redaction.** Review stdout embedded in findings is redacted for OpenAI keys (`sk-proj-*`), Anthropic keys (`sk-*`), and Bearer tokens.
- **Machine-verified.** Before returning success, the review lifecycle verifies: worktree is clean (no uncommitted changes) AND required artifacts exist. A missing artifact raises `RuntimeError`, which the runner records as a phase failure.

## See Also

- [Architecture overview](ARCHITECTURE.md) — Phase execution flow
- [Pipeline state machine](hermes-state-machine.md) — Outcome types and transitions
- [CLI reference](reference-cli.md) — `tick` and `kill` for phase management
- [Circuit breaker explanation](explanation-circuit-breaker.md) — Failed phases and no-progress tracking
