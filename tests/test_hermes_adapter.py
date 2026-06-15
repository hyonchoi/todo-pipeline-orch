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

    assert call_count[0] == 3, "Should attempt 1 + 2 retries = 3 times"


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
    """_hermes_call should clamp timeout to [MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS]."""
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
        from hermes_pipeline.decision.agent import _hermes_call
        # max_tokens=0 -> clamped to MIN (30s)
        _hermes_call(model="m", max_tokens=0, prompt="test")
        assert captured_timeout[-1] == MIN_TIMEOUT_SECONDS, "0 tokens should clamp to min"

        # max_tokens=50 -> 50//100=0, clamped to MIN (30s)
        _hermes_call(model="m", max_tokens=50, prompt="test")
        assert captured_timeout[-1] == MIN_TIMEOUT_SECONDS, "50 tokens should clamp to min"

        # max_tokens=5000 -> 5000//100=50s, no clamp
        _hermes_call(model="m", max_tokens=5000, prompt="test")
        assert captured_timeout[-1] == 50, "5000 tokens should be 50s"

        # max_tokens=100000 -> 100000//100=1000s, clamped to MAX (300s)
        _hermes_call(model="m", max_tokens=100000, prompt="test")
        assert captured_timeout[-1] == MAX_TIMEOUT_SECONDS, "100k tokens should clamp to max"
