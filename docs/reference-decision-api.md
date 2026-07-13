# Decision Module Reference

The `hermes_pipeline.decision` subpackage provides the Hermes-agent selection interface: LLM-driven TODO pick via `hermes chat -q`, SHA-pinned prompts, immutable decision records, and outcome sidecars.

This module defines cross-repo contracts consumed by the Hermes config repo. The dataclasses in `schema.py` are the source of truth.

## Public API

```python
from hermes_pipeline.decision import run_selection, HermesSelectionDecision, SelectionContext, Outcome
```

### `run_selection()`

```python
def run_selection(
    *,
    tick_id: str,
    ctx: SelectionContext,
    cfg: FullConfig,
    timeout: int | None = None,
) -> HermesSelectionDecision: ...
```

Build prompt → call agent → persist immutable decision → return.

**Parameters:**
- `tick_id` — Unique identifier for this tick (ulid-based)
- `ctx` — Selection context (TODOS.md content, in-flight list, recent decisions, kanban snapshot)
- `cfg` — Full config with selection model, prompt path, max tokens, expected prompt SHA
- `timeout` — Hard ceiling in seconds for the agent call. When `None`, auto-derived from `max_tokens`. Pass an explicit value when bounded by a per-project tick budget.

**Error handling:**
- `PromptShaMismatch` → returns `picked=None`, fires Slack alert. Rationale prefixed with `prompt_sha_mismatch:`
- Config error (missing env var) → returns `picked=None`, rationale prefixed with `config_error:`
- API error (401/429/5xx/timeout) → returns `picked=None`, rationale prefixed with `api_error:`
- All error paths persist a decision record so the next tick's `recent_decisions` carries the cause.

**Trust boundary:** Validates `picked` against server-parsed TODO IDs in `ctx.todos_md`, not the LLM-supplied `candidates_considered`. Rejects picks that are: wrong shape, not in TODOS.md, or already in-flight.

### `SelectionContext`

```python
@dataclass(frozen=True)
class SelectionContext:
    todos_md: str              # Full TODOS.md text content
    in_flight: list[str]       # TODOs currently in pipeline (e.g. ["TODO-3", "TODO-5"])
    recent_decisions: list[dict]  # Last N decision records (for continuity)
    kanban_snapshot: dict      # Current kanban task statuses
    project_slug: str          # Project name
```

Built by `decision/context.py` via `build_context()`.

### `HermesSelectionDecision`

```python
@dataclass(frozen=True)
class HermesSelectionDecision:
    tick_id: str
    timestamp: str                       # ISO 8601 UTC
    model: str                           # Model id used for selection
    prompt_sha: str                      # SHA of the prompt used
    candidates_considered: list[str]     # TODOs the agent evaluated
    picked: str | None                   # Selected TODO (e.g. "TODO-5") or None
    rationale: str                       # Why this TODO was picked (or not)
    blocked_reasons: dict[str, str]     # Per-TODO blockers
    in_flight: list[str]                # Snapshot of in-flight TODOs at selection time
```

Persisted at `.hermes/decisions/<tick_id>.json`. Written exactly once; never edited.

### `Outcome`

```python
Outcome = Literal[
    "in_flight",
    "merged",
    "failed_at_phase_N",
    "discarded",
    "killed_by_operator",
    "failed_to_spawn",
]
```

Outcome sidecars attach to the decision record. The decision file is never modified.

## Outcome Sidecars

Outcome sidecars are written to `.hermes/outcomes/<tick_id>-phases.json` (JSONL format). The decision store is write-once: `FileExistsError` on a terminal outcome means a prior outcome already committed the tick's fate.

```python
from hermes_pipeline.decision.store import append_outcome

append_outcome(
    state_dir, tick_id,
    outcome="failed_at_phase_phase_4_development",
    detail={"todo_id": "TODO-5", "error": "phase subprocess crashed"},
)
```

## Plan-Gate Schemas

The `schema.py` module also defines the plan-gate contract:

### `DecisionSheet`

```python
@dataclass(frozen=True)
class DecisionSheet:
    schema_version: str          # Must be "1.0"
    todo_id: int                 # Positive integer TODO number
    tick_id: str                 # Tick that generated the plan
    questions: list[DecisionQuestion]
```

Persisted at `.hermes/decisions/<tick_id>-plan.json`. Rewritten with answers when `approve-plan` approves. Rejection sidecars at `.hermes/decisions/<tick_id>-rejected.json` are written on rejection.

### `DecisionQuestion`

```python
@dataclass(frozen=True)
class DecisionQuestion:
    question_id: str                                    # e.g. "q1"
    classification: Literal["taste", "premise", "user-challenge", "mechanical"]
    prompt: str                                         # The decision question
    options: list[_Option]                              # ≥ 2 options
    recommendation: str                                 # Recommended option label (e.g. "A")
    rationale: str                                      # Why the recommendation
    answer: str | None                                  # Set by approve-plan
```

Validation: classification must be valid, options ≥ 2, recommendation must match an option label, answer must be None or match an option label.

### `validate_decision_sheet()`

```python
def validate_decision_sheet(data: dict) -> DecisionSheet: ...
```

Constructs a `DecisionSheet` from a raw dict. Raises `PlanGateError` on any validation failure.

## Selection Agent

The agent module (`decision/agent.py`) wraps `hermes chat -q`:

- **`call_agent()`** — Runs the Hermes chat subprocess with the selection prompt. Returns a result with `parsed` (dict), `prompt_sha` (str).
- **`compute_prompt_sha()`** — SHA-256 of the prompt file. Used for drift detection.
- **`PromptShaMismatch`** — Raised when the computed SHA differs from `expected_prompt_sha` in config.

## See Also

- [Selection seat contract](../hermes_pipeline/decision/README.md) — Full contract for Hermes config repo integration
- [How to recover from a prompt SHA mismatch](howto-prompt-sha-mismatch.md) — When selection aborts with `prompt_sha_mismatch:`
- [How to run the eval suite](howto-eval-suite.md) — Before changing the prompt, model, or agent code
- [Pipeline state machine](hermes-state-machine.md) — Decision file layout and transitions
- [CLI reference](reference-cli.md) — `tick` subcommand (selection entry point)
