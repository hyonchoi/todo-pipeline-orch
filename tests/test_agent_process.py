"""Provider-free process ownership and deadline regression tests."""

import os
import sys
import time

import pytest

from hermes_pipeline import agent_process

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="verified Linux identity")


def run(tmp_path, source, **kwargs):
    return agent_process.run_process(
        [sys.executable, "-c", source], cwd=tmp_path, stdin_bytes=b"",
        timeout=2, cleanup_timeout=0.5, **kwargs,
    )


def test_exact_prompt_stdin_and_launch_receipt(tmp_path):
    prompt = b"quotes ' \" $()\\\n\x00" * 20000
    events = []
    result = agent_process.run_process(
        [sys.executable, "-c", "import sys; from pathlib import Path; Path('prompt').write_bytes(sys.stdin.buffer.read())"],
        cwd=tmp_path, stdin_bytes=prompt, timeout=3, cleanup_timeout=0.5,
        on_launch=events.append,
    )
    assert (tmp_path / "prompt").read_bytes() == prompt
    assert result["outcome"] == "exited"
    assert result["exit_code"] == 0
    assert result["cleanup"] == "confirmed"
    assert events[0]["deadline"] > events[0]["launched_monotonic"]
    assert events[0]["identity"]["pid"] > 0


def test_timeout_stays_timed_out_when_term_handler_exits_zero(tmp_path):
    result = agent_process.run_process(
        [sys.executable, "-c", "import signal,time,sys; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(30)"],
        cwd=tmp_path, stdin_bytes=b"", timeout=0.3, cleanup_timeout=0.5,
    )
    assert result["outcome"] == "timed_out"
    assert result["exit_code"] == 0


@pytest.mark.parametrize("mode", ["resistant", "stopped", "grandchild", "detached"])
def test_timeout_cleans_owned_descendants(tmp_path, mode):
    child = (
        "import os,signal,time; "
        + ("os.setsid(); " if mode == "detached" else "")
        + "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        + ("os.kill(os.getpid(), signal.SIGSTOP); " if mode == "stopped" else "")
        + "time.sleep(30)"
    )
    if mode == "grandchild":
        child = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
    source = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
    sightings = []
    result = agent_process.run_process(
        [sys.executable, "-c", source], cwd=tmp_path, stdin_bytes=b"",
        timeout=0.4, cleanup_timeout=0.5, on_processes=sightings.append,
    )
    assert result["outcome"] == "timed_out"
    assert result["cleanup"] == "confirmed"
    assert max(map(len, sightings)) >= (3 if mode == "grandchild" else 2)
    for identity in sightings[-1]:
        current = agent_process.process_snapshot(identity["pid"])
        assert current is None or current["state"] == "Z" or current["start_ticks"] != identity["start_ticks"]


def test_stale_identity_never_signaled(monkeypatch):
    identity = agent_process.process_snapshot(os.getpid())
    identity["start_ticks"] += 1
    monkeypatch.setattr(agent_process, "_pidfd_signal", lambda *_: pytest.fail("stale pid signaled"))
    result = agent_process.cleanup_processes([identity], cleanup_timeout=0.01)
    assert result["cleanup"] == "cleanup_unconfirmed"


def test_unknown_identity_is_not_cleanup_success():
    result = agent_process.cleanup_processes([{"pid": os.getpid()}], cleanup_timeout=0.01)
    assert result["cleanup"] == "cleanup_unconfirmed"


def test_unread_stdin_cannot_block_deadline(tmp_path):
    started = time.monotonic()
    result = agent_process.run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path, stdin_bytes=b"x" * 2_000_000, timeout=0.15, cleanup_timeout=0.5,
    )
    assert result["outcome"] == "timed_out"
    assert time.monotonic() - started < 2


def test_launch_receipt_failure_cleans_before_propagating(tmp_path):
    identity = None

    def fail(event):
        nonlocal identity
        identity = event["identity"]
        raise ValueError("receipt unavailable")

    with pytest.raises(ValueError, match="receipt unavailable"):
        run(tmp_path, "import time; time.sleep(30)", on_launch=fail)
    assert agent_process._live(identity) is False


def test_launch_callback_time_is_charged_to_deadline(tmp_path):
    result = agent_process.run_process(
        [sys.executable, "-c", "pass"], cwd=tmp_path, stdin_bytes=b"",
        timeout=0.05, cleanup_timeout=0.5, on_launch=lambda _: time.sleep(0.1),
    )
    assert result["exit_code"] == 0
    assert result["outcome"] == "timed_out"


def test_old_boot_identity_blocks_cleanup(monkeypatch):
    identity = agent_process.process_snapshot(os.getpid())
    identity["boot_id"] = "old-boot"
    monkeypatch.setattr(agent_process, "_pidfd_signal", lambda *_: pytest.fail("old boot signaled"))
    assert agent_process.cleanup_processes([identity], cleanup_timeout=0.01)["cleanup"] == "cleanup_unconfirmed"


def test_pid_reused_after_fd_open_is_never_signaled(monkeypatch):
    identity = agent_process.process_snapshot(os.getpid())
    replacement = dict(identity, start_ticks=identity["start_ticks"] + 1)
    monkeypatch.setattr(agent_process, "process_snapshot", lambda _: replacement)
    monkeypatch.setattr(agent_process, "_pidfd_signal", lambda *_: pytest.fail("replacement signaled"))
    assert agent_process._signal(identity, 15) is False


def test_unsupported_pidfds_block_before_launch(tmp_path, monkeypatch):
    def unavailable(_):
        raise OSError("unsupported")

    monkeypatch.setattr(agent_process, "_pidfd_open", unavailable)
    monkeypatch.setattr(agent_process.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(agent_process.ProcessLaunchError, match="client_not_launched") as caught:
        run(tmp_path, "pass")
    assert caught.value.cleanup == "confirmed"
    assert caught.value.processes == []


def test_absolute_attempt_deadline_cannot_be_refreshed_by_later_client(tmp_path):
    deadline = time.monotonic() + 0.15
    result = agent_process.run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path,
        stdin_bytes=b"", timeout=30, cleanup_timeout=1, deadline_monotonic=deadline)
    assert result["deadline"] == deadline
    assert result["outcome"] == "timed_out"


def test_expired_shared_deadline_never_launches(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_process.subprocess, "Popen", lambda *a, **k: pytest.fail("expired client launched"))
    with pytest.raises(agent_process.ProcessLaunchError):
        agent_process.run_process([sys.executable, "-c", "pass"], cwd=tmp_path,
                                  stdin_bytes=b"", timeout=1, deadline_monotonic=time.monotonic() - 1)


def test_unobservable_process_scan_never_confirms_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_process, "_discover", lambda _: False)
    result = run(tmp_path, "pass")
    assert result["exit_code"] == 0
    assert result["cleanup"] == "cleanup_unconfirmed"


def test_untracked_session_descendant_after_parent_exit_blocks_cleanup(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    orphan = dict(parent, pid=992, ppid=1, start_ticks=20)
    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", lambda pid: orphan if pid == 992 else None)
    assert agent_process._discover({991: parent}) is False


def test_discovery_ignores_candidate_that_exits_after_owned_session_scan(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    candidate = dict(parent, pid=992, ppid=991, start_ticks=20)
    reads = {991: 0, 992: 0}

    def snapshot(pid):
        reads[pid] += 1
        if pid == 991:
            return parent
        return candidate if reads[pid] == 1 else None

    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/991"), agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    known = {991: parent}
    assert agent_process._discover(known) is True
    assert known == {991: parent}
    assert reads == {991: 2, 992: 2}


def test_disappeared_candidate_older_than_owned_anchor_blocks_discovery(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    candidate = dict(parent, pid=992, ppid=991, start_ticks=5)
    reads = {991: 0, 992: 0}

    def snapshot(pid):
        reads[pid] += 1
        if pid == 991:
            return parent
        return candidate if reads[pid] == 1 else None

    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/991"), agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    assert agent_process._discover({991: parent}) is False


def test_discovery_ignores_candidate_pid_reuse_after_owned_session_scan(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    candidate = dict(parent, pid=992, ppid=991, start_ticks=20)
    replacement = dict(candidate, start_ticks=30, ppid=1, session=999)
    reads = {991: 0, 992: 0}

    def snapshot(pid):
        reads[pid] += 1
        if pid == 991:
            return parent
        return candidate if reads[pid] == 1 else replacement

    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/991"), agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    known = {991: parent}
    assert agent_process._discover(known) is True
    assert known == {991: parent}
    assert reads == {991: 2, 992: 2}


def test_disappeared_candidate_still_requires_live_owned_anchor(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    candidate = dict(parent, pid=992, ppid=991, start_ticks=20)
    reads = {991: 0, 992: 0}

    def snapshot(pid):
        reads[pid] += 1
        if pid == 991:
            return parent if reads[pid] == 1 else None
        return candidate if reads[pid] == 1 else None

    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/991"), agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    assert agent_process._discover({991: parent}) is False


def test_disappeared_detached_session_leader_still_blocks_discovery(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    detached = dict(parent, pid=992, ppid=991, session=992, start_ticks=20)
    reads = {991: 0, 992: 0}

    def snapshot(pid):
        reads[pid] += 1
        if pid == 991:
            return parent
        return detached if reads[pid] == 1 else None

    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/991"), agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    assert agent_process._discover({991: parent}) is False


def test_scan_parent_reuse_cannot_adopt_unrelated_child(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    unrelated = dict(parent, pid=992, ppid=991, session=999, start_ticks=30)
    reads = 0

    def snapshot(pid):
        nonlocal reads
        if pid == 992:
            return unrelated
        reads += 1
        return parent if reads == 1 else dict(parent, start_ticks=20, session=999)

    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/991"), agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    known = {991: parent}
    assert agent_process._discover(known) is False
    assert 992 not in known


def test_child_identity_read_failure_cleans_owned_handle(tmp_path, monkeypatch):
    snapshot = agent_process.process_snapshot
    popen = agent_process.subprocess.Popen
    children = []

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def inaccessible(pid):
        if pid != os.getpid():
            raise PermissionError("identity unavailable")
        return snapshot(pid)

    monkeypatch.setattr(agent_process.subprocess, "Popen", capture)
    monkeypatch.setattr(agent_process, "process_snapshot", inaccessible)
    try:
        with pytest.raises(PermissionError, match="identity unavailable"):
            run(tmp_path, "import time; time.sleep(30)")
        assert children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)


@pytest.mark.parametrize("deny_identity", [False, True])
def test_child_handle_acquisition_failure_records_cleanup(tmp_path, monkeypatch, deny_identity):
    snapshot = agent_process.process_snapshot
    pidfd_open = agent_process._pidfd_open
    popen = agent_process.subprocess.Popen
    children = []
    receipts = []

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def unavailable(pid):
        if pid != os.getpid():
            raise PermissionError("sensitive operating-system detail")
        return pidfd_open(pid)

    def identity(pid):
        if deny_identity and pid != os.getpid():
            raise PermissionError("sensitive identity detail")
        return snapshot(pid)

    monkeypatch.setattr(agent_process.subprocess, "Popen", capture)
    monkeypatch.setattr(agent_process, "_pidfd_open", unavailable)
    monkeypatch.setattr(agent_process, "process_snapshot", identity)
    try:
        with pytest.raises(agent_process.ProcessOwnershipError) as caught:
            run(tmp_path, "import time; time.sleep(30)", on_launch=receipts.append)
        assert caught.value.cleanup == "cleanup_unconfirmed"
        assert "sensitive" not in str(caught.value)
        assert children[0].poll() is not None
        if deny_identity:
            assert not caught.value.processes
            assert not receipts
        else:
            assert children[0].poll() is not None
            assert caught.value.processes[0]["pid"] == children[0].pid
            assert receipts[0]["identity"]["pid"] == children[0].pid
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
