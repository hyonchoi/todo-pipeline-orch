import subprocess
from unittest.mock import patch

import pytest

from hermes_pipeline.slack import notify


@pytest.mark.parametrize("message", ["📝 hello", "--to telegram:other", "$(touch /tmp/never)\nnext line"])
def test_notify_calls_hermes_send(message):
    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        notify("ops", message)
        run.assert_called_once_with(
            ["hermes", "send", "--to", "slack:ops", "--", message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            check=True,
        )


@pytest.mark.parametrize("error", [
    FileNotFoundError("SECRET"),
    PermissionError("SECRET"),
    subprocess.TimeoutExpired(["SECRET"], 10, output="SECRET", stderr="SECRET"),
    subprocess.CalledProcessError(2, ["SECRET"], output="SECRET", stderr="SECRET"),
    RuntimeError("SECRET"),
])
def test_notify_swallows_failure_without_provider_details(error, caplog, capsys):
    with patch("subprocess.run", side_effect=error):
        notify("ops", "msg")
    assert "slack notify failed" in caplog.text
    assert "SECRET" not in caplog.text
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize("channel", ["", "   ", "-evil", "ops\n"])
def test_notify_skips_when_channel_empty_or_invalid(channel):
    with patch("subprocess.run") as run:
        notify(channel, "msg")
        run.assert_not_called()
