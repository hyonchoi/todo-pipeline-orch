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
| hermes chat success (terminal phase) | phase running | ready_for_review | `.hermes/ready_for_review/N.json` (carries tick_id) | `.hermes/phase_started/TODO-N.json` |
| hermes chat failure | phase running | failed | `.hermes/ready_for_review/N.json` with merge_status=failed, `.hermes/outcomes/<tick>.json` (failed_at_phase_*) | `.hermes/phase_started/TODO-N.json` |
| Phase 9 typed-confirm match | ready_for_review (pending) | merged | RFR updated to merged, `.hermes/outcomes/<tick>.json` (merged) | — |
| Phase 9 typed-confirm mismatch | ready_for_review (pending) | unchanged | — | — |
| `pipeline-watch kill` | phase running | killed | `.hermes/outcomes/<tick>.json` (killed_by_operator) | `.hermes/phase_started/TODO-N.json`, optionally `.hermes/tick.lock/` |
| Stale marker sweep (read time) | phase running (orphaned) | absent | — | `.hermes/phase_started/TODO-N.json` |
| Prompt SHA mismatch | tick lock held | tick lock released | `.hermes/decisions/<tick>.json` (rationale=prompt_sha_mismatch), Slack alert | `.hermes/tick.lock/` |

**Immutability invariant:** `.hermes/decisions/<tick>.json` is written exactly
once. Outcomes attach via the sidecar; never edit the decision file.

**No-progress definition:** a decision with `picked=None` AND
`rationale` NOT starting with `prompt_sha_mismatch:` AND NOT starting with
`tick_lock_held:`. These two reasons are config/race faults, not stalls.
