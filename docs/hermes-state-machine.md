# Pipeline State Machine

Each row is a single transition. Columns: trigger / pre-state / post-state /
file writes / file deletes.

| Trigger | Pre-state | Post-state | Writes | Deletes |
|---|---|---|---|---|
| `pipeline-tick` starts | — | tick lock held | `.hermes/tick.lock/holder.json` | — |
| prior tick has running/ready kanban tasks | tick lock held | tick lock released | — | `.hermes/tick.lock/` |
| prior tick complete, recorded PR branch not merged | tick lock held | tick lock released | observed outcomes in `.hermes/outcomes/<tick>-phases.json` | `.hermes/tick.lock/` |
| prior tick complete, recorded PR branch merged | tick lock held | selection allowed | observed outcomes in `.hermes/outcomes/<tick>-phases.json` | — |
| `run_selection` returns picked=None | tick lock held | tick lock released | `.hermes/decisions/<tick>.json`, `.hermes/outcomes/<tick>-phases.json` (`picked_none`) | `.hermes/tick.lock/` |
| `run_selection` returns picked=TODO-N | tick lock held | kanban phases registered | `.hermes/decisions/<tick>.json`, `.hermes/current_tick_id.txt`, `.hermes/outcomes/<tick>-phases.json` (`tick_started`) | `.hermes/tick.lock/` |
| kanban phase reaches `done` | kanban task active | next task unblocked by kanban | `.hermes/outcomes/<tick>-phases.json` (`phase_complete`) | — |
| kanban phase reaches `failed` or `archived` | kanban task active | tick failed | `.hermes/outcomes/<tick>-phases.json` (`failed_at_phase_*`) | — |
| default Phase 8 completes PR handoff | final kanban task active | waiting for PR merge | `.hermes/pipeline_branch.txt`, `.hermes/outcomes/<tick>-phases.json` (`all_phases_complete`) | — |
| legacy `phase_9_ship` gate is blocked and pre-gate work is done | prior tick in-flight | waiting for `tpo approve` | `.hermes/outcomes/<tick>-ship.json` | — |
| legacy `tpo approve` succeeds | ship sidecar pending | merged | version files on PR branch, kanban gate task completed | `.hermes/outcomes/<tick>-ship.json` |
| Prompt SHA mismatch | tick lock held | tick lock released | `.hermes/decisions/<tick>.json` (rationale=prompt_sha_mismatch), Slack alert | `.hermes/tick.lock/` |

**Immutability invariant:** `.hermes/decisions/<tick>.json` is written exactly
once. Outcomes attach via the sidecar; never edit the decision file.

**No-progress definition:** a decision with `picked=None` writes a
`picked_none` outcome and leaves the pipeline idle rather than stalled. A
`tick_started` outcome without later terminal phase outcomes is treated as a
stall so the circuit breaker can alert.
