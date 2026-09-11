"""Provider-free process ownership and deadline regression tests."""

import os
import sys
import time
from types import SimpleNamespace

import pytest

from hermes_pipeline import agent_process

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="verified Linux identity")


@pytest.fixture(autouse=True)
def legacy_process_backend(monkeypatch):
    """Retain process-tree backend coverage used by legacy recovery and Darwin."""
    monkeypatch.setattr(agent_process, "sys", SimpleNamespace(platform="legacy"))


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


def test_run_latches_ownership_ambiguity_after_later_clean_inventory(tmp_path, monkeypatch):
    discover = agent_process._discover
    scans = 0

    def ambiguous_once(known):
        nonlocal scans
        scans += 1
        if scans == 1:
            return agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS
        return discover(known)

    monkeypatch.setattr(agent_process, "_discover", ambiguous_once)
    result = run(tmp_path, "import time; time.sleep(0.05)")
    assert scans > 1
    assert result["exit_code"] == 0
    assert result["cleanup"] == "cleanup_unconfirmed"


def test_run_does_not_discover_again_after_observing_client_exit(tmp_path, monkeypatch):
    dead_scans = 0
    cleanup_calls = 0
    exit_observed = False
    poll = agent_process.subprocess.Popen.poll

    def observed_poll(process):
        nonlocal exit_observed
        result = poll(process)
        if result is not None:
            exit_observed = True
        return result

    def discover(known):
        nonlocal dead_scans
        if not exit_observed:
            return agent_process.DiscoveryOutcome.CLEAN
        dead_scans += 1
        return agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS

    def cleanup(identities, **kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return {"cleanup": "confirmed", "processes": list(identities)}

    monkeypatch.setattr(agent_process.subprocess.Popen, "poll", observed_poll)
    monkeypatch.setattr(agent_process, "_discover", discover)
    monkeypatch.setattr(agent_process, "cleanup_processes", cleanup)
    result = run(tmp_path, "import time; time.sleep(0.05)")
    assert dead_scans == 0
    assert cleanup_calls == 1
    assert result["exit_code"] == 0
    assert result["cleanup"] == "confirmed"


def test_linux_snapshot_treats_esrch_as_disappearance(monkeypatch):
    read_text = agent_process.Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/991/stat":
            raise ProcessLookupError("process exited during stat read")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(agent_process.Path, "read_text", read)
    assert agent_process.process_snapshot(991) is None


@pytest.mark.parametrize("failure", ["enumeration", "disappearance", "read"])
def test_cleanup_retries_unrelated_scan_churn_until_clean(monkeypatch, failure):
    local = agent_process.process_snapshot(os.getpid())
    dead = dict(local, pid=991, session=991, state="Z")
    scans = 0
    elapsed = [0.0]

    def pids(_):
        nonlocal scans
        scans += 1
        if failure == "enumeration" and scans <= 3:
            raise FileNotFoundError("process list changed")
        return iter([agent_process.Path("/proc/992")]) if scans <= 3 else iter([])

    def snapshot(pid):
        if pid == 992:
            if failure == "read":
                raise PermissionError("unrelated process unreadable")
            return None
        return dead if pid == 991 else local

    monkeypatch.setattr(agent_process.Path, "iterdir", pids)
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    monkeypatch.setattr(agent_process.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(agent_process.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    monkeypatch.setattr(agent_process, "_signal", lambda *_: pytest.fail("dead or unrelated process signaled"))
    result = agent_process.cleanup_processes([dead], cleanup_timeout=1)
    assert result["cleanup"] == "confirmed"
    assert scans == 4
    assert elapsed[0] < 1


@pytest.mark.parametrize("cleanup_timeout", [0, 0.1, 5])
def test_permanent_scan_churn_has_bounded_retry_window(monkeypatch, cleanup_timeout):
    local = agent_process.process_snapshot(os.getpid())
    dead = dict(local, pid=991, state="Z")
    elapsed = [0.0]
    scans = 0

    def unavailable(_):
        nonlocal scans
        scans += 1
        raise PermissionError("process enumeration unavailable")

    monkeypatch.setattr(agent_process.Path, "iterdir", unavailable)
    monkeypatch.setattr(agent_process, "process_snapshot", lambda pid: dead if pid == 991 else local)
    monkeypatch.setattr(agent_process.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(agent_process.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    result = agent_process.cleanup_processes([dead], cleanup_timeout=cleanup_timeout)
    assert result["cleanup"] == "cleanup_unconfirmed"
    assert elapsed[0] == pytest.approx(min(2, cleanup_timeout))
    if cleanup_timeout == 0:
        assert scans == 1
    else:
        assert scans > 1


@pytest.mark.parametrize("cleanup_timeout", [0, 0.1, 5])
@pytest.mark.parametrize("pending", ["transient", "living"])
@pytest.mark.parametrize("late", ["start", "finish"])
def test_cleanup_rejects_retry_at_deadline(monkeypatch, cleanup_timeout, pending, late):
    local = agent_process.process_snapshot(os.getpid())
    owned = dict(local, pid=991, session=991)
    elapsed = [0.0]
    scan_starts = []
    retry_limit = min(2, cleanup_timeout) if pending == "transient" else cleanup_timeout

    def pids(_):
        scan_starts.append(elapsed[0])
        # A retry that could report success either starts too late or starts on
        # time but finishes too late. Neither observation can confirm cleanup.
        if late == "start":
            elapsed[0] += max(0.01, retry_limit - 0.01)
        elif len(scan_starts) == 1:
            elapsed[0] += 0.01
        else:
            elapsed[0] = retry_limit + 0.01
        if pending == "transient" and len(scan_starts) == 1:
            raise PermissionError("host scan unavailable")
        return iter([])

    def snapshot(pid):
        if pid != 991:
            return local
        state = "S" if pending == "living" and len(scan_starts) == 1 else "Z"
        return dict(owned, state=state)

    monkeypatch.setattr(agent_process.Path, "iterdir", pids)
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    monkeypatch.setattr(agent_process, "_signal", lambda *_: True)
    monkeypatch.setattr(agent_process.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(agent_process.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    result = agent_process.cleanup_processes([owned], cleanup_timeout=cleanup_timeout)
    assert result["cleanup"] == "cleanup_unconfirmed"
    if late == "start" or cleanup_timeout == 0:
        assert scan_starts == [0.0]
        assert elapsed[0] == pytest.approx(retry_limit if cleanup_timeout else 0.01)
    else:
        assert scan_starts == pytest.approx([0.0, 0.03])
        assert elapsed[0] == pytest.approx(retry_limit + 0.01)


def test_run_does_not_latch_transient_inventory_failure(tmp_path, monkeypatch):
    iterdir = agent_process.Path.iterdir
    scans = 0

    def pids(path):
        nonlocal scans
        scans += 1
        if scans <= 2:
            raise FileNotFoundError("unrelated host process churn")
        return iterdir(path)

    monkeypatch.setattr(agent_process.Path, "iterdir", pids)
    result = run(tmp_path, "import time; time.sleep(0.05)")
    assert scans > 2
    assert result["exit_code"] == 0
    assert result["cleanup"] == "confirmed"


@pytest.mark.parametrize("ambiguity", ["candidate_read", "anchor_reused", "relation", "chronology", "known_read", "known_reused"])
def test_ownership_ambiguity_survives_later_clean_scan(monkeypatch, ambiguity):
    local = agent_process.process_snapshot(os.getpid())
    parent = dict(local, pid=991, start_ticks=10, session=991, ppid=1, state="S")
    candidate = dict(parent, pid=992, start_ticks=20, ppid=991)
    scans = 0
    reads = {}
    elapsed = [0.0]
    signaled = []

    def pids(_):
        nonlocal scans
        scans += 1
        # Include an unrelated unreadable PID, ensuring transient errors do not
        # mask an ownership failure elsewhere in the same scan.
        return iter(agent_process.Path(f"/proc/{pid}") for pid in ([991, 992, 993] if scans == 1 else [991]))

    def snapshot(pid):
        reads[pid] = reads.get(pid, 0) + 1
        if pid == 993:
            raise PermissionError("unrelated process")
        if pid == 992:
            if reads[pid] == 1:
                return candidate
            if ambiguity == "candidate_read":
                raise PermissionError("related candidate")
            if ambiguity == "relation":
                return dict(candidate, ppid=1)
            if ambiguity == "chronology":
                return dict(candidate, start_ticks=5)
            return candidate
        if pid == 991:
            if scans > 1:
                return dict(parent, state="Z")
            if ambiguity == "known_read" and reads[pid] == 1:
                raise PermissionError("known identity")
            if ((ambiguity == "known_reused" and reads[pid] == 1)
                    or (ambiguity == "anchor_reused" and reads[pid] == 2)):
                return dict(parent, start_ticks=30)
            return parent
        return local

    # Chronology must already be invalid in the original observation: a safely
    # revalidated anchor can otherwise resolve ordinary member PID reuse.
    if ambiguity == "chronology":
        candidate["start_ticks"] = 5
    monkeypatch.setattr(agent_process.Path, "iterdir", pids)
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    monkeypatch.setattr(agent_process, "_signal", lambda identity, sig: signaled.append(identity["pid"]) or True)
    monkeypatch.setattr(agent_process.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(agent_process.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    result = agent_process.cleanup_processes([parent], cleanup_timeout=1)
    assert scans == 2
    assert result["cleanup"] == "cleanup_unconfirmed"
    assert result["processes"] == [parent]
    assert signaled and set(signaled) == {991}


@pytest.mark.parametrize("state", ["unreadable", "reused", "incomplete"])
def test_unverifiable_known_identity_is_never_signaled(monkeypatch, state):
    local = agent_process.process_snapshot(os.getpid())
    known = dict(local, pid=991, session=991, state="S")

    def snapshot(pid):
        if pid != 991:
            return local
        if state == "unreadable":
            raise PermissionError("known identity unreadable")
        return dict(known, start_ticks=known["start_ticks"] + 1)

    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([]))
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    monkeypatch.setattr(agent_process, "_signal", lambda *_: pytest.fail("unverifiable identity signaled"))
    identity = {"pid": 991} if state == "incomplete" else known
    result = agent_process.cleanup_processes([identity], cleanup_timeout=0)
    assert result["cleanup"] == "cleanup_unconfirmed"


def test_untracked_session_descendant_after_parent_exit_blocks_cleanup(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    orphan = dict(parent, pid=992, ppid=1, start_ticks=20)
    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", lambda pid: orphan if pid == 992 else None)
    assert agent_process._discover({991: parent}) is agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS


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
    assert agent_process._discover(known) is agent_process.DiscoveryOutcome.CLEAN
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
    assert agent_process._discover({991: parent}) is agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS


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
    assert agent_process._discover(known) is agent_process.DiscoveryOutcome.CLEAN
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
    assert agent_process._discover({991: parent}) is agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS


def test_disappeared_detached_session_leader_is_retained_as_owned_history(monkeypatch):
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
    known = {991: parent}
    assert agent_process._discover(known) is agent_process.DiscoveryOutcome.CLEAN
    assert known == {991: parent, 992: detached}
    assert reads == {991: 2, 992: 2}


def test_vanished_detached_leader_with_same_scan_orphan_blocks_cleanup(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    detached = dict(parent, pid=992, ppid=991, session=992, start_ticks=20)
    orphan = dict(parent, pid=993, ppid=1, session=992, start_ticks=30)

    def install_scan():
        reads = {991: 0, 992: 0, 993: 0}
        def snapshot(pid):
            if pid not in reads:
                return dict(parent, pid=pid)
            reads[pid] += 1
            if pid == 991:
                return parent
            if pid == 992:
                return detached if reads[pid] == 1 else None
            return orphan
        monkeypatch.setattr(
            agent_process.Path, "iterdir",
            lambda _: iter(agent_process.Path(f"/proc/{pid}") for pid in reads))
        monkeypatch.setattr(agent_process, "process_snapshot", snapshot)

    install_scan()
    known = {991: parent}
    assert agent_process._discover(known) is agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS
    assert known == {991: parent, 992: detached}

    install_scan()
    signaled = []
    monkeypatch.setattr(agent_process, "_signal", lambda identity, sig: signaled.append(identity["pid"]) or True)
    result = agent_process.cleanup_processes([parent], cleanup_timeout=0)
    assert result["cleanup"] == "cleanup_unconfirmed"
    assert {identity["pid"] for identity in result["processes"]} == {991, 992}
    assert 993 not in signaled


def test_historical_detached_session_rejects_later_unknown_orphan(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    detached = dict(parent, pid=992, ppid=991, session=992, start_ticks=20)
    orphan = dict(parent, pid=993, ppid=1, session=992, start_ticks=30)
    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/991"), agent_process.Path("/proc/993")]))
    monkeypatch.setattr(
        agent_process, "process_snapshot",
        lambda pid: parent if pid == 991 else orphan if pid == 993 else None)
    assert agent_process._discover({991: parent, 992: detached}) is agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS


def test_reused_detached_session_leader_pid_blocks_discovery(monkeypatch):
    parent = dict(pid=991, start_ticks=10, host="h", boot_id="b", session=991, ppid=1, state="S")
    detached = dict(parent, pid=992, ppid=991, session=992, start_ticks=20)
    replacement = dict(detached, start_ticks=30, session=999, ppid=1)
    reads = {991: 0, 992: 0}

    def snapshot(pid):
        reads[pid] += 1
        if pid == 991:
            return parent
        return detached if reads[pid] == 1 else replacement

    monkeypatch.setattr(agent_process.Path, "iterdir", lambda _: iter([agent_process.Path("/proc/991"), agent_process.Path("/proc/992")]))
    monkeypatch.setattr(agent_process, "process_snapshot", snapshot)
    assert agent_process._discover({991: parent}) is agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS


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
    assert agent_process._discover(known) is agent_process.DiscoveryOutcome.OWNERSHIP_AMBIGUOUS
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
