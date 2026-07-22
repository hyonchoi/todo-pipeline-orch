# Pipeline State Machine

Each row is a single transition. Columns: trigger / pre-state / post-state /
file writes / file deletes.

| Trigger | Pre-state | Post-state | Writes | Deletes |
|---|---|---|---|---|
| `pipeline-tick` starts | — | tick lock held | `.hermes/tick.lock/holder.json` | — |
| `run_selection` returns picked=None | tick lock held | tick lock released | `.hermes/decisions/<tick>.json` | `.hermes/tick.lock/` |
| `run_selection` returns picked=TODO-N (shadow) | tick lock held | tick lock released | `.hermes/decisions/<tick>.json` | `.hermes/tick.lock/` |
| `run_selection` returns picked=TODO-N (live) | tick lock held | phase running | `.hermes/decisions/<tick>.json`, `.hermes/phase_started/TODO-N.json` | `.hermes/tick.lock/` |
| hermes chat success (non-terminal phase) | phase running | phase running (next) | (nothing externally visible) | — |
| phase_5_review success (tests pass) | phase running | phase running (next) | `.hermes/outcomes/<tick>.json` (review_clean), review artifacts in `docs/pipeline/` | `.hermes/phase_started/TODO-N.json` |
| phase_5_review failure (tests fail) | phase running | phase running (next) | `.hermes/outcomes/<tick>.json` (review_reverted_test_failure), worktree restored to pre-review HEAD | `.hermes/phase_started/TODO-N.json` |
| phase_5_review timeout/error | phase running | failed | `.hermes/outcomes/<tick>.json` (review_timeout), worktree restored to pre-review HEAD, review artifacts | `.hermes/phase_started/TODO-N.json` |
| hermes chat success (terminal phase) | phase running | ready_for_review | `.hermes/ready_for_review/todo-N.json` (carries tick_id) | `.hermes/phase_started/TODO-N.json` |
| hermes chat failure | phase running | failed | `.hermes/ready_for_review/todo-N.json` with merge_status=failed, `.hermes/outcomes/<tick>.json` (failed_at_phase_*) | `.hermes/phase_started/TODO-N.json` |
| Phase 9 typed-confirm match | ready_for_review (pending) | merged | RFR updated to merged, `.hermes/outcomes/<tick>.json` (merged) | — |
| Phase 9 typed-confirm mismatch | ready_for_review (pending) | unchanged | — | — |
| Stale marker sweep (read time) | phase running (orphaned) | absent | — | `.hermes/phase_started/TODO-N.json` |
| Prompt SHA mismatch | tick lock held | tick lock released | `.hermes/decisions/<tick>.json` (rationale=prompt_sha_mismatch), Slack alert | `.hermes/tick.lock/` |

**Immutability invariant:** `.hermes/decisions/<tick>.json` is written exactly
once. Outcomes attach via the sidecar; never edit the decision file.

**No-progress definition:** a decision with `picked=None` AND
`rationale` NOT starting with `prompt_sha_mismatch:` AND NOT starting with
`tick_lock_held:`. These two reasons are config/race faults, not stalls.
