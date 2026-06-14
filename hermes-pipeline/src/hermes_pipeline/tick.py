"""Tick-level lock — closes overlapping-cron and spawn-failure-orphan races."""
from __future__ import annotations
import contextlib
import datetime as _dt
import json
import os
import time
from pathlib import Path

class TickLockHeld(Exception):
    """Raised when the lock is held and the holder is not stale."""

class TickLock:
    def __init__(self, state_dir: Path | str, *, max_age_min: int):
        self._state_dir = Path(state_dir)
        self._max_age_s = max_age_min * 60

    @property
    def lock_dir(self) -> Path:
        return self._state_dir / "tick.lock"

    def _holder_path(self) -> Path:
        return self.lock_dir / "holder.json"

    def _try_sweep_stale(self) -> None:
        if not self.lock_dir.exists():
            return
        holder = self._holder_path()
        if not holder.exists():
            self.lock_dir.rmdir()
            return
        if time.time() - holder.stat().st_mtime > self._max_age_s:
            holder.unlink()
            self.lock_dir.rmdir()

    @contextlib.contextmanager
    def acquire(self, tick_id: str):
        self._state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.lock_dir.mkdir()
        except FileExistsError:
            self._try_sweep_stale()
            try:
                self.lock_dir.mkdir()
            except FileExistsError as e:
                raise TickLockHeld(f"tick.lock held; tick_id={tick_id} skipped") from e
        self._holder_path().write_text(json.dumps({
            "tick_id": tick_id,
            "acquired_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pid": os.getpid(),
        }, sort_keys=True))
        try:
            yield
        finally:
            try:
                self._holder_path().unlink()
            except FileNotFoundError:
                pass
            try:
                self.lock_dir.rmdir()
            except OSError:
                pass
