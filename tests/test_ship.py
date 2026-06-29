from pathlib import Path

import pytest

from hermes_pipeline.ship import (
    ShipSidecar,
    write_sidecar,
    read_sidecar,
    find_ship_sidecar,
    delete_sidecar,
)


def _sidecar(**kw):
    base = dict(
        tick_id="01TICK",
        todo_id=5,
        pr_number=42,
        pr_head_sha="abc123",
        base_branch="main",
        work_branch="todo-5-feature",
        phase_8_task_id="t_8",
        bump_version=None,
    )
    base.update(kw)
    return ShipSidecar(**base)


def test_write_then_read_roundtrip(tmp_path):
    sc = _sidecar()
    path = write_sidecar(sc, state_dir=tmp_path)
    assert path == tmp_path / "outcomes" / "01TICK-ship.json"
    assert path.exists()
    got = read_sidecar(tmp_path, "01TICK")
    assert got == sc


def test_read_missing_returns_none(tmp_path):
    assert read_sidecar(tmp_path, "NOPE") is None


def test_write_is_atomic_no_temp_left(tmp_path):
    write_sidecar(_sidecar(), state_dir=tmp_path)
    leftovers = list((tmp_path / "outcomes").glob("*.tmp"))
    assert leftovers == []


def test_find_by_todo_id(tmp_path):
    write_sidecar(_sidecar(tick_id="01AAA", todo_id=5), state_dir=tmp_path)
    write_sidecar(_sidecar(tick_id="01BBB", todo_id=9), state_dir=tmp_path)
    got = find_ship_sidecar(tmp_path, 9)
    assert got is not None
    assert got.todo_id == 9
    assert got.tick_id == "01BBB"
    assert find_ship_sidecar(tmp_path, 123) is None


def test_delete_sidecar(tmp_path):
    write_sidecar(_sidecar(), state_dir=tmp_path)
    delete_sidecar(tmp_path, "01TICK")
    assert read_sidecar(tmp_path, "01TICK") is None
    # idempotent
    delete_sidecar(tmp_path, "01TICK")
