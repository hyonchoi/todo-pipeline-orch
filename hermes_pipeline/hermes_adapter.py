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
    ]
    if model != "auto":
        cmd.extend(["-m", model])
    cmd.extend(["--source", "tool"])

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
        ]
        if model != "auto":
            cmd.extend(["-m", model])
        cmd.extend(["--source", "tool"])

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
