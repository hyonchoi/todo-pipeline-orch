"""Hermes CLI adapter — replaces direct Anthropic calls with `hermes chat -q`.

Two functions:
- hermes_call(): simple one-shot query (replaces _anthropic_call in decision/agent.py)
- hermes_agent_call(): agent-style subprocess with PID tracking (replaces
  _run_claude_subprocess in phases.py)
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

MAX_ERROR_OUTPUT = 300  # chars of stdout/stderr to include in error messages
HERMES_AGENT_DEFAULT_TIMEOUT = 1800  # 30-minute default for agent calls
HERMES_RETRY_ATTEMPTS = 2  # retries for transient CLI failures
HERMES_RETRY_DELAY = 1  # seconds between retries


class HermesCallError(Exception):
    """Raised when `hermes chat -q` returns non-zero exit code."""

    def __init__(self, message: str, returncode: int, stderr: str):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class HermesAgentResult:
    """Result from an agent-style hermes call — matches the shape of
    _run_hermes_subprocess return value for drop-in compatibility."""
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
        "-Q",
    ]
    if model != "auto":
        cmd.extend(["-m", model])
    cmd.extend(["--source", "tool"])

    last_err = None
    for attempt in range(1 + HERMES_RETRY_ATTEMPTS):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=prompt,
            )
            break
        except subprocess.TimeoutExpired:
            raise  # timeout is never transient
        except FileNotFoundError:
            raise  # hermes binary missing is never transient
        except OSError as exc:
            last_err = exc
            if attempt < HERMES_RETRY_ATTEMPTS:
                time.sleep(HERMES_RETRY_DELAY)

    if last_err is not None:
        raise last_err

    if result.returncode != 0:
        raise HermesCallError(
            message=(
                f"hermes chat failed: rc={result.returncode} "
                f"stdout={result.stdout[:MAX_ERROR_OUTPUT]} "
                f"stderr={result.stderr[:MAX_ERROR_OUTPUT]}"
            ),
            returncode=result.returncode,
            stderr=result.stderr,
        )

    return result.stdout.strip()


def hermes_agent_call(
    *,
    prompt: str,
    model: str = "auto",
    tools: str = "",
    turns: int = 25,
    timeout: int = HERMES_AGENT_DEFAULT_TIMEOUT,
    cwd: str | None = None,
    on_pid: callable | None = None,
) -> HermesAgentResult:
    """Call `hermes chat -q` in agent mode and return structured result.

    Drop-in replacement for _run_hermes_subprocess. The prompt is augmented
    with agent constraints (tools availability, turn limit) encoded in the
    system prompt portion since `hermes chat -q` does not have --tools/--turns
    flags — Hermes manages those internally.

    WARNING: Tool and turn constraints are prompt-only. `hermes chat -q` has
    no per-call --tools or --turns flags, so the worker's actual access is
    governed by Hermes config, not by the Phase's tools string. A phase
    configured with ``tools: "Read"`` will get Hermes' full tool access unless
    the Hermes config restricts it. TODO-6 follow-up: migrate to Hermes
    per-call tool scoping once the CLI supports it.

    Args:
        prompt: The prompt text to send.
        model: Model identifier. "auto" lets Hermes resolve from config.
        tools: Comma-separated tool list (e.g. "Read,Write,Bash"). Empty string
            means no tools. Passed in the prompt header — **not enforced** by
            Hermes at the CLI level.
        turns: Maximum turns (encoded in prompt, Hermes enforces).
        timeout: Seconds before killing the process.
        cwd: Working directory for the subprocess.
        on_pid: Callback fired with subprocess PID immediately after spawn.

    Returns:
        HermesAgentResult with returncode, stdout, stderr, timed_out.
    """
    # Build augmented prompt with agent constraints.
    # Note: these constraints are advisory — Hermes chat -q lacks --tools/--turns.
    if tools:
        tools_str = tools
        access_line = f"Available tools: {tools}. Do not use tools not listed."
    else:
        tools_str = "none"
        access_line = "Do not use tools."
    agent_header = (
        f"AGENT_MODE: tools={tools_str}, max_turns={turns}. "
        f"{access_line} Complete the task within {turns} turns.\n\n"
    )
    augmented_prompt = agent_header + prompt

    try:
        cmd = [
            "hermes", "chat", "-q",
            "-Q",
        ]
        if model != "auto":
            cmd.extend(["-m", model])
        cmd.extend(["--source", "tool"])

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
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

        stdout, stderr = proc.communicate(input=augmented_prompt, timeout=timeout)
        return HermesAgentResult(
            returncode=proc.returncode or 0,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=False,
        )

    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate()
        except KeyboardInterrupt:
            raise
        return HermesAgentResult(
            returncode=-1,
            stdout=stdout or "",
            stderr=stderr + " [killed on timeout]",
            timed_out=True,
        )
