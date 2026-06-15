# TODO-6: Replace Anthropic Calls with Hermes Chat

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all direct Anthropic SDK invocations from the orchestrator and route all LLM queries through `hermes chat -q`.

**Architecture:** Create `hermes_adapter.py` with two functions — `hermes_call()` for simple one-shot queries (replaces `_anthropic_call` in the decision agent) and `hermes_agent_call()` for agent-style subprocess calls with PID tracking (replaces `_run_claude_subprocess` in phases). Both functions call `hermes chat -q` as a subprocess. Existing tests are updated to monkey-patch the new functions. Finally, `anthropic` is removed from `pyproject.toml`.

**Tech Stack:** Python 3.12+, `hermes` CLI, `subprocess`, `pyyaml`

---

## File Structure

### New Files
- `hermes_pipeline/hermes_adapter.py` — Two functions: `hermes_call()` and `hermes_agent_call()`. Single responsibility: wrap `hermes chat -q` subprocess calls.

### Modified Files
- `hermes_pipeline/decision/agent.py` — Replace `_anthropic_call()` (lines 67-75) with `hermes_call()` from `hermes_adapter`
- `hermes_pipeline/phases.py` — Replace `_run_claude_subprocess()` (lines 100-136) with `hermes_agent_call()` from `hermes_adapter`
- `tests/test_decision_agent.py` — Update monkey-patch targets from `_anthropic_call` to `hermes_pipeline.hermes_adapter.hermes_call`
- `tests/test_phases_invoke.py` — Update monkey-patch targets from `_run_claude_subprocess` to `hermes_pipeline.hermes_adapter.hermes_agent_call`
- `pyproject.toml` — Remove `anthropic>=0.40` from dependencies
- `docs/gstack/codex-review-corrected-plan.md` — Already written, no changes needed

### Unchanged (kept as-is)
- `runner.py` — PipelineRunner, branch naming, phase loop
- `state.py` — State machine, checkpoints, ready_for_review
- `kanban.py` — Kanban integration (Protocol-based)
- `circuit.py` — Circuit breaker
- `tick.py` — Tick lock
- `merge.py` — Branch merge logic
- All other tests

---

## Task 1: Create `hermes_adapter.py`

**Files:**
- Create: `hermes_pipeline/hermes_adapter.py`
- Test: `tests/test_hermes_adapter.py`

- [ ] **Step 1: Write failing test — hermes_call success case**

Create `tests/test_hermes_adapter.py`:

```python
from __future__ import annotations
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from hermes_pipeline.hermes_adapter import hermes_call, HermesCallError
from hermes_pipeline.hermes_adapter import hermes_agent_call, HermesAgentResult


def test_hermes_call_returns_stdout_on_success():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "plan output here"
    fake_proc.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_proc):
        result = hermes_call(prompt="hello world", model="claude-sonnet-4-6")

    assert result == "plan output here"


def test_hermes_call_raises_on_nonzero_exit():
    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stdout = ""
    fake_proc.stderr = "E100: gateway unreachable"

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_proc):
        with pytest.raises(HermesCallError, match="gateway unreachable"):
            hermes_call(prompt="hello", model="claude-sonnet-4-6")


def test_hermes_call_passes_correct_args():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "ok"
    fake_proc.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_proc) as mock_run:
        hermes_call(prompt="test prompt", model="claude-sonnet-4-6", timeout=60)

    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert "hermes" in cmd
    assert "chat" in cmd
    assert "-q" in cmd
    assert "test prompt" in cmd
    assert "-m" in cmd
    assert "claude-sonnet-4-6" in cmd

    assert call_args[1]["timeout"] == 60
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
import os
from dataclasses import dataclass
from typing import Literal


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
        "-m", model,
        "--source", "tool",
    ]

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
        proc = subprocess.Popen(
            [
                "hermes", "chat", "-q",
                augmented_prompt,
                "-Q",
                "-m", model,
                "--source", "tool",
            ],
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
Expected: 3/3 PASS

- [ ] **Step 5: Write failing test — hermes_agent_call success and timeout**

Add to `tests/test_hermes_adapter.py`:

```python
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
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_hermes_adapter.py -v`
Expected: 5/5 PASS

- [ ] **Step 7: Commit**

```bash
git add hermes_pipeline/hermes_adapter.py tests/test_hermes_adapter.py
git commit -m "feat: add hermes_adapter to replace direct Anthropic calls

- hermes_call(): one-shot query via hermes chat -q
- hermes_agent_call(): agent-style call with PID tracking
- Both functions wrap subprocess calls, no external SDKs"
```

---

## Task 2: Replace `_anthropic_call` in `decision/agent.py`

**Files:**
- Modify: `hermes_pipeline/decision/agent.py:67-75`
- Test: `tests/test_decision_agent.py`

- [ ] **Step 1: Replace `_anthropic_call` function with `hermes_call`**

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
    from . import hermes_adapter

    result = hermes_adapter.hermes_call(
        model=model,
        prompt=prompt,
        timeout=min(max_tokens * 2, 300),  # Rough estimate: 2s per 1000 tokens, cap at 5min
    )
    return result
```

- [ ] **Step 2: Update `call_agent` to handle `HermesCallError`**

In `call_agent()`, the current code:

```python
def call_agent(
    *,
    ctx: SelectionContext,
    prompt_path: Path,
    model: str,
    max_tokens: int,
    expected_sha: str | None,
) -> AgentResult:
    actual_sha = compute_prompt_sha(prompt_path)
    if expected_sha is not None and expected_sha != actual_sha:
        raise PromptShaMismatch(expected_sha, actual_sha)
    rendered = build_prompt(prompt_path, ctx)
    raw = _anthropic_call(model=model, max_tokens=max_tokens, prompt=rendered)
    return AgentResult(parsed=_parse(raw), prompt_sha=actual_sha, raw_response=raw)
```

**After:**

```python
def call_agent(
    *,
    ctx: SelectionContext,
    prompt_path: Path,
    model: str,
    max_tokens: int,
    expected_sha: str | None,
) -> AgentResult:
    actual_sha = compute_prompt_sha(prompt_path)
    if expected_sha is not None and expected_sha != actual_sha:
        raise PromptShaMismatch(expected_sha, actual_sha)
    rendered = build_prompt(prompt_path, ctx)
    raw = _hermes_call(model=model, max_tokens=max_tokens, prompt=rendered)
    return AgentResult(parsed=_parse(raw), prompt_sha=actual_sha, raw_response=raw)
```

Note: `HermesCallError` will propagate as-is — existing callers expect exceptions to bubble up from `call_agent`.

- [ ] **Step 3: Update `tests/test_decision_agent.py` monkey-patch targets**

In `tests/test_decision_agent.py`, update all references from `_anthropic_call` to `_hermes_call`:

**Before (line 46-48):**
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

- _hermes_call() uses hermes_adapter.hermes_call() instead of Anthropic SDK
- call_agent() unchanged except function name
- Tests monkey-patch _hermes_call instead of _anthropic_call"
```

---

## Task 3: Replace `_run_claude_subprocess` in `phases.py`

**Files:**
- Modify: `hermes_pipeline/phases.py:100-136`
- Test: `tests/test_phases_invoke.py`

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
    The `tools` and `turns` parameters are encoded in the agent prompt header.
    Tests monkey-patch this function to avoid hitting the real CLI.
    """
    from .hermes_adapter import hermes_agent_call

    result = hermes_agent_call(
        prompt=prompt,
        tools=tools != "none",
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

- [ ] **Step 2: Update `tests/test_phases_invoke.py` monkey-patch targets**

The existing tests in `test_phases_invoke.py` already monkey-patch `_run_claude_subprocess` — since we kept the same function name and return shape, these patches continue to work without changes. Verify by running:

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

- _run_claude_subprocess now calls hermes_adapter.hermes_agent_call()
- Same return dict shape for drop-in compatibility
- tests/test_phases_invoke.py patches still work unchanged"
```

---

## Task 4: Remove Anthropic Dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Remove `anthropic>=0.40` from dependencies**

In `pyproject.toml`, replace:

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
- All tests pass with hermes_adapter as the only LLM backend"
```

---

## Task 5: Final Verification

**Files:** None (verification only)

- [ ] **Step 1: Verify no Anthropic imports remain**

Run: `rg "import anthropic|from anthropic" hermes_pipeline/`
Expected: 0 matches — no Anthropic imports anywhere in the codebase.

- [ ] **Step 2: Verify all tests pass**

Run: `uv run pytest -v --tb=short`
Expected: ALL PASS

- [ ] **Step 3: Verify TODO-6 success criteria from codex-review-corrected-plan.md**

Checklist:
1. `uv run pytest` passes — all existing tests green
2. `anthropic` removed from `pyproject.toml` — no direct SDK calls
3. Two call sites replaced: `_hermes_call` in `decision/agent.py`, `hermes_agent_call` in `phases.py`
4. Error handling works — `HermesCallError` raised on non-zero exit

- [ ] **Step 4: Update TODOS.md**

Mark TODO-6 as done in `TODOS.md`:

**Before:** `TODO-6: route LLM queries through \`hermes\` instead of direct Claude calls`
**After:** `TODO-6 [x]: route LLM queries through \`hermes\` instead of direct Claude calls`

- [ ] **Step 5: Commit**

```bash
git add TODOS.md
git commit -m "docs: mark TODO-6 done — LLM queries routed through hermes"
```

---

## Self-Review

**1. Spec coverage:**
- [x] Create hermes_adapter.py with hermes_call and hermes_agent_call → Task 1
- [x] Replace _anthropic_call with hermes_call → Task 2
- [x] Replace _run_claude_subprocess with hermes_agent_call → Task 3
- [x] Remove anthropic from pyproject.toml → Task 4
- [x] Run all tests → Task 4 Step 3, Task 5 Step 2
- [x] Error handling (HermesCallError) → Task 1 tests
- [x] PID tracking (on_pid callback) → Task 1 hermes_agent_call tests
- [x] Timeout handling → Task 1 timeout test

**2. Placeholder scan:** No "TBD", "TODO", "implement later" found. Every step has code blocks or exact commands.

**3. Type consistency:**
- `HermesAgentResult` defined in Task 1, used in Task 3 — field names match (`returncode`, `stdout`, `stderr`, `timed_out`)
- `HermesCallError` defined in Task 1, tested in Task 1
- `_run_claude_subprocess` return dict shape preserved — `{"returncode": ..., "stdout": ..., "stderr": ..., "timed_out": ...}` — same as before
- Monkey-patch targets in Task 2 (`_hermes_call`) and Task 3 (no change needed — `_run_claude_subprocess` name preserved)

---

Plan complete. Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
