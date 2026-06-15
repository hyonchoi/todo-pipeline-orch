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
        "-Q",
        "-m", "claude-sonnet-4-6",
        "--source", "tool",
    ]
    assert mock_run.call_args[1]["timeout"] == 60
    # Prompt is passed via stdin, not as a CLI arg
    assert mock_run.call_args[1]["input"] == "test prompt"


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

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        hermes_agent_call(
            prompt="do something",
            tools="Read,Write",
            turns=15,
        )

    # The augmented prompt is passed via stdin to communicate()
    comm_args = fake_proc.communicate.call_args
    prompt_arg = comm_args[1]["input"] if comm_args[1] else comm_args[0][0]
    assert "AGENT_MODE" in prompt_arg
    assert "tools=Read,Write" in prompt_arg
    assert "Available tools: Read,Write" in prompt_arg
    assert "max_turns=15" in prompt_arg
    assert "do something" in prompt_arg


def test_hermes_agent_call_no_tools_prompt_not_contradictory():
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.pid = 12345
    fake_proc.communicate.return_value = ("ok", "")

    with patch("hermes_pipeline.hermes_adapter.subprocess.Popen", return_value=fake_proc):
        hermes_agent_call(
            prompt="no tools here",
            tools="",
            turns=10,
        )

    comm_args = fake_proc.communicate.call_args
    prompt_arg = comm_args[1]["input"] if comm_args[1] else comm_args[0][0]
    assert "tools=none" in prompt_arg
    assert "Do not use tools." in prompt_arg
    # Must NOT contain the contradictory "You have tool access"
    assert "You have tool access" not in prompt_arg


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
