from __future__ import annotations
import os
import time
from pathlib import Path
import pytest
from hermes_pipeline.tick import TickLock, TickLockHeld

def test_acquire_release(tmp_path):
    lk = TickLock(tmp_path, max_age_min=10)
    with lk.acquire("01JA"):
        assert (tmp_path / "tick.lock").is_dir()
        assert (tmp_path / "tick.lock" / "holder.json").exists()
    assert not (tmp_path / "tick.lock").exists()

def test_second_acquire_raises(tmp_path):
    lk = TickLock(tmp_path, max_age_min=10)
    with lk.acquire("01JA"):
        with pytest.raises(TickLockHeld):
            with lk.acquire("01JB"):
                pass

def test_stale_lock_swept(tmp_path):
    (tmp_path / "tick.lock").mkdir()
    holder = tmp_path / "tick.lock" / "holder.json"
    holder.write_text('{"tick_id": "old", "acquired_at": "2020-01-01T00:00:00Z"}')
    old = time.time() - 60 * 60 * 24
    os.utime(holder, (old, old))
    lk = TickLock(tmp_path, max_age_min=10)
    with lk.acquire("01JNEW"):
        assert "01JNEW" in (tmp_path / "tick.lock" / "holder.json").read_text()

def test_release_on_exception(tmp_path):
    lk = TickLock(tmp_path, max_age_min=10)
    with pytest.raises(RuntimeError):
        with lk.acquire("01JA"):
            raise RuntimeError("boom")
    assert not (tmp_path / "tick.lock").exists()

def test_lock_dir_without_holder_is_not_swept_eagerly(tmp_path):
    """Regression: a contender must not rmdir a freshly-mkdir'd lock_dir
    before the owner has written holder.json. Doing so allowed two ticks
    to both pass acquire()."""
    (tmp_path / "tick.lock").mkdir()  # owner mid-bootstrap; no holder yet
    lk = TickLock(tmp_path, max_age_min=10)
    with pytest.raises(TickLockHeld):
        with lk.acquire("01JB"):
            pass
    assert (tmp_path / "tick.lock").exists(), "contender must not steal a bootstrapping lock"
