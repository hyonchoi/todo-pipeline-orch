"""Provider-free process ownership and deadline regression tests."""

import os
import sys
import time
from pathlib import Path

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


def test_linux_snapshot_treats_esrch_as_disappearance(monkeypatch):
    read_text = agent_process.Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/991/stat":
            raise ProcessLookupError("process exited during stat read")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(agent_process.Path, "read_text", read)
    assert agent_process.process_snapshot(991) is None


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


@pytest.mark.parametrize("exits", [True, False])
def test_only_direct_root_owned_when_descendant_survives(tmp_path, monkeypatch, exits):
    import signal
    from pathlib import Path

    monkeypatch.setattr(Path, "iterdir", lambda *_: pytest.fail("host process inventory forbidden"))
    source = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "Path('descendant.pid').write_text(str(p.pid)); "
        + ("sys.exit(0)" if exits else "time.sleep(30)")
    )
    descendant = None
    try:
        result = agent_process.run_process([sys.executable, '-c', source], cwd=tmp_path,
                                          stdin_bytes=b'', timeout=0.3, cleanup_timeout=0.5)
        descendant = agent_process.process_snapshot(int((tmp_path / 'descendant.pid').read_text()))
        assert descendant is not None and agent_process._live(descendant)
        assert result['cleanup'] == 'confirmed'
        assert result['outcome'] == ('exited' if exits else 'timed_out')
        assert len(result['processes']) == 1
        assert result['processes'][0]['pid'] != descendant['pid']
    finally:
        if descendant is None and (tmp_path / 'descendant.pid').exists():
            descendant = agent_process.process_snapshot(int((tmp_path / 'descendant.pid').read_text()))
        if descendant is not None:
            agent_process._signal(descendant, signal.SIGKILL)
            stop = time.monotonic() + 2
            while agent_process._live(descendant) and time.monotonic() < stop:
                time.sleep(0.01)
            assert agent_process._live(descendant) is False


def test_exited_unreaped_root_has_durable_identity(tmp_path, monkeypatch):
    snapshot = agent_process.process_snapshot
    def delayed(pid):
        if pid != os.getpid():
            time.sleep(0.1)
        return snapshot(pid)
    monkeypatch.setattr(agent_process, 'process_snapshot', delayed)
    receipts = []
    result = run(tmp_path, 'pass', on_launch=receipts.append)
    assert result['exit_code'] == 0
    assert result['cleanup'] == 'confirmed'
    assert receipts[0]['identity']['start_ticks'] > 0


@pytest.mark.parametrize('source', [
    'import os,signal,time; os.kill(os.getpid(), signal.SIGSTOP); time.sleep(30)',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)',
])
def test_direct_stopped_and_resistant_root_terminated(tmp_path, source):
    result = agent_process.run_process([sys.executable, '-c', source], cwd=tmp_path,
                                      stdin_bytes=b'', timeout=0.2, cleanup_timeout=0.5)
    assert result['outcome'] == 'timed_out'
    assert result['cleanup'] == 'confirmed'
    assert result['exit_code'] is not None


def test_empty_root_inventory_is_not_cleanup_proof():
    assert agent_process.cleanup_processes([], cleanup_timeout=0)['cleanup'] == 'cleanup_unconfirmed'


def test_default_output_is_discarded(tmp_path):
    """Default behavior: stdout/stderr go to DEVNULL."""
    if not os.path.exists("/proc/self/fd"):
        pytest.skip("/proc/self/fd unavailable")
    readlink_script = "import os,sys; open(sys.argv[1],'w').write(os.readlink('/proc/self/fd/1')+'\\n'+os.readlink('/proc/self/fd/2'))"
    output_path = tmp_path / "fds.txt"
    result = agent_process.run_process(
        [sys.executable, "-c", readlink_script, str(output_path)],
        cwd=tmp_path, stdin_bytes=b"", timeout=2, cleanup_timeout=0.5,
    )
    assert result["outcome"] == "exited"
    assert result["exit_code"] == 0
    content = output_path.read_text().strip().split("\n")
    assert content == ["/dev/null", "/dev/null"]


def test_stderr_only_capture_and_partial_open_rollback(tmp_path):
    """stderr_path only -> stderr captured, stdout /dev/null; symlink on stderr -> ProcessLaunchError with no fd leak."""
    if not os.path.exists("/proc/self/fd"):
        pytest.skip("/proc/self/fd unavailable")
    readlink_script = "import os,sys; open(sys.argv[1],'w').write(os.readlink('/proc/self/fd/1')+'\\n'+os.readlink('/proc/self/fd/2'))"
    output_path = tmp_path / "fds.txt"
    stderr_path = tmp_path / "stderr.txt"
    result = agent_process.run_process(
        [sys.executable, "-c", readlink_script, str(output_path)],
        cwd=tmp_path, stdin_bytes=b"", timeout=2, cleanup_timeout=0.5,
        stderr_path=stderr_path,
    )
    assert result["outcome"] == "exited"
    assert result["exit_code"] == 0
    content = output_path.read_text().strip().split("\n")
    assert content[0] == "/dev/null"
    assert "stderr.txt" in content[1]

    fd_count_before = len(os.listdir("/proc/self/fd"))
    stderr_symlink = tmp_path / "stderr_link"
    target = tmp_path / "target"
    target.write_text("x")
    stderr_symlink.symlink_to(target)
    with pytest.raises(agent_process.ProcessLaunchError) as exc_info:
        agent_process.run_process(
            [sys.executable, "-c", "print('x')"],
            cwd=tmp_path, stdin_bytes=b"", timeout=2, cleanup_timeout=0.5,
            stderr_path=stderr_symlink,
        )
    assert exc_info.value.cleanup == "confirmed"
    assert exc_info.value.processes == []
    fd_count_after = len(os.listdir("/proc/self/fd"))
    assert fd_count_before == fd_count_after



@pytest.mark.parametrize("path_param", ["stdout_path", "stderr_path"])
def test_output_path_symlink_refused(tmp_path, path_param):
    """symlink as stdout_path or stderr_path raises ProcessLaunchError."""
    link_path = tmp_path / f"{path_param}.txt"
    target = tmp_path / "target.txt"
    target.write_text("existing")
    link_path.symlink_to(target)

    with pytest.raises(agent_process.ProcessLaunchError):
        agent_process.run_process(
            [sys.executable, "-c", "print('out')"],
            cwd=tmp_path, stdin_bytes=b"", timeout=2, cleanup_timeout=0.5,
            **{path_param: link_path},
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo unavailable")
def test_output_path_must_be_regular_file(tmp_path):
    """A pre-planted FIFO must fail the launch promptly instead of blocking it."""
    fifo = tmp_path / "stdout.log"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(agent_process.ProcessLaunchError):
        agent_process.run_process(
            [sys.executable, "-c", "print('out')"],
            cwd=tmp_path, stdin_bytes=b"", timeout=2, cleanup_timeout=0.5,
            stdout_path=fifo,
        )
    assert time.monotonic() - started < 1.0


def test_output_path_rejects_character_device(tmp_path):
    """The regular-file check fires even when the open itself succeeds."""
    with pytest.raises(agent_process.ProcessLaunchError):
        agent_process.run_process(
            [sys.executable, "-c", "print('out')"],
            cwd=tmp_path, stdin_bytes=b"", timeout=2, cleanup_timeout=0.5,
            stdout_path=Path("/dev/null"),
        )


def test_output_capture_appends_to_existing_file(tmp_path):
    out = tmp_path / "stdout.log"
    out.write_text("earlier\n")
    result = agent_process.run_process(
        [sys.executable, "-c", "print('later')"],
        cwd=tmp_path, stdin_bytes=b"", timeout=10, cleanup_timeout=1,
        stdout_path=out,
    )
    assert result["outcome"] == "exited"
    assert out.read_text() == "earlier\nlater\n"
