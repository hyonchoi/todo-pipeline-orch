import json
import os
import subprocess
import sys

import pytest

from hermes_pipeline.agent_execution import (
    ExecutionError,
    ExecutionStore,
    LockUnconfirmed,
    identity_matches,
    process_identity,
)


@pytest.fixture
def execution(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = ExecutionStore(tmp_path / "state")
    arguments = dict(
        registration_id="registration-1", plan_identity="a" * 64, phase="development",
        prompt=b"exact\x00prompt\n", client={"name": "codex", "tools": ["Bash"]},
        worktree=str(worktree), branch="task", result_contract={"path": "result.json"},
        timeout=30,
    )
    store.register("execution-1", **arguments)
    return store, arguments


def test_pins_bytes_and_rejects_registration_drift(execution):
    store, arguments = execution
    assert store.prompt("execution-1") == arguments["prompt"]
    assert store.register("execution-1", **arguments) == store.load("execution-1")
    with pytest.raises(ExecutionError):
        store.register("execution-1", **{**arguments, "branch": "different"})


def test_reentry_does_not_restart_or_refresh_budget(execution):
    store, _ = execution
    first, created = store.admit("execution-1")
    assert created
    store.update_attempt("execution-1", 1, status="running", deadline_monotonic=123.0)
    second, created = store.admit("execution-1")
    assert not created
    assert second["attempts"][0]["deadline_monotonic"] == 123.0
    assert first["attempts"][0]["generation"] == 1


def test_retry_requires_durable_approval_and_confirmed_cleanup(execution):
    store, _ = execution
    store.admit("execution-1")
    store.update_attempt("execution-1", 1, status="timed_out", cleanup="unconfirmed")
    with pytest.raises(ExecutionError):
        store.admit("execution-1", recovery_event="operator-event")
    store.authorize_retry("execution-1", expected_generation=1, event_id="operator-event")
    with pytest.raises(ExecutionError):
        store.admit("execution-1", recovery_event="operator-event")
    store.update_attempt("execution-1", 1, cleanup="confirmed")
    retried, created = store.admit("execution-1", recovery_event="operator-event")
    assert created and len(retried["attempts"]) == 2
    with pytest.raises(ExecutionError):
        store.update_attempt("execution-1", 1, exit_code=0)


def test_deadline_terminal_outcome_cannot_be_changed(execution):
    store, _ = execution
    store.admit("execution-1")
    store.update_attempt("execution-1", 1, status="timed_out")
    store.update_attempt("execution-1", 1, exit_code=0, cleanup="confirmed")
    with pytest.raises(ExecutionError):
        store.update_attempt("execution-1", 1, status="exited")
    assert store.load("execution-1")["attempts"][0]["status"] == "timed_out"


def test_lock_is_kernel_held_and_reentrant(execution):
    store, _ = execution
    other = ExecutionStore(store.root)
    with store.locked("execution-1"):
        store.admit("execution-1")
        with pytest.raises(LockUnconfirmed):
            with other.locked("execution-1"):
                pytest.fail("second owner acquired lock")
    with other.locked("execution-1"):
        pass


def test_schema_symlink_and_worktree_containment_fail_closed(execution, tmp_path):
    store, arguments = execution
    for invalid in ("../escape", "with/slash", ""):
        with pytest.raises(ExecutionError):
            store.load(invalid)
    with pytest.raises(ExecutionError):
        ExecutionStore(tmp_path / "worktree" / "state").register("bad", **arguments)
    record_path = store.root / "execution-1" / "record.json"
    record = json.loads(record_path.read_text())
    record["unknown"] = "unsafe"
    record_path.write_text(json.dumps(record))
    with pytest.raises(ExecutionError):
        store.load("execution-1")
    record_path.unlink()
    target = tmp_path / "target"
    target.write_text("{}")
    record_path.symlink_to(target)
    with pytest.raises(ExecutionError):
        store.load("execution-1")


def test_process_identity_rejects_reused_pid_and_other_boot():
    identity = process_identity(os.getpid())
    assert identity_matches(identity)
    assert not identity_matches({**identity, "start_ticks": "impossible"})
    assert not identity_matches({**identity, "boot_id": "previous-boot"})


def test_atomic_failure_preserves_previous_record(execution, monkeypatch):
    store, _ = execution
    before = store.load("execution-1")
    def fail_replace(*args, **kwargs):
        raise OSError("simulated interruption")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.admit("execution-1")
    assert store.load("execution-1") == before


def test_unresolved_attempt_blocks_other_identity_in_same_worktree(execution):
    store, arguments = execution
    store.register("execution-2", **arguments)
    store.admit("execution-1")
    with pytest.raises(ExecutionError):
        store.admit("execution-2")
    store.update_attempt("execution-1", 1, status="interrupted", cleanup="confirmed")
    assert store.admit("execution-2")[1]


def test_cross_identity_worktree_lock_is_kernel_held(execution):
    store, arguments = execution
    store.register("execution-2", **arguments)
    other = ExecutionStore(store.root)
    with store.worktree_locked("execution-1"):
        with pytest.raises(LockUnconfirmed):
            other.admit("execution-2")


@pytest.mark.parametrize("changes", [
    {"supervisor": {"pid": 1}}, {"started_monotonic": float("inf")},
    {"exit_code": True}, {"owned_processes": ["unknown"]},
])
def test_rejects_malformed_attempt_evidence(execution, changes):
    store, _ = execution
    store.admit("execution-1")
    with pytest.raises(ExecutionError):
        store.update_attempt("execution-1", 1, **changes)


def test_collected_exit_and_launch_budget_are_immutable(execution):
    store, _ = execution
    store.admit("execution-1")
    store.update_attempt("execution-1", 1, deadline_monotonic=123, exit_code=1)
    with pytest.raises(ExecutionError):
        store.update_attempt("execution-1", 1, exit_code=0)
    with pytest.raises(ExecutionError):
        store.update_attempt("execution-1", 1, deadline_monotonic=999)


def test_process_module_snapshot_is_accepted_as_receipt(execution):
    from hermes_pipeline.agent_process import process_snapshot
    store, _ = execution
    store.admit("execution-1")
    snapshot = process_snapshot(os.getpid())
    store.update_attempt("execution-1", 1, client_process=snapshot)
    assert identity_matches(snapshot)


def test_replacing_owner_filename_cannot_split_lock_ownership(execution):
    store, _ = execution
    other = ExecutionStore(store.root)
    owner_file = store.root / "execution-1" / "owner.lock"
    with store.locked("execution-1"):
        owner_file.unlink(missing_ok=True)
        owner_file.touch()
        with pytest.raises(LockUnconfirmed):
            with other.locked("execution-1"):
                pytest.fail("replacement split lock ownership")


def test_fifo_record_read_is_bounded(execution):
    store, _ = execution
    record = store.root / "execution-1" / "record.json"
    record.unlink()
    os.mkfifo(record)
    script = "from hermes_pipeline.agent_execution import ExecutionStore; import sys; ExecutionStore(sys.argv[1]).load('execution-1')"
    child = subprocess.Popen([sys.executable, "-c", script, str(store.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert child.wait(timeout=2) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.parametrize("changes", [
    {"status": "exited", "exit_code": 0, "exit_signal": 9},
    {"started_monotonic": 20, "deadline_monotonic": 10},
    {"status": "exited"},
])
def test_rejects_contradictory_collected_evidence(execution, changes):
    store, _ = execution
    store.admit("execution-1")
    with pytest.raises(ExecutionError):
        store.update_attempt("execution-1", 1, **changes)


@pytest.mark.parametrize("new_root", [False, True])
def test_registration_requires_durable_parent_directory_entries(execution, tmp_path, monkeypatch, new_root):
    store, arguments = execution
    if new_root:
        store = ExecutionStore(tmp_path / "new-state")
    parent = tmp_path if new_root else store.root
    parent_stat = parent.stat()
    original_fsync = os.fsync
    def reject_parent_sync(descriptor):
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) == (parent_stat.st_dev, parent_stat.st_ino):
            raise OSError("injected parent directory fsync failure")
        original_fsync(descriptor)
    monkeypatch.setattr(os, "fsync", reject_parent_sync)
    for _ in range(2):
        with pytest.raises(ExecutionError):
            store.register("new-execution", **arguments)
    assert not (store.root / "new-execution" / "record.json").exists()


def test_cgroup_receipts_persist_and_cannot_be_dropped(execution):
    store, _ = execution
    store.admit('execution-1')
    receipt = dict(version=1, path='/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/tpo-' + 'a' * 32 + '.scope',
                   unit='tpo-' + 'a' * 32 + '.scope', device=1, inode=42,
                   host='host', boot_id='boot')
    store.update_attempt('execution-1', 1, owned_cgroups=[receipt])
    assert store.load('execution-1')['attempts'][0]['owned_cgroups'] == [receipt]
    with pytest.raises(ExecutionError):
        store.update_attempt('execution-1', 1, owned_cgroups=[])


def test_legacy_record_upgrades_without_inventing_cgroup_evidence(execution):
    store, _ = execution
    record, _ = store.admit('execution-1')
    record['version'] = 1
    record['attempts'][0].pop('owned_cgroups', None)
    path = store.root / 'execution-1' / 'record.json'
    path.write_text(json.dumps(record))
    upgraded = store.load('execution-1')
    assert upgraded['version'] == 2
    assert upgraded['attempts'][0]['owned_cgroups'] == []
    assert json.loads(path.read_text())['version'] == 1  # reads do not rewrite


@pytest.mark.parametrize('change', [dict(path='/sys/fs/cgroup'), dict(inode=True),
                                   dict(version=99), dict(path='/sys/fs/cgroup/../other'),
                                   dict(unknown='field')])
def test_cgroup_receipt_schema_rejects_unsafe_evidence(execution, change):
    store, _ = execution
    store.admit('execution-1')
    receipt = dict(version=1, path='/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/tpo-' + 'a' * 32 + '.scope',
                   unit='tpo-' + 'a' * 32 + '.scope', device=1, inode=42,
                   host='host', boot_id='boot')
    with pytest.raises(ExecutionError):
        store.update_attempt('execution-1', 1, owned_cgroups=[{**receipt, **change}])
