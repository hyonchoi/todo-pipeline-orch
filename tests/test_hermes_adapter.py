from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest
import signal
import subprocess

from hermes_pipeline.hermes_adapter import (
    hermes_call,
    HermesCallError,
    hermes_agent_call,
    HermesAgentResult,
    check_hermes,
    HermesDependencyError,
    HERMES_RETRY_ATTEMPTS,
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
        "hermes", "chat", "-q", "test prompt",
        "-Q",
        "-m", "claude-sonnet-4-6",
        "--source", "tool",
    ]
    assert mock_run.call_args[1]["timeout"] == 60
    # Prompt is passed as -q CLI arg, not via stdin
    assert "input" not in mock_run.call_args[1]


def test_hermes_call_omits_model_flag_when_auto():
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "ok"
    fake_result.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result) as mock_run:
        hermes_call(prompt="test", model="auto")

    cmd = mock_run.call_args[0][0]
    assert "-m" not in cmd


def test_hermes_call_returns_empty_string_on_no_output():
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = ""
    fake_result.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        result = hermes_call(prompt="test", model="auto")

    assert result == ""


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
            tools="Read,Write,Bash",
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
            tools="Read,Write",
            turns=15,
        )

    # The augmented prompt is passed as -q CLI arg
    cmd = mock_popen.call_args[0][0]
    prompt_arg = cmd[3]  # cmd[3] is the -q argument
    assert "AGENT_MODE" in prompt_arg
    assert "tools=Read,Write" in prompt_arg
    assert "Available tools: Read,Write" in prompt_arg
    assert "max_turns=15" in prompt_arg
    assert "do something" in prompt_arg
    # CLI-level enforcement flags
    assert "-t" in cmd
    assert "Read,Write" in cmd
    assert "--max-turns" in cmd
    assert "15" in cmd


def test_hermes_agent_call_no_tools_prompt_not_contradictory():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.pid = 12345
    fake_proc.communicate.return_value = ("ok", "")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc) as mock_popen:
        hermes_agent_call(
            prompt="no tools here",
            tools="",
            turns=10,
        )

    cmd = mock_popen.call_args[0][0]
    prompt_arg = cmd[3]  # cmd[3] is the -q argument
    assert "tools=none" in prompt_arg
    assert "Do not use tools." in prompt_arg
    # Must NOT contain the contradictory "You have tool access"
    assert "You have tool access" not in prompt_arg
    # No -t flag when no tools
    assert "-t" not in cmd


def test_hermes_agent_call_handles_timeout():
    fake_proc = MagicMock()
    fake_proc.pid = 99999

    call_count = [0]

    def communicate(input=None, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)
        return ("", "timed out")

    fake_proc.communicate = communicate
    pid_seen = []

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        with patch("os.killpg"):
            result = hermes_agent_call(
                prompt="slow task",
                timeout=10,
                on_pid=pid_seen.append,
            )

    assert result.returncode == -1
    assert result.timed_out is True
    # Process group kill (not proc.kill) is used to prevent orphaned children
    assert pid_seen == [99999], "on_pid callback must fire"


def test_hermes_agent_call_respects_cwd():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate.return_value = ("ok", "")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc) as mock_popen:
        hermes_agent_call(
            prompt="test",
            tools="Read",
            cwd="/some/path",
        )

    assert mock_popen.call_args[1]["cwd"] == "/some/path"


def test_hermes_agent_call_keyboard_interrupt_not_masked():
    """KeyboardInterrupt during post-timeout communicate must propagate."""
    fake_proc = MagicMock()
    fake_proc.pid = 77777

    def communicate(input=None, timeout=None):
        raise KeyboardInterrupt()

    fake_proc.communicate = communicate

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        with pytest.raises(KeyboardInterrupt):
            hermes_agent_call(
                prompt="slow task",
                tools="Read",
                timeout=10,
            )


def test_hermes_call_error_includes_stdout():
    """HermesCallError message should include stdout for debugging."""
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = "partial result"
    fake_result.stderr = "E100: error"

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        with pytest.raises(HermesCallError, match="partial result"):
            hermes_call(prompt="hello")


# === GAP-FILLING TESTS ===


def test_hermes_call_file_not_found_error_propagates():
    """subprocess.run raises FileNotFoundError when 'hermes' binary is not found — must propagate up."""
    with patch(
        "hermes_pipeline.hermes_adapter.subprocess.run",
        side_effect=FileNotFoundError("hermes"),
    ):
        with pytest.raises(FileNotFoundError):
            hermes_call(prompt="hello")


def test_hermes_call_timeout_expired_propagates():
    """subprocess.run raises TimeoutExpired when the process exceeds the timeout — must propagate up."""
    with patch(
        "hermes_pipeline.hermes_adapter.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=10),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            hermes_call(prompt="hello", timeout=10)


def test_hermes_agent_call_nonzero_return_code():
    """When hermes exits with a non-zero code, the result should carry that code, not raise."""
    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.pid = 12345
    fake_proc.communicate.return_value = ("", "error output")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        result = hermes_agent_call(prompt="do something")

    assert result.returncode == 1
    assert result.stderr == "error output"
    assert result.timed_out is False


def test_hermes_agent_call_returncode_none_coalesced_to_zero():
    """When Popen.returncode is None (shouldn't happen after communicate, but defensible), coalesce to 0."""
    fake_proc = MagicMock()
    fake_proc.returncode = None
    fake_proc.pid = 12345
    fake_proc.communicate.return_value = ("ok", "")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        result = hermes_agent_call(prompt="do something")

    assert result.returncode == 0


def test_hermes_agent_call_popen_file_not_found_propagates():
    """When Popen raises FileNotFoundError (hermes not in PATH), propagate up."""
    with patch(
        "hermes_pipeline.hermes_adapter.subprocess.Popen",
        side_effect=FileNotFoundError("hermes"),
    ):
        with pytest.raises(FileNotFoundError):
            hermes_agent_call(prompt="hello")


def test_hermes_agent_call_on_pid_callback_exception_suppressed():
    """When on_pid callback raises, the exception must be silently suppressed."""
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.pid = 12345
    fake_proc.communicate.return_value = ("ok", "")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        # on_pid that raises should not bubble up
        result = hermes_agent_call(
            prompt="test",
            on_pid=lambda pid: (_ for _ in ()).throw(ValueError("boom")),
        )

    assert result.returncode == 0


def test_hermes_agent_call_timeout_stderr_augmented():
    """After timeout kill, stderr must include '[killed on timeout]' marker."""
    fake_proc = MagicMock()
    fake_proc.pid = 99999

    call_count = [0]

    def communicate(input=None, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)
        # Second call after kill — returns output
        return ("", "killed")

    fake_proc.communicate = communicate

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        result = hermes_agent_call(prompt="slow", timeout=5)

    assert result.timed_out is True
    assert "[killed on timeout]" in result.stderr


# === RETRY AND TIMEOUT EDGE-CASE TESTS (CRITICAL — pre-landing review) ===


def test_hermes_call_retries_on_os_error():
    """hermes_call should retry on transient OSError up to HERMES_RETRY_ATTEMPTS times
    and call time.sleep between retries."""
    call_count = [0]

    def fake_run(*a, **kw):
        call_count[0] += 1
        if call_count[0] < 2:
            raise OSError("Temporary network issue")
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok"
        result.stderr = ""
        return result

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", side_effect=fake_run):
        with patch("hermes_pipeline.hermes_adapter.time.sleep") as mock_sleep:
            result = hermes_call(prompt="test")

    assert result == "ok"
    assert call_count[0] == 2, "Should retry once after OSError"
    mock_sleep.assert_called_once()


def test_hermes_call_oserror_exhaust():
    """When OSError persists across all retries, the last OSError should be raised."""
    call_count = [0]

    def always_fail(*a, **kw):
        call_count[0] += 1
        raise OSError("Persistent network failure")

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", side_effect=always_fail):
        with pytest.raises(OSError, match="Persistent network failure"):
            hermes_call(prompt="test")

    assert call_count[0] == 1 + HERMES_RETRY_ATTEMPTS, "Should attempt 1 + 2 retries = 3 times"


def test_hermes_call_retry_succeeds_on_second():
    """When OSError occurs on first attempt but second succeeds, no further retries."""
    call_count = [0]

    def fail_once_then_succeed(*a, **kw):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("Transient failure")
        result = MagicMock()
        result.returncode = 0
        result.stdout = "recovered"
        result.stderr = ""
        return result

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", side_effect=fail_once_then_succeed):
        result = hermes_call(prompt="test")

    assert result == "recovered"
    assert call_count[0] == 2


def test_hermes_call_timeout_clamping():
    """_api_call should clamp timeout to [MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS]."""
    from hermes_pipeline.decision.agent import (
        TOKENS_PER_SECOND,
        MIN_TIMEOUT_SECONDS,
        MAX_TIMEOUT_SECONDS,
    )

    captured_timeout = []

    def fake_hermes_call(*, prompt, model, timeout):
        captured_timeout.append(timeout)
        return "ok"

    with patch("hermes_pipeline.hermes_adapter.hermes_call", fake_hermes_call):
        from hermes_pipeline.decision.agent import _api_call
        # max_tokens=0 -> clamped to MIN (30s)
        _api_call(model="m", max_tokens=0, prompt="test", backend="hermes")
        assert captured_timeout[-1] == MIN_TIMEOUT_SECONDS, "0 tokens should clamp to min"

        # max_tokens=50 -> 50//100=0, clamped to MIN (30s)
        _api_call(model="m", max_tokens=50, prompt="test", backend="hermes")
        assert captured_timeout[-1] == MIN_TIMEOUT_SECONDS, "50 tokens should clamp to min"

        # max_tokens=5000 -> 5000//100=50s, no clamp
        _api_call(model="m", max_tokens=5000, prompt="test", backend="hermes")
        assert captured_timeout[-1] == 50, "5000 tokens should be 50s"

        # max_tokens=100000 -> 100000//100=1000s, clamped to MAX (300s)
        _api_call(model="m", max_tokens=100000, prompt="test", backend="hermes")
        assert captured_timeout[-1] == MAX_TIMEOUT_SECONDS, "100k tokens should clamp to max"


# === CHECK_HERMES PREFLIGHT TESTS ===


def test_check_hermes_returns_version():
    """check_hermes returns the version string when hermes is available."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "hermes 0.3.0\n"
    fake_result.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        result = check_hermes()

    assert result == "hermes 0.3.0"


def test_check_hermes_raises_on_file_not_found():
    """check_hermes raises HermesDependencyError when hermes is not in PATH."""
    with patch(
        "hermes_pipeline.hermes_adapter.subprocess.run",
        side_effect=FileNotFoundError("hermes"),
    ):
        with pytest.raises(HermesDependencyError, match="not found in PATH"):
            check_hermes()


def test_check_hermes_raises_on_timeout():
    """check_hermes raises HermesDependencyError when hermes --version hangs."""
    with patch(
        "hermes_pipeline.hermes_adapter.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=10),
    ):
        with pytest.raises(HermesDependencyError, match="timed out"):
            check_hermes()


def test_check_hermes_raises_on_nonzero_exit():
    """check_hermes raises HermesDependencyError when hermes --version fails."""
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "hermes: config error"

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        with pytest.raises(HermesDependencyError, match="config error"):
            check_hermes()


# === HERMES_AGENT_CALL: PROCESS GROUP KILL ON TIMEOUT ===


def test_hermes_agent_call_timeout_kills_process_group():
    """On timeout, hermes_agent_call kills the entire process group, not just the process."""
    import os
    fake_proc = MagicMock()
    fake_proc.pid = 99999

    call_count = [0]

    def communicate(input=None, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)
        return ("", "killed")

    fake_proc.communicate = communicate

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        with patch("os.killpg") as mock_killpg:
            hermes_agent_call(prompt="slow task", timeout=10)

    mock_killpg.assert_called_once_with(99999, signal.SIGKILL)
    # Verify proc.kill() is NOT called
    fake_proc.kill.assert_not_called()


def test_hermes_agent_call_timeout_communicate_has_timeout():
    """Post-kill communicate must have a timeout to prevent hanging."""
    import os
    fake_proc = MagicMock()
    fake_proc.pid = 99999

    call_count = [0]

    def communicate(input=None, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)
        # Verify timeout is passed (should be 5)
        return ("", "killed")

    fake_proc.communicate = communicate

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        with patch("os.killpg"):
            result = hermes_agent_call(prompt="slow", timeout=10)

    assert result.timed_out is True


def test_hermes_agent_call_timeout_process_group_lookup_error():
    """ProcessLookupError from killpg on timeout is handled gracefully."""
    fake_proc = MagicMock()
    fake_proc.pid = 99999

    call_count = [0]

    def communicate(input=None, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)
        return ("", "")

    fake_proc.communicate = communicate

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        with patch("os.killpg", side_effect=ProcessLookupError):
            result = hermes_agent_call(prompt="slow", timeout=10)

    assert result.timed_out is True
    assert result.returncode == -1


# === HERMES_AGENT_CALL: RETRY LOGIC ===


def test_hermes_agent_call_retries_on_os_error():
    """hermes_agent_call should retry on transient OSError up to HERMES_RETRY_ATTEMPTS times."""
    call_count = [0]

    def fake_popen(*a, **kw):
        call_count[0] += 1
        if call_count[0] < 2:
            raise OSError("Temporary spawn failure")
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 12345
        proc.communicate.return_value = ("ok", "")
        return proc

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", side_effect=fake_popen):
        with patch("hermes_pipeline.hermes_adapter.time.sleep") as mock_sleep:
            result = hermes_agent_call(prompt="test")

    assert result.returncode == 0
    assert call_count[0] == 2, "Should retry once after OSError"
    mock_sleep.assert_called_once()


def test_hermes_agent_call_oserror_exhaust():
    """When OSError persists across all retries, the last OSError should be raised."""
    call_count = [0]

    def always_fail(*a, **kw):
        call_count[0] += 1
        raise OSError("Persistent spawn failure")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", side_effect=always_fail):
        with pytest.raises(OSError, match="Persistent spawn failure"):
            hermes_agent_call(prompt="test")

    assert call_count[0] == 1 + HERMES_RETRY_ATTEMPTS, "Should attempt 1 + 2 retries = 3 times"


def test_hermes_agent_call_popen_file_not_found_not_retried():
    """FileNotFoundError from Popen should propagate immediately, not be retried."""
    call_count = [0]

    def fake_popen(*a, **kw):
        call_count[0] += 1
        raise FileNotFoundError("hermes")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", side_effect=fake_popen):
        with pytest.raises(FileNotFoundError):
            hermes_agent_call(prompt="test")

    assert call_count[0] == 1, "FileNotFoundError should not be retried"


def test_hermes_agent_call_timeout_post_kill_communicate_fallback_2s():
    """After SIGKILL, if communicate(timeout=5) ALSO times out,
    fall back to communicate(timeout=2)."""
    fake_proc = MagicMock()
    fake_proc.pid = 99999

    call_count = [0]
    timeouts_seen = []

    def communicate(input=None, timeout=None):
        call_count[0] += 1
        timeouts_seen.append(timeout)
        # All communicate calls time out — the fallback path
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)

    fake_proc.communicate = communicate

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        with patch("os.killpg"):
            result = hermes_agent_call(prompt="slow", timeout=10)

    assert result.timed_out is True
    assert result.returncode == -1
    # First call: original timeout, second: 5s, third (fallback): 2s
    assert timeouts_seen[1] == 5, "Post-kill communicate should use 5s timeout"
    assert timeouts_seen[2] == 2, "Fallback communicate should use 2s timeout"
    assert call_count[0] == 3, "Should attempt 3 communicates total"


def test_hermes_call_permission_error_raises():
    """PermissionError when hermes is not executable should propagate (not retry)."""
    with patch("hermes_pipeline.hermes_adapter.subprocess.run", side_effect=PermissionError("not executable")):
        with pytest.raises(PermissionError, match="not executable"):
            hermes_call(prompt="test")


def test_hermes_call_error_output_truncated():
    """HermesCallError message should truncate stdout/stderr to MAX_ERROR_OUTPUT chars."""
    from hermes_pipeline.hermes_adapter import MAX_ERROR_OUTPUT

    long_output = "x" * (MAX_ERROR_OUTPUT + 100)
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = long_output
    fake_result.stderr = "stderr " + long_output

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        with pytest.raises(HermesCallError) as exc_info:
            hermes_call(prompt="test")

    # The message truncates stdout and stderr to MAX_ERROR_OUTPUT
    msg = str(exc_info.value)
    # stdout portion in the message should be at most MAX_ERROR_OUTPUT
    assert "stdout=" in msg
    # The stderr attribute carries the full stderr (untruncated)
    assert exc_info.value.stderr == "stderr " + long_output, "stderr attribute should carry full stderr"


def test_hermes_agent_call_popen_permission_error_raises():
    """PermissionError from Popen (hermes exists but not executable) should propagate."""
    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", side_effect=PermissionError("denied")):
        with pytest.raises(PermissionError, match="denied"):
            hermes_agent_call(prompt="test")


def test_hermes_agent_call_none_stdout_stderr_coalesced():
    """If communicate returns None for stdout/stderr, or should coalesce to empty string."""
    fake_proc = MagicMock()
    fake_proc.communicate.return_value = ("", "")
    fake_proc.returncode = 0

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        result = hermes_agent_call(prompt="test")

    assert result.stdout == "", "stdout should be empty string"
    assert result.stderr == "", "stderr should be empty string"


# === CLAUDE CALL TESTS ===


def test_claude_call_returns_stdout_on_success():
    """claude_call returns stripped stdout on success."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "  hello world  \n"
    fake_result.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        from hermes_pipeline.hermes_adapter import claude_call
        result = claude_call(prompt="test")

    assert result == "hello world"


def test_claude_call_raises_on_nonzero_exit():
    """claude_call raises ClaudeCallError on non-zero exit code."""
    from hermes_pipeline.hermes_adapter import ClaudeCallError
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "auth failed"

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        from hermes_pipeline.hermes_adapter import claude_call
        with pytest.raises(ClaudeCallError):
            claude_call(prompt="test")


def test_claude_call_passes_model_flag():
    """claude_call should include --model flag when model is not auto."""
    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        r = MagicMock()
        r.returncode = 0
        r.stdout = "ok"
        r.stderr = ""
        return r

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", side_effect=fake_run):
        from hermes_pipeline.hermes_adapter import claude_call
        claude_call(prompt="test", model="claude-sonnet-4-6")

    assert captured_cmd[0] == ["claude", "-p", "test", "--model", "claude-sonnet-4-6"]


def test_claude_call_omits_model_flag_when_auto():
    """claude_call should omit --model flag when model is auto."""
    captured_cmd = []

    def fake_run(cmd, **kwargs):
        captured_cmd.append(cmd)
        r = MagicMock()
        r.returncode = 0
        r.stdout = "ok"
        r.stderr = ""
        return r

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", side_effect=fake_run):
        from hermes_pipeline.hermes_adapter import claude_call
        claude_call(prompt="test", model="auto")

    assert captured_cmd[0] == ["claude", "-p", "test"]


def test_claude_call_passes_timeout():
    """claude_call should pass timeout to subprocess.run."""
    captured_kwargs = {}

    def fake_run(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        r = MagicMock()
        r.returncode = 0
        r.stdout = "ok"
        r.stderr = ""
        return r

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", side_effect=fake_run):
        from hermes_pipeline.hermes_adapter import claude_call
        claude_call(prompt="test", timeout=60)

    assert captured_kwargs["timeout"] == 60


def test_check_claude_returns_version():
    """check_claude returns the version string when claude is available."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "2.1.183 (Claude Code)\n"
    fake_result.stderr = ""

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        from hermes_pipeline.hermes_adapter import check_claude, ClaudeDependencyError
        version = check_claude()

    assert "2.1.183" in version


def test_check_claude_raises_when_not_found():
    """check_claude raises ClaudeDependencyError when claude binary is not found."""
    from hermes_pipeline.hermes_adapter import ClaudeDependencyError

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", side_effect=FileNotFoundError()):
        from hermes_pipeline.hermes_adapter import check_claude
        with pytest.raises(ClaudeDependencyError, match="not found"):
            check_claude()


def test_check_claude_raises_on_version_failure():
    """check_claude raises ClaudeDependencyError when claude --version fails."""
    from hermes_pipeline.hermes_adapter import ClaudeDependencyError
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "some error"

    with patch("hermes_pipeline.hermes_adapter.subprocess.run", return_value=fake_result):
        from hermes_pipeline.hermes_adapter import check_claude
        with pytest.raises(ClaudeDependencyError, match="failed"):
            check_claude()
