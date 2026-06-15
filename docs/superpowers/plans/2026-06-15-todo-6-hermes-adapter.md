# TODO-6: Replace Anthropic Calls with Hermes Chat

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all direct Anthropic SDK invocations from the orchestrator and route all LLM queries through `hermes chat -q`.

**Architecture:** Create `hermes_pipeline/hermes_adapter.py` with two functions — `hermes_call()` for simple one-shot queries (replaces `_anthropic_call` in the decision agent) and `hermes_agent_call()` for agent-style subprocess calls with PID tracking (replaces `_run_claude_subprocess` in phases). Both functions call `hermes chat -q` as a subprocess. Existing tests are updated to monkey-patch the new functions. Finally, `anthropic` is removed from `pyproject.toml`. This is **Phase A** of the migration outlined in [the approved design doc](../gstack/hyonchoi-feat-v0.3-hermes-centric-design-20260614-225657.md) and the [CEO plan](../gstack/ceo-plans/2026-06-15-hermes-native-pipeline.md).

**Tech Stack:** Python 3.12+, `hermes` CLI, `subprocess`, `pyyaml`

---

## File Structure

### New Files
- `hermes_pipeline/hermes_adapter.py` — Two functions: `hermes_call()` and `hermes_agent_call()`. Single responsibility: wrap `hermes chat -q` subprocess calls.

### Modified Files
- `hermes_pipeline/decision/agent.py` — Replace `_anthropic_call()` (lines 67-75) with `hermes_call()` from `hermes_adapter`
- `hermes_pipeline/phases.py` — Replace `_run_claude_subprocess()` (lines 100-136) with `hermes_agent_call()` from `hermes_adapter`
- `tests/test_decision_agent.py` — Update monkey-patch targets from `_anthropic_call` to `_hermes_call`
- `tests/test_phases_invoke.py` — **No changes needed** — tests monkey-patch `_run_claude_subprocess` and we keep that function name/shape
- `pyproject.toml` — Remove `anthropic>=0.40` from dependencies

### Unchanged (kept as-is)
- `runner.py` — PipelineRunner, branch naming, phase loop
- `state.py` — State machine, checkpoints, ready_for_review
- `kanban.py` — Kanban integration (Protocol-based)
- `circuit.py` — Circuit breaker
- `tick.py` — Tick lock
- `merge.py` — Branch merge logic
- `configs/phases.yaml` — Phase definitions
- All other tests

---

## Design Decisions (from source docs)

### Why `hermes chat -q` over `hermes proxy start`?

The design doc mentions both approaches. `hermes proxy start` creates an OpenAI-compatible local proxy — just redirect `base_url`. But that keeps the Python package in orchestration mode (same SDK calls, different endpoint). `hermes chat -q` is the correct choice because: model policy, auth, and fallback are managed by Hermes, not Python. The `anthropic` package is removed entirely.

### `--source tool` flag

Added to both `hermes_call()` and `hermes_agent_call()` to signal to Hermes that this call originates from pipeline tooling, not user interaction. Hermes can use this for logging, metrics, and rate-limiting purposes.

### Model resolution

`hermes_call()` takes an optional `model` parameter — when passed, `-m <model>` is added to the command. When `"auto"`, Hermes resolves from its config (via `hermes model`). This aligns with the TODO-6 context: "hermes model sets default model+provider so decision/agent.py no longer hardcodes one."

### Fallback

Model fallback is handled by Hermes via `hermes fallback` — no custom fallback logic needed (TODO-5 collapse).

### Tools encoding

`hermes chat -q` does not have `--tools` or `--turns` flags. The `hermes_agent_call()` function encodes these constraints in the prompt header:
```
AGENT_MODE: tools=enabled, available_tools=Read,Write,Bash, max_turns=15.
You have tool access. Complete the task within 15 turns.
```

This way Hermes (and its workers) can enforce these constraints internally.

---

## Task 1: Create `hermes_adapter.py`

**Files:**
- Create: `hermes_pipeline/hermes_adapter.py`
- Test: `tests/test_hermes_adapter.py`

- [ ] **Step 1: Write failing test — hermes_call success, failure, and args**

Create `tests/test_hermes_adapter.py`:

```python
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest
import subprocess

from hermes_pipeline.hermes_adapter import (
    hermes_call,
    HermesCallError,
    hermes_agent_call,
    HermesAgentResult,
)


def test_hermes_call_returns_stdout_on_success():
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "  plan output here  "
    fake_result.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        result = hermes_call(prompt="hello world", model="claude-sonnet-4-6")

    assert result == "plan output here"


def test_hermes_call_raises_on_nonzero_exit():
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "E100: gateway unreachable"

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        with pytest.raises(HermesCallError, match="gateway unreachable"):
            hermes_call(prompt="hello", model="claude-sonnet-4-6")


def test_hermes_call_includes_error_details():
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "detailed error message"

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        with pytest.raises(HermesCallError) as exc_info:
            hermes_call(prompt="hello", model="claude-sonnet-4-6")

    assert exc_info.value.returncode == 1
    assert exc_info.value.stderr == "detailed error message"


def test_hermes_call_passes_correct_args():
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "ok"
    fake_result.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result) as mock_run:
        hermes_call(prompt="test prompt", model="claude-sonnet-4-6", timeout=60)

    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "hermes", "chat", "-q",
        "test prompt",
        "-Q",
        "-m", "claude-sonnet-4-6",
        "--source", "tool",
    ]
    assert mock_run.call_args[1]["timeout"] == 60


def test_hermes_call_omits_model_flag_when_auto():
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "ok"
    fake_result.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result) as mock_run:
        hermes_call(prompt="test", model="auto")

    cmd = mock_run.call_args[0][0]
    assert "-m" not in cmd


def test_hermes_agent_call_returns_result_on_success():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.pid = 12345
    fake_proc.communicate.return_value = ("agent output", "")

    pid_seen = []

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        result = hermes_agent_call(
            prompt="do something",
            model="claude-sonnet-4-6",
            tools=True,
            turns=15,
            on_pid=pid_seen.append,
        )

    assert isinstance(result, HermesAgentResult)
    assert result.returncode == 0
    assert result.stdout == "agent output"
    assert result.timed_out is False
    assert pid_seen == [12345], "on_pid callback must fire"


def test_hermes_agent_call_encodes_tools_in_prompt():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.pid = 12345
    fake_proc.communicate.return_value = ("ok", "")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc) as mock_popen:
        hermes_agent_call(
            prompt="do something",
            tools=True,
            turns=15,
        )

    # The spawned command should include an augmented prompt with AGENT_MODE header
    cmd = mock_popen.call_args[0][0]
    # Find the prompt argument (comes after -q)
    q_idx = cmd.index("-q")
    prompt_arg = cmd[q_idx + 1]
    assert "AGENT_MODE" in prompt_arg
    assert "tools=enabled" in prompt_arg
    assert "max_turns=15" in prompt_arg
    assert "do something" in prompt_arg


def test_hermes_agent_call_handles_timeout():
    fake_proc = MagicMock()
    fake_proc.pid = 99999

    def communicate(timeout):
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)

    fake_proc.communicate = communicate
    pid_seen = []

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        result = hermes_agent_call(
            prompt="slow task",
            timeout=10,
            on_pid=pid_seen.append,
        )

    assert result.returncode == -1
    assert result.timed_out is True
    fake_proc.kill.assert_called_once()


def test_hermes_agent_call_respects_cwd():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate.return_value = ("ok", "")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc) as mock_popen:
        hermes_agent_call(
            prompt="test",
            cwd="/some/path",
        )

    assert mock_popen.call_args[1]["cwd"] == "/some/path"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_hermes_adapter.py -v`
Expected: FAIL — ModuleNotFoundError: No module named 'hermes_pipeline.hermes_adapter'

- [ ] **Step 3: Write minimal implementation**

Create `hermes_pipeline/hermes_adapter.py`:

```python
"""Hermes CLI adapter — replaces direct Anthropic calls with `hermes chat -q`.

Two functions:
- hermes_call(): simple one-shot query (replaces _anthropic_call in decision/agent.py)
- hermes_agent_call(): agent-style subprocess with PID tracking (replaces
  _run_claude_subprocess in phases.py)
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass


class HermesCallError(Exception):
    """Raised when `hermes chat -q` returns non-zero exit code."""

    def __init__(self, message: str, returncode: int, stderr: str):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class HermesAgentResult:
    """Result from an agent-style hermes call — matches the shape of
    _run_claude_subprocess return value for drop-in compatibility."""
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def hermes_call(
    *,
    prompt: str,
    model: str = "auto",
    timeout: int = 120,
) -> str:
    """Call `hermes chat -q` and return stripped stdout.

    Args:
        prompt: The prompt text to send.
        model: Model identifier. "auto" lets Hermes resolve from config.
        timeout: Seconds before killing the process.

    Returns:
        Stripped stdout from hermes chat.

    Raises:
        HermesCallError: If the process exits with non-zero.
    """
    cmd = [
        "hermes", "chat", "-q",
        prompt,
        "-Q",
        "--source", "tool",
    ]
    if model != "auto":
        cmd.extend(["-m", model])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise HermesCallError(
            message=(
                f"hermes chat failed: rc={result.returncode} "
                f"stderr={result.stderr[:300]}"
            ),
            returncode=result.returncode,
            stderr=result.stderr,
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
) -> HermesAgentResult:
    """Call `hermes chat -q` in agent mode and return structured result.

    Drop-in replacement for _run_claude_subprocess. The prompt is augmented
    with agent constraints (tools availability, turn limit) encoded in the
    system prompt portion since `hermes chat -q` does not have --tools/--turns
    flags — Hermes manages those internally.

    Args:
        prompt: The prompt text to send.
        model: Model identifier. "auto" lets Hermes resolve from config.
        tools: Whether tools should be available (encoded in prompt, Hermes enforces).
        turns: Maximum turns (encoded in prompt, Hermes enforces).
        timeout: Seconds before killing the process.
        cwd: Working directory for the subprocess.
        on_pid: Callback fired with subprocess PID immediately after spawn.

    Returns:
        HermesAgentResult with returncode, stdout, stderr, timed_out.
    """
    # Build augmented prompt with agent constraints
    tools_str = "enabled" if tools else "disabled"
    agent_header = (
        f"AGENT_MODE: tools={tools_str}, max_turns={turns}. "
        f"You have tool access. Complete the task within {turns} turns.\n\n"
    )
    augmented_prompt = agent_header + prompt

    try:
        cmd = [
            "hermes", "chat", "-q",
            augmented_prompt,
            "-Q",
            "--source", "tool",
        ]
        if model != "auto":
            cmd.extend(["-m", model])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )

        if on_pid is not None:
            try:
                on_pid(proc.pid)
            except Exception:
                pass

        stdout, stderr = proc.communicate(timeout=timeout)
        return HermesAgentResult(
            returncode=proc.returncode or 0,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=False,
        )

    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return HermesAgentResult(
            returncode=-1,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_hermes_adapter.py -v`
Expected: 9/9 PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/hermes_adapter.py tests/test_hermes_adapter.py
git commit -m "feat: add hermes_adapter to replace direct Anthropic calls

- hermes_call(): one-shot query via hermes chat -q
- hermes_agent_call(): agent-style call with PID tracking and
  AGENT_MODE prompt header for tools/turns constraints
- HermesCallError with returncode and stderr attributes
- Model 'auto' omits -m flag — Hermes resolves from config
- --source tool flag signals pipeline tooling origin"
```

---

## Task 2: Replace `_anthropic_call` in `decision/agent.py`

**Files:**
- Modify: `hermes_pipeline/decision/agent.py:67-75`
- Modify: `hermes_pipeline/decision/agent.py:114` (call site)
- Test: `tests/test_decision_agent.py`

- [ ] **Step 1: Replace `_anthropic_call` function with `_hermes_call`**

In `hermes_pipeline/decision/agent.py`, replace lines 67-75:

**Before:**
```python
def _anthropic_call(*, model: str, max_tokens: int, prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
```

**After:**
```python
def _hermes_call(*, model: str, max_tokens: int, prompt: str) -> str:
    from .. import hermes_adapter

    result = hermes_adapter.hermes_call(
        model=model,
        prompt=prompt,
        timeout=min(max(max_tokens // 100, 30), 300),
    )
    return result
```

Note: `from .. import hermes_adapter` — the parent of `decision` is `hermes_pipeline`. The timeout is derived from `max_tokens` as a rough estimate (30-300s range).

- [ ] **Step 2: Update the call site in `call_agent()`**

In `hermes_pipeline/decision/agent.py`, line 114:

**Before:**
```python
    raw = _anthropic_call(model=model, max_tokens=max_tokens, prompt=rendered)
```

**After:**
```python
    raw = _hermes_call(model=model, max_tokens=max_tokens, prompt=rendered)
```

- [ ] **Step 3: Update `tests/test_decision_agent.py` monkey-patch targets**

In `tests/test_decision_agent.py`, update all three references from `_anthropic_call` to `_hermes_call`:

**Line 46-48 — Before:**
```python
    monkeypatch.setattr(
        "hermes_pipeline.decision.agent._anthropic_call",
        lambda *a, **kw: called.append(True) or "",
    )
```

**After:**
```python
    monkeypatch.setattr(
        "hermes_pipeline.decision.agent._hermes_call",
        lambda *a, **kw: called.append(True) or "",
    )
```

Apply same change to lines 62-64 and 79-81 (3 occurrences total).

- [ ] **Step 4: Run decision agent tests**

Run: `uv run pytest tests/test_decision_agent.py -v`
Expected: 5/5 PASS

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/decision/agent.py tests/test_decision_agent.py
git commit -m "feat: replace _anthropic_call with hermes_call in decision agent

- _hermes_call() uses hermes_adapter.hermes_call() instead of
  Anthropic SDK — model policy shifts from Python to Hermes
- call_agent() signature unchanged — HermesCallError propagates
  through as-is (callers expect exceptions to bubble)
- Tests monkey-patch _hermes_call instead of _anthropic_call"
```

---

## Task 3: Replace `_run_claude_subprocess` in `phases.py`

**Files:**
- Modify: `hermes_pipeline/phases.py:100-136`
- Test: `tests/test_phases_invoke.py` (no changes needed — same function name/shape)

- [ ] **Step 1: Replace `_run_claude_subprocess` function**

In `hermes_pipeline/phases.py`, replace lines 100-136:

**Before:**
```python
def _run_claude_subprocess(
    *,
    claude_cmd: str,
    prompt: str,
    tools: str,
    turns: int,
    timeout: int,
    cwd,
    on_pid=None,
) -> dict:
    """Run the Claude CLI as a subprocess.

    Returns a dict with returncode, stdout, stderr keys. The optional
    `on_pid(pid)` callback fires immediately after spawn so the caller can
    record the child PID for kill routing. Tests monkey-patch this function
    to avoid hitting the real CLI.
    """
    proc = _sp.Popen(
        [claude_cmd, "-p", prompt, "--tools", tools, "--turns", str(turns)],
        stdout=_sp.PIPE,
        stderr=_sp.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    if on_pid is not None:
        try:
            on_pid(proc.pid)
        except Exception:
            pass
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr}
    except _sp.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return {"returncode": -1, "stdout": stdout, "stderr": stderr, "timed_out": True}
```

**After:**
```python
def _run_claude_subprocess(
    *,
    claude_cmd: str,
    prompt: str,
    tools: str,
    turns: int,
    timeout: int,
    cwd,
    on_pid=None,
) -> dict:
    """Run a phase via `hermes chat -q`.

    Returns a dict with returncode, stdout, stderr, timed_out keys — same
    shape as the old Claude subprocess call for drop-in compatibility.
    The `claude_cmd` parameter is ignored (Hermes resolves model via config).
    The `tools` parameter is a comma-separated list (e.g., "Read,Write,Bash")
    encoded in the AGENT_MODE prompt header. Tests monkey-patch this function
    to avoid hitting the real CLI.
    """
    from .hermes_adapter import hermes_agent_call

    result = hermes_agent_call(
        prompt=prompt,
        tools=len(tools) > 0,
        turns=turns,
        timeout=timeout,
        cwd=cwd,
        on_pid=on_pid,
    )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
    }
```

Key point: `tools=len(tools) > 0` — the `tools` parameter is a comma-separated string like "Read,Write,Bash". We convert to boolean for `hermes_agent_call`. Non-empty means tools are enabled.

- [ ] **Step 2: Verify existing tests still work**

The existing tests in `test_phases_invoke.py` monkey-patch `_run_claude_subprocess` with lambdas returning dicts — since we kept the same function name and return dict shape, these patches work without changes.

- [ ] **Step 3: Run phases invoke tests**

Run: `uv run pytest tests/test_phases_invoke.py -v`
Expected: 5/5 PASS

- [ ] **Step 4: Run phases marker tests**

Run: `uv run pytest tests/test_phases_marker.py -v`
Expected: 7/7 PASS (these patch `_invoke_claude`, which still exists and calls `_run_claude_subprocess`)

- [ ] **Step 5: Commit**

```bash
git add hermes_pipeline/phases.py
git commit -m "feat: replace _run_claude_subprocess with hermes_agent_call

- _run_claude_subprocess now delegates to hermes_adapter.hermes_agent_call()
- Same function name and return dict shape for drop-in compatibility
- claude_cmd parameter ignored (Hermes resolves model from config)
- tools string converted to boolean (non-empty = enabled)
- test_phases_invoke.py patches continue to work unchanged"
```

---

## Task 4: Remove Anthropic Dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Remove `anthropic>=0.40` from dependencies**

In `pyproject.toml`:

**Before:**
```toml
dependencies = [
    "pyyaml>=6.0",
    "anthropic>=0.40",
    "python-ulid>=2.2",
]
```

**After:**
```toml
dependencies = [
    "pyyaml>=6.0",
    "python-ulid>=2.2",
]
```

- [ ] **Step 2: Run `uv sync` to confirm no import errors**

Run: `uv sync`
Expected: Success — `anthropic` package removed, no broken imports.

- [ ] **Step 3: Run ALL tests to verify nothing broke**

Run: `uv run pytest -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat: remove anthropic dependency — all LLM traffic via hermes

- anthropic>=0.40 removed from pyproject.toml
- All tests pass with hermes_adapter as the only LLM backend
- Model policy (fallback, auth, pinning) now managed by Hermes"
```

---

## Task 5: Final Verification

**Files:** None (verification only)

- [ ] **Step 1: Verify no Anthropic imports remain**

Run: `rg "import anthropic|from anthropic" hermes_pipeline/`
Expected: 0 matches — no Anthropic imports anywhere in the codebase.

- [ ] **Step 2: Verify no Anthropic SDK usage**

Run: `rg "Anthropic(" hermes_pipeline/`
Expected: 0 matches.

- [ ] **Step 3: Verify all tests pass**

Run: `uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 4: Verify TODO-6 success criteria**

From the corrected plan and design doc:
1. `uv run pytest` passes — all existing tests green
2. `anthropic` removed from `pyproject.toml` — no direct SDK calls
3. Two call sites replaced: `_hermes_call` in `decision/agent.py`, `hermes_agent_call` in `phases.py`
4. Error handling works — `HermesCallError` raised on non-zero exit
5. Model fallback handled by Hermes via `hermes fallback` — no custom logic

- [ ] **Step 5: Update TODOS.md**

Mark TODO-6 as done in `TODOS.md`. Find the TODO-6 line and change:

**Before:**
```
TODO-6: route LLM queries through `hermes` instead of direct Claude calls
```

**After:**
```
TODO-6 [x]: route LLM queries through `hermes` instead of direct Claude calls
```

- [ ] **Step 6: Commit**

```bash
git add TODOS.md
git commit -m "docs: mark TODO-6 done — LLM queries routed through hermes

Phase A complete: replaced two Anthropic call sites with hermes chat -q.
Phase B (Kanban-Native) is a future migration per CEO plan."
```

---

## Self-Review

**1. Spec coverage:**
- [x] Create hermes_adapter.py with hermes_call and hermes_agent_call → Task 1
- [x] Replace _anthropic_call with hermes_call → Task 2
- [x] Replace _run_claude_subprocess with hermes_agent_call → Task 3
- [x] Remove anthropic from pyproject.toml → Task 4
- [x] Run all tests → Task 4 Step 3, Task 5 Step 3
- [x] Error handling (HermesCallError with returncode/stderr) → Task 1 tests
- [x] PID tracking (on_pid callback) → Task 1 test
- [x] Timeout handling → Task 1 test
- [x] Model resolution ("auto" omits -m flag) → Task 1 test
- [x] Tools encoding in AGENT_MODE header → Task 1 test
- [x] --source tool flag → Task 1 tests
- [x] Verify no Anthropic imports remain → Task 5 Step 1-2

**2. Placeholder scan:** No "TBD", "TODO", "implement later" found. Every step has code blocks or exact commands.

**3. Type consistency:**
- `HermesAgentResult` defined in Task 1, used in Task 3 — field names match (`returncode`, `stdout`, `stderr`, `timed_out`)
- `HermesCallError` defined in Task 1, tested in Task 1 — includes `returncode` and `stderr` attributes
- `_run_claude_subprocess` return dict shape preserved — `{"returncode": ..., "stdout": ..., "stderr": ..., "timed_out": ...}` — same as before
- Import path in Task 2: `from .. import hermes_adapter` — correct for `hermes_pipeline/decision/agent.py` (parent of `decision` is `hermes_pipeline`)
- Import path in Task 3: `from .hermes_adapter import hermes_agent_call` — correct for `hermes_pipeline/phases.py` (same package level)
- Monkey-patch targets: Task 2 patches `_hermes_call`, Task 3 patches stay on `_run_claude_subprocess` (unchanged name)

**4. Source doc alignment:**
- [x] Design doc Phase 1: "Replace _anthropic_call() with hermes chat -q, add hermes fallback config, remove anthropic" — covered (fallback is Hermes-native, no Python config needed)
- [x] CEO plan Phase A: "Replace two call sites, keep state machine, keep tests" — covered
- [x] CEO plan: "hermes model sets default model+provider" — covered via "auto" model parameter
- [x] Codex corrected plan: all Phase A steps covered

---

Plan complete and saved to `docs/superpowers/plans/2026-06-15-todo-6-hermes-adapter.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
