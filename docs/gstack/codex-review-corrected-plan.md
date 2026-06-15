# Corrected TODO-6 Plan: Phase A — Hermes Adapter (Codex-Validated)

Generated from Codex outside-voice review, 2026-06-15
Branch: feat/TODO-6-hermes-chat-replacement

## Why We Changed Course

The original Approach B plan (Full Kanban-Native) was rejected by Codex on architectural grounds:
- Kanban is state storage, not a workflow engine — deleting the orchestrator loses real semantics
- Phase A alone (replace Anthropic calls with `hermes chat -q`) satisfies TODO-6
- Deleting `runner.py`, `state.py`, `phases.py` before proving Kanban can enforce sequencing is reckless

**New approach**: Replace the two Anthropic call sites with Hermes, keep the state machine, keep the tests. Deliver TODO-6 in 2-3 days, not 2-3 weeks.

## TODO-6 Restated

> Remove any direct `claude`/Anthropic SDK invocations from the orchestrator and route all LLM queries through the `hermes` command.

Two call sites to replace. That's it.

## Call Site 1: `decision/agent.py:_anthropic_call()`

**Current**: Direct Anthropic SDK call (lines 67-75)
```python
def _anthropic_call(*, model: str, max_tokens: int, prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
```

**Replace with**: `hermes chat -q` subprocess call
```python
def _hermes_call(*, model: str, max_tokens: int, prompt: str) -> str:
    import subprocess
    # Use hermes proxy or chat -q to route through Hermes
    result = subprocess.run(
        ["hermes", "chat", "-q", prompt, "-Q", "-m", model, "--source", "tool"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"hermes chat failed: rc={result.returncode} "
            f"stderr={result.stderr[:200]}"
        )
    return result.stdout.strip()
```

**Why `hermes chat -q` over `hermes proxy start`**: The proxy approach requires a long-running server process — adds operational complexity and a dependency on the gateway being up. `hermes chat -q` is a one-shot subprocess, same pattern as `_run_claude_subprocess`.

**Testing**: `test_decision_agent.py` already monkey-patches `_anthropic_call`. Add a test that calls `_hermes_call` with a mocked subprocess. Same `AgentResult` shape, same `_parse()` function.

## Call Site 2: `phases.py:_run_claude_subprocess()`

**Current**: Runs `claude` CLI as subprocess (lines 100-136)
```python
def _run_claude_subprocess(
    *, claude_cmd: str, prompt: str, tools: str,
    turns: int, timeout: int, cwd, on_pid=None,
) -> dict:
    proc = _sp.Popen(
        [claude_cmd, "-p", prompt, "--tools", tools, "--turns", str(turns)],
        ...
    )
```

**Replace with**: `hermes agent` or `hermes chat` subprocess call

This is trickier because the current code uses Claude CLI flags (`--tools`, `--turns`). Need to determine Hermes equivalents:
- If Hermes supports `hermes agent -p "<prompt>" --tools --turns`, drop-in swap
- If not, use `hermes chat -q` with a system prompt that encodes tools/turns constraints
- Alternative: use `hermes proxy start` and point `OPENAI_BASE_URL` at it — but that's a long-running process

**Decision**: Use `hermes agent` if available, otherwise `hermes chat -q` with system prompt encoding. Profile against a real Hermes instance to determine which works.

**Testing**: `test_phases_invoke.py` and `test_phases_marker.py` already mock `_run_claude_subprocess`. Same approach.

## Phase A Steps

### Step 1: Create `hermes_adapter.py`

```python
"""Hermes CLI adapter — replaces direct Anthropic calls with `hermes chat -q`."""

import subprocess

class HermesCallError(Exception):
    """Raised when hermes chat -q returns non-zero."""
    pass

def hermes_call(
    *,
    prompt: str,
    model: str = "auto",  # Let Hermes resolve from config
    timeout: int = 120,
) -> str:
    """Call `hermes chat -q` and return stdout, or raise HermesCallError."""
    result = subprocess.run(
        [
            "hermes", "chat", "-q", prompt,
            "-Q", "-m", model, "--source", "tool",
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise HermesCallError(
            f"hermes chat failed: rc={result.returncode} "
            f"stderr={result.stderr[:300]}"
        )
    return result.stdout.strip()

def hermes_agent_call(
    *,
    prompt: str,
    model: str = "auto",
    tools: bool = True,
    turns: int = 25,
    timeout: int = 1800,
    cwd: str | None = None,
    on_pid: callable | None = None,
) -> dict:
    """Call `hermes agent` (or `hermes chat -q` with agent semantics) and return result dict.

    Returns same shape as _run_claude_subprocess:
    {returncode, stdout, stderr, timed_out}
    """
    ...
```

### Step 2: Replace `_anthropic_call` in `decision/agent.py`

- Replace `_anthropic_call()` with `hermes_call()` from `hermes_adapter`
- Keep same function signature — `call_agent()` is the public API
- Add `HermesCallError` handling in `call_agent()` — maps to `RuntimeError` for compatibility

### Step 3: Replace `_run_claude_subprocess` in `phases.py`

- Replace with `hermes_agent_call()` from `hermes_adapter`
- Keep same return shape: `{returncode, stdout, stderr, timed_out}`
- Tests mock this function — same monkey-patch pattern

### Step 4: Remove Anthropic Dependency

- Remove `anthropic>=0.40` from `pyproject.toml`
- Run `uv sync` to confirm no import errors

### Step 5: Run Tests

- `uv run pytest` — all existing tests should pass
- `test_decision_agent.py` — monkey-patch `_hermes_call` instead of `_anthropic_call`
- `test_phases_invoke.py` — monkey-patch `_run_hermes_subprocess` instead of `_run_claude_subprocess`

### Step 6: Add Integration Test

- `tests/test_hermes_adapter.py` — basic test that calls `hermes chat -q` with a mock
- Verify error handling: non-zero exit, timeout, empty output

## What We Keep

- `runner.py` — PipelineRunner, branch naming, phase loop
- `state.py` — State machine, checkpoints, ready_for_review
- `phases.py` — Phase definitions, marker helpers, prompt rendering
- `kanban.py` — Kanban integration (Protocol-based)
- `circuit.py` — Circuit breaker
- `tick.py` — Tick lock
- `merge.py` — Branch merge logic
- All existing tests

## What We Don't Do (Phase A)

- No Kanban task creation per phase
- No `kanban_factory.py`
- No deletion of existing modules
- No Slack notifications
- No dashboard
- No dry-run mode
- No output normalization
- No worktree changes

## Rollback

If `hermes chat -q` doesn't work:
1. Revert the two function replacements
2. Re-add `anthropic` to `pyproject.toml`
3. Done — zero state damage

## Phase B: Kanban-Native (Future)

After Phase A is proven in production:
1. Build `kanban_factory.py` to create 6 linked Kanban tasks per TODO
2. Prove Kanban can enforce sequencing (dependency edges)
3. Prove artifact passing works
4. Replace `PipelineRunner` with Kanban dispatch
5. Delete old modules only after parity tests pass

This is the Approach B that was originally planned, but now with:
- Phase A proven as a safe state
- Real Hermes Kanban experience to inform the design
- Parity tests from Phase A to validate against

## Success Criteria for Phase A

1. `uv run pytest` passes — all existing tests green
2. `anthropic` removed from `pyproject.toml` — no direct SDK calls
3. Manual run of `pipeline-watch` executes a phase through `hermes chat -q` without errors
4. Error handling works — `hermes chat -q` failure raises `HermesCallError`, surfaces in logs
