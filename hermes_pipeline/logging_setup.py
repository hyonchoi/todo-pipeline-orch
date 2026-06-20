from __future__ import annotations
import logging
import logging.handlers
import secrets
import sys
import time
from pathlib import Path

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_tick_id: str | None = None

def new_tick_id() -> str:
    """ULID-ish: 26 chars, Crockford base32, time-sortable prefix."""
    t = int(time.time() * 1000)
    time_part = ""
    for _ in range(10):
        time_part = _CROCKFORD[t & 0x1F] + time_part
        t >>= 5
    rand_part = "".join(_CROCKFORD[b & 0x1F] for b in secrets.token_bytes(16))
    return (time_part + rand_part)[:26]

def set_tick_id(tid: str | None) -> None:
    global _tick_id
    _tick_id = tid

def _current_tick_id() -> str:
    return _tick_id or "-"

class _TickFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.tick_id = _current_tick_id()
        return True

def configure(log_path: Path, retention_days: int = 7, level: int = logging.INFO) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s tick_id=%(tick_id)s %(name)s %(message)s"
    )
    file_h = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=retention_days, encoding="utf-8",
    )
    file_h.setFormatter(fmt)
    file_h.addFilter(_TickFilter())
    err_h = logging.StreamHandler(sys.stderr)
    err_h.setFormatter(fmt)
    err_h.addFilter(_TickFilter())
    root = logging.getLogger()
    # Close old handlers to prevent file-descriptor leaks on reconfigure.
    for h in list(root.handlers):
        h.close()
    root.handlers = [file_h, err_h]
    root.setLevel(level)

    # pipeline.verbose logger — INFO when --verbose, WARNING (off) by default.
    verbose_logger = logging.getLogger("pipeline.verbose")
    verbose_logger.setLevel(logging.WARNING)
