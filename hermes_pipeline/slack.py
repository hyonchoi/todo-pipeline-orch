from __future__ import annotations

import logging
import subprocess

from .project_config import _is_valid_slack_channel

log = logging.getLogger(__name__)

def notify(channel: str, message: str) -> None:
    """Send to an explicitly configured Slack destination; failures are best-effort."""
    if not _is_valid_slack_channel(channel):
        return
    try:
        subprocess.run(
            ["hermes", "send", "--to", f"slack:{channel}", "--", message],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        log.warning("slack notify failed: exit_status=%d", exc.returncode)
    except Exception as exc:
        log.warning("slack notify failed: %s", type(exc).__name__)
