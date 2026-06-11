from __future__ import annotations
import logging
import subprocess

log = logging.getLogger(__name__)

def notify(channel: str, message: str) -> None:
    """Send a Slack message via `hermes chan message`. Best-effort, never raises."""
    if not channel:
        return
    try:
        subprocess.run(
            ["hermes", "chan", "message", channel, message],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log.warning("slack notify failed: %s", e)
