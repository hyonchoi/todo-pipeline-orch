---
name: outcome_aware_avoids_failed
in_flight: []
recent_decisions:
  - tick_id: "old1"
    picked: "TODO-2"
    outcome: "failed_at_phase_autoplan"
expected_picked_in: ["TODO-1"]
expected_picked_not: ["TODO-2"]
---
- TODO-1 [priority:high effort:M] implement caching layer
- TODO-2 [priority:high effort:M] fix the build
