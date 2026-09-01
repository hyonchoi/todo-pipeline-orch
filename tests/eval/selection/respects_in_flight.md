---
name: respects_in_flight
# TODO-2 is deliberately rendered in the body but is in flight, so it is not a candidate.
candidate_ids: [TODO-1, TODO-3]
in_flight: ["TODO-2"]
recent_decisions: []
expected_picked_in: ["TODO-1", "TODO-3"]
expected_picked_not: ["TODO-2"]
---
- TODO-1 [priority:high effort:M] implement caching
- TODO-2 [priority:high effort:S] fix typo
- TODO-3 [priority:medium effort:M] add logging
