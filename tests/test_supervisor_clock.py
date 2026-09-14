"""Clock injection tests for supervisor module (deterministic, no real time.monotonic/sleep).

These tests verify that injected sleep and now callables work correctly for:
- Lock acquisition retry loops
- Status remaining_s calculation
- Wait loop deadline boundaries
"""

import errno
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from hermes_pipeline import _agent_supervisor as supervisor
from hermes_pipeline.agent_execution import (
    ExecutionStore,
    LockUnconfirmed,
    process_identity,
)


def forbid_module_clock(monkeypatch):
    """Forbid fallback to the real module clock; tests must use injected callables."""
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(
        monotonic=lambda: pytest.fail("module clock fallback used"),
        sleep=lambda s: pytest.fail("module sleep fallback used")
    ))


@pytest.fixture
def execution(tmp_path):
    """Minimal execution fixture for clock tests."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-b", "task", str(worktree)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "--allow-empty", "-m", "test base"], check=True, capture_output=True)
    store = ExecutionStore(tmp_path / "state")
    store.register(
        "execution-1", registration_id="tick-1", plan_identity="a" * 64,
        phase="development", prompt=b"exact\x00prompt\n", client={"name": "codex", "tools": ["Bash"]},
        worktree=str(worktree), branch="task", result_contract={
            "kind": "legacy", "git_metadata": {
                "worktree_git_dir": str(worktree / ".git"), "common_dir": str(worktree / ".git")}}, timeout=30,
    )
    from hermes_pipeline.agent_checkpoint import ProgressJournal
    ProgressJournal(store, "execution-1").initialize()
    return store, worktree


def test_admission_worktree_lock_gives_up_at_deadline_with_injected_clock(execution, monkeypatch):
    """_admission_worktree_lock gives up at the deadline when clock is injected."""
    forbid_module_clock(monkeypatch)
    store, _ = execution
    # Hold the worktree lock with a separate store instance to simulate a competing tick
    competing_lock = ExecutionStore(store.root).worktree_locked("execution-1")
    competing_lock.__enter__()

    # Recorded clock
    clock = [0.0]
    sleep_calls = []

    def recorded_sleep(interval):
        sleep_calls.append(interval)
        clock[0] += interval

    def injected_now():
        return clock[0]

    try:
        with pytest.raises(supervisor._AdmissionBusy):
            with supervisor._admission_worktree_lock(store, "execution-1", sleep=recorded_sleep, now=injected_now):
                pytest.fail("lock was never free")
    finally:
        competing_lock.__exit__(None, None, None)

    # Verify every sleep was 0.5 (the configured retry interval)
    assert all(s == 0.5 for s in sleep_calls)
    # Verify we slept for at least ADMISSION_WORKTREE_RETRY_S total time
    total_slept = sum(sleep_calls)
    assert supervisor.ADMISSION_WORKTREE_RETRY_S <= total_slept <= supervisor.ADMISSION_WORKTREE_RETRY_S + 0.5


def test_admission_worktree_lock_acquires_after_contention_clears(execution, monkeypatch):
    """_admission_worktree_lock acquires after recording sleep releases the competing lock."""
    forbid_module_clock(monkeypatch)
    store, _ = execution
    # Hold the worktree lock
    competing_lock = ExecutionStore(store.root).worktree_locked("execution-1")
    competing_lock.__enter__()

    clock = [0.0]
    sleep_calls = []
    run_count = [0]

    def recorded_sleep(interval):
        sleep_calls.append(interval)
        clock[0] += interval
        # Release the competing lock after first sleep call
        if len(sleep_calls) == 1:
            competing_lock.__exit__(None, None, None)

    def injected_now():
        return clock[0]

    with supervisor._admission_worktree_lock(store, "execution-1", sleep=recorded_sleep, now=injected_now):
        # Inside the context, we should have the lock
        with store.worktree_locked("execution-1"):
            run_count[0] += 1

    assert run_count[0] == 1
    assert len(sleep_calls) == 1  # Only one sleep before lock was released
    assert sleep_calls[0] == 0.5


def test_execution_lock_retrying_gives_up_after_window(execution, monkeypatch):
    """_execution_lock_retrying gives up after ADMISSION_LOCK_RETRY_S with injected clock."""
    forbid_module_clock(monkeypatch)
    store, _ = execution
    # Hold the execution lock
    competing_lock = ExecutionStore(store.root).locked("execution-1")
    competing_lock.__enter__()

    clock = [0.0]
    sleep_calls = []

    def recorded_sleep(interval):
        sleep_calls.append(interval)
        clock[0] += interval

    try:
        with pytest.raises(LockUnconfirmed):
            with supervisor._execution_lock_retrying(store, "execution-1", sleep=recorded_sleep, now=lambda: clock[0]):
                pytest.fail("lock was never free")
    finally:
        competing_lock.__exit__(None, None, None)

    # Verify every sleep was 0.02 and window was traversed
    assert len(sleep_calls) > 1
    assert set(sleep_calls) == {0.02}
    assert supervisor.ADMISSION_LOCK_RETRY_S <= sum(sleep_calls) <= supervisor.ADMISSION_LOCK_RETRY_S + 0.02


def test_execution_lock_retrying_acquires_after_contention_clears(execution, monkeypatch):
    """_execution_lock_retrying acquires after recording sleep releases the competing lock."""
    forbid_module_clock(monkeypatch)
    store, _ = execution
    # Hold the execution lock
    competing_lock = ExecutionStore(store.root).locked("execution-1")
    competing_lock.__enter__()

    clock = [0.0]
    sleep_calls = []
    run_count = [0]

    def recorded_sleep(interval):
        sleep_calls.append(interval)
        clock[0] += interval
        # Release the competing lock after first sleep
        if len(sleep_calls) == 1:
            competing_lock.__exit__(None, None, None)

    def injected_now():
        return clock[0]

    with supervisor._execution_lock_retrying(store, "execution-1", sleep=recorded_sleep, now=injected_now):
        # Inside the context, we should have the lock
        with store.locked("execution-1"):
            run_count[0] += 1

    assert run_count[0] == 1
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 0.02


def test_execution_lock_retrying_non_contention_reraises_immediately(execution, monkeypatch):
    """_execution_lock_retrying re-raises non-contention LockUnconfirmed with zero sleeps."""
    forbid_module_clock(monkeypatch)
    store, _ = execution

    clock = [0.0]
    sleep_calls = []

    def recorded_sleep(interval):
        sleep_calls.append(interval)
        clock[0] += interval

    def injected_now():
        return clock[0]

    # Mock store.locked to raise a non-contention LockUnconfirmed
    original_locked = store.locked
    call_count = [0]

    def mocked_locked(identity):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call raises non-contention error (no EAGAIN/EWOULDBLOCK)
            raise LockUnconfirmed("not_contention") from OSError(errno.EACCES, "permission denied")
        return original_locked(identity)

    monkeypatch.setattr(store, "locked", mocked_locked)

    with pytest.raises(LockUnconfirmed, match="not_contention"):
        with supervisor._execution_lock_retrying(store, "execution-1", sleep=recorded_sleep, now=injected_now):
            pytest.fail("should have raised")

    # No sleeps should have occurred
    assert sleep_calls == []


def test_execution_lock_retrying_body_exception_propagates(execution, monkeypatch):
    """_execution_lock_retrying propagates body exceptions and releases the lock."""
    forbid_module_clock(monkeypatch)
    store, _ = execution

    def recorded_sleep(interval):
        pass

    def injected_now():
        return 0.0

    with pytest.raises(ValueError, match="body error"):
        with supervisor._execution_lock_retrying(store, "execution-1", sleep=recorded_sleep, now=injected_now):
            raise ValueError("body error")

    # After exiting, we should be able to acquire the lock (it was released)
    # Use a separate ExecutionStore instance to avoid re-entrant short-circuit
    with ExecutionStore(store.root).locked("execution-1"):
        pass


def test_status_remaining_s_with_injected_now(execution, monkeypatch):
    """status() calculates remaining_s using injected now callable."""
    forbid_module_clock(monkeypatch)
    store, _ = execution

    # First admit an attempt
    record, _ = store.admit("execution-1")
    generation = record["attempts"][-1]["generation"]

    # Set up an attempt with a known deadline
    deadline_value = 100.0
    store.update_attempt(
        "execution-1", generation,
        status="running",
        supervisor=process_identity(os.getpid()),
        deadline_monotonic=deadline_value
    )

    # Test with different now values
    def injected_now_t30():
        return 30.0

    report = supervisor.status(store, "execution-1", now=injected_now_t30)
    # remaining_s should be deadline_value - 30.0 = 70.0
    assert report["remaining_s"] == 70.0

    def injected_now_t80():
        return 80.0

    report = supervisor.status(store, "execution-1", now=injected_now_t80)
    # remaining_s should be deadline_value - 80.0 = 20.0
    assert report["remaining_s"] == 20.0


def test_status_remaining_s_none_when_not_alive(execution, monkeypatch):
    """status() returns remaining_s=None when attempt is not alive (now must not be called)."""
    forbid_module_clock(monkeypatch)
    store, _ = execution

    # First admit an attempt
    record, _ = store.admit("execution-1")
    generation = record["attempts"][-1]["generation"]

    # Set up a terminal attempt (not alive)
    store.update_attempt(
        "execution-1", generation,
        status="exited",
        exit_code=0,
        cleanup="confirmed",
        deadline_monotonic=100.0
    )

    report = supervisor.status(store, "execution-1", now=lambda: pytest.fail("now must not be consulted for terminal attempt"))
    assert report["remaining_s"] is None


def test_main_wait_respects_injected_clock(execution, monkeypatch, capsys):
    """main() with --wait uses injected sleep and now to drive the wait loop."""
    forbid_module_clock(monkeypatch)
    store, _ = execution

    # Set up a running attempt with known deadline
    store.admit("execution-1")
    store.update_attempt("execution-1", 1, status="running", supervisor=process_identity(os.getpid()),
                         deadline_monotonic=200.0)

    # Recorded clock for deterministic wait loop
    clock = [0.0]
    sleep_calls = []

    def recorded_sleep(interval):
        sleep_calls.append(interval)
        clock[0] += interval
        # At 50s elapsed, terminate the attempt to drive the loop to completion
        if clock[0] >= 50 and store.load("execution-1")["attempts"][-1]["status"] == "running":
            store.update_attempt("execution-1", 1, status="timed_out", cleanup="confirmed")

    def injected_now():
        return clock[0]

    # Run main with --wait and injected clock
    result = supervisor.main(
        ["run", "--wait", "--root", str(store.root), "--execution", "execution-1"],
        sleep=recorded_sleep,
        now=injected_now
    )

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    final = lines[-1]

    # Verify the wait loop was entered and progressed with injected sleep
    assert len(sleep_calls) > 0, "injected sleep must be called"
    assert all(s <= 0.1 for s in sleep_calls), "all sleep intervals must be <= 0.1"
    # Loop terminates near the deadline (started + timeout + ceiling_tail, or earlier if terminal)
    assert clock[0] >= 50, "clock must advance at least to attempt termination point"
    # Verify final report shows the terminal state
    assert final["final"] is True
    assert final["status"] == "timed_out"
    assert result == 1
