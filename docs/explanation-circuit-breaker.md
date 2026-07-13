# Circuit Breaker

The circuit breaker stops runaway pipeline ticks from consuming LLM budget on a stuck project.

## The Problem

A pipeline tick is an LLM invocation. If selection keeps returning `picked=None` because all TODOs are blocked, in-flight, or malformed, every tick spends tokens on a no-op. Without a circuit breaker, a 5-minute cron fires ~288 times a day on a stalled project, each call burning tokens and returning nothing.

## How It Works

The circuit breaker tracks consecutive no-progress ticks in `.hermes/circuit.json`:

```json
{"consecutive_no_progress": 3, "last_alert_at": "2026-07-10T14:30:00Z"}
```

**Observation triggers:**

1. **Selection picks a TODO** → counter resets to 0 (progress)
2. **Selection picks None** → counter increments (no progress)
3. **Prior tick phases complete** → counter resets (progress, observed from outcome sidecars)
4. **Prior tick phases fail** → counter increments (no progress)
5. **Prior tick picked None** (all TODOs done/blocked) → counter unchanged (idle, not a failure)

**Alert:** When the counter reaches the threshold (default: 3), fires a Slack alert via `hermes chan message`. Alert dedup: one alert per `alert_dedup_hours` (default: 24).

## Configuration

Tunable via `.hermes/config.toml`:

```toml
[circuit_breaker]
no_progress_threshold = 3    # consecutive no-progress ticks before Slack alert
alert_dedup_hours = 24       # minimum interval between duplicate alerts
max_phase_timeout_min = 120  # max time for a single phase to complete
max_tick_duration_min = 10   # max time for the entire project tick
```

## Design Decisions

**Separate "idle" from "stalled".** When selection returns `picked=None` because all TODOs are genuinely complete, the circuit breaker does not count that as no-progress. This is a healthy idle state, not a failure. The distinction comes from the outcome sidecar: `picked_none` resets the dedup timer but not the counter, while `failed_at_phase_*` increments the counter.

**Threshold, not a hard stop.** The circuit breaker alerts but does not stop the cron. The Hermes gateway service manages tick scheduling and cron backoff. The circuit breaker is an observable signal, not a control plane — it tells you the pipeline is stuck; it does not decide what to do about it.

**Outcome-driven observation.** The circuit breaker observes from outcome sidecars, not kanban status. Outcomes are the source of truth because a kanban task can remain "running" while a phase subprocess has already crashed. The outcome sidecar is written atomically before the phase marker is cleared.

## Trade-offs

- **False negatives on rapid fix:** if an operator resolves a stuck TODO between ticks, the counter still increments once before resetting. Not a risk — one extra alert is harmless.
- **No circuit breaker for selection timeouts.** A `PromptShaMismatch` or API error returns `picked=None` but the rationale prefix distinguishes config faults from genuine stalls. The config-fault path fires a Slack alert directly and skips the circuit breaker.

## See Also

- [How to debug ticks and recover counters](howto-debugging-and-recovery.md) — Diagnosing no-progress ticks
- [Pipeline state machine](hermes-state-machine.md) — Outcome sidecars and state transitions
- [CLI reference](reference-cli.md) — `tick` subcommand
