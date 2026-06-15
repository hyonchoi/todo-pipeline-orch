import subprocess
from unittest.mock import patch
from hermes_pipeline.slack import notify

def test_notify_calls_hermes_chan_message():
    with patch("subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        notify("ops", "📝 hello")
        run.assert_called_once_with(
            ["hermes", "chan", "message", "ops", "📝 hello"],
            capture_output=True, text=True, timeout=10,
        )

def test_notify_swallows_failure():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        notify("ops", "msg")  # must not raise

def test_notify_skips_when_channel_empty():
    with patch("subprocess.run") as run:
        notify("", "msg")
        run.assert_not_called()
