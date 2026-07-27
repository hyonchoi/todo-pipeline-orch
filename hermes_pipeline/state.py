"""State file helpers."""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path


def _atomic_write_text(path: Path, payload: str) -> None:
    """Crash-safe write: tmp + rename. Same-directory tmp keeps rename atomic."""
    tmp = path.with_name(f"{path.name}.{_uuid.uuid4().hex}.tmp")
    tmp.write_text(payload)
    tmp.replace(path)
