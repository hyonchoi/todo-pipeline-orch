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

    call_count = [0]

    def communicate(timeout=None):
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
            cwd="/some/path",
        )

    assert mock_popen.call_args[1]["cwd"] == "/some/path"
