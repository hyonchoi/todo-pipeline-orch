"""Provider-free tests of the registered, deterministic supervisor boundary."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_pipeline import _agent_supervisor as supervisor
from hermes_pipeline.agent_execution import ExecutionError, ExecutionStore


@pytest.fixture(autouse=True)
def portable_process_backend(monkeypatch):
    """Exercise supervisor contracts without requiring a Linux user manager.

    Native cgroup launch/cleanup is covered separately in test_agent_cgroup.
    Keep the real legacy process implementation for these provider-free tests.
    """
    from types import SimpleNamespace

    from hermes_pipeline import agent_process

    if sys.platform == 'linux':
        monkeypatch.setattr(agent_process, 'sys', SimpleNamespace(platform='legacy'))


@pytest.mark.parametrize('failure', ['timeout', 'supervisor_loss', 'loss_after_timeout_receipt'])
def test_collection_preserves_primary_exit_and_owns_attempt_outcome(execution, monkeypatch, failure):
    from hermes_pipeline import agent_collector as collector
    store, worktree = execution
    monkeypatch.setattr(supervisor, 'validate_registration', lambda *args: None)
    monkeypatch.setattr(supervisor, 'client_argv', lambda *args, **kwargs: [sys.executable, '-c', 'pass'])
    deadline = time.monotonic() + 20
    monkeypatch.setattr(supervisor, 'run_process', lambda *args, **kwargs:
                        {'outcome': 'exited', 'exit_code': 0, 'signal': None, 'cleanup': 'confirmed',
                         'processes': [], 'deadline': deadline})

    def collect(*args, **kwargs):
        assert store.load('execution-1')['attempts'][-1]['exit_code'] == 0
        if failure == 'supervisor_loss':
            raise SystemExit()
        if failure == 'loss_after_timeout_receipt':
            collector._record_collector_exit(store, 'execution-1', 1,
                {'outcome': 'timed_out', 'exit_code': -9, 'signal': 9, 'cleanup': 'confirmed'}, 'review', None)
            raise SystemExit()
        collector._run_owned(store, 'execution-1', 1, ['fake-review'], cwd=worktree,
                             stdin_bytes=b'', env={}, deadline=deadline)
        return {'complete': True}

    monkeypatch.setattr(collector, 'collect_checkpoints', collect)
    monkeypatch.setattr(collector, 'run_process', lambda *args, **kwargs:
                        {'outcome': 'timed_out', 'exit_code': -9, 'signal': 9, 'cleanup': 'confirmed', 'processes': []})
    if failure != 'timeout':
        with pytest.raises(SystemExit):
            supervisor.supervise(store, 'execution-1')
        monkeypatch.setattr(supervisor, 'identity_matches', lambda identity: False)
        supervisor.recover(store, 'execution-1', cleanup_timeout=0)
    else:
        supervisor.supervise(store, 'execution-1')
    attempt = store.load('execution-1')['attempts'][-1]
    assert attempt['exit_code'] == 0
    assert attempt['status'] == ('interrupted' if failure == 'supervisor_loss' else 'timed_out')
    if failure != 'supervisor_loss':
        receipt = json.loads((store.root / 'execution-1' / 'collector-exits-1.json').read_text())['exits'][-1]
        assert receipt['outcome'] == 'timed_out'
        assert receipt['exit_code'] == -9
        assert receipt['exit_signal'] == 9


@pytest.fixture
def execution(tmp_path):
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


@pytest.fixture
def manifest_execution(execution):
    from hermes_pipeline.agent_checkpoint import ProgressJournal

    store, worktree = execution
    registration = store.load("execution-1")["registration"]
    store.register(
        "manifest-1", registration_id="tick-manifest", plan_identity="b" * 64,
        phase="development", prompt=b"manifest prompt", client=registration["client"],
        worktree=str(worktree), branch="task", result_contract=registration["result_contract"],
        timeout=30, manifest={"tasks": [{"id": "one", "verification": ["python check.py"]}]},
    )
    ProgressJournal(store, "manifest-1").initialize()
    return store, worktree


@pytest.mark.parametrize("identity", ["execution-1", "manifest-1"])
def test_launch_requires_no_verification_sandbox(manifest_execution, monkeypatch, identity):
    store, _ = manifest_execution
    monkeypatch.setattr(supervisor, "validate_registration", lambda *args: None)
    monkeypatch.setattr(supervisor, "confirm_process_capability", lambda: None)
    monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(supervisor, "run_process", lambda *a, **k: {
        "outcome": "timed_out", "exit_code": None, "signal": None,
        "cleanup": "confirmed", "processes": [],
    })
    assert supervisor.supervise(store, identity)["generation"] == 1


def test_codex_direct_execution_preserves_stdin(execution, tmp_path):
    store, _ = execution
    staging = tmp_path / "staging"
    staging.mkdir()
    argv = supervisor.client_argv(store.load("execution-1")["registration"], staging)
    assert argv == ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "-"]


@pytest.mark.parametrize("operation", ["validate", "recover"])
def test_metadata_substitution_blocks_registration_and_recovery(execution, tmp_path, operation):
    from hermes_pipeline.agent_checkpoint import ProgressJournal

    store, worktree = execution
    store.admit("execution-1")
    clone = tmp_path / "unrelated-clone"
    subprocess.run(["git", "clone", str(worktree), str(clone)], check=True, capture_output=True)
    assert subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"]) == subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"])
    (worktree / ".git").rename(worktree / "original-git")
    (worktree / ".git").write_text("gitdir: " + str(clone / ".git") + "\n")
    with pytest.raises(ExecutionError, match="git_metadata_drift"):
        if operation == "validate":
            supervisor.validate_registration(store, "execution-1")
        else:
            ProgressJournal(store, "execution-1").recovery_context(1)


def test_claude_preserves_tools_and_stdin_mode(execution, tmp_path, monkeypatch):
    store, _ = execution
    registration = store.load("execution-1")["registration"]
    registration["client"] = {"name": "claude", "tools": ["Bash", "Read"]}
    staging = tmp_path / "staging"
    staging.mkdir()
    argv = supervisor.client_argv(registration, staging)
    assert argv[:3] == ["claude", "-p", "--dangerously-skip-permissions"]
    assert argv[argv.index("--tools") + 1] == "Bash,Read"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings == {"disableAllHooks": True}
    registration["client"]["tools"] = ["Bash;echo unsafe"]
    with pytest.raises(ExecutionError):
        supervisor.client_argv(registration, staging)


def test_worker_reentry_attaches_without_new_launch(execution, monkeypatch):
    store, _ = execution
    store.admit("execution-1")
    store.update_attempt("execution-1", 1, status="running", deadline_monotonic=123.0)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **k: pytest.fail("duplicate launch"))
    report = supervisor.attach(store, "execution-1")
    assert report["generation"] == 1
    assert len(store.load("execution-1")["attempts"]) == 1
    assert store.load("execution-1")["attempts"][0]["deadline_monotonic"] == 123.0


def test_abandoned_empty_inventory_cannot_establish_success(execution):
    store, _ = execution
    store.admit("execution-1")
    report = supervisor.recover(store, "execution-1", cleanup_timeout=0)
    assert report["status"] == "cleanup_unconfirmed"
    attempt = store.load("execution-1")["attempts"][-1]
    assert attempt["status"] == "interrupted"
    assert attempt["reason"] == "exit_unobservable"
    assert attempt["exit_code"] is None
    assert attempt["cleanup"] == "unconfirmed"


def test_recovery_uses_durable_client_before_inventory_callback(execution, monkeypatch):
    from hermes_pipeline.agent_execution import process_identity

    store, _ = execution
    store.admit("execution-1")
    direct = process_identity(os.getpid())
    store.update_attempt("execution-1", 1, status="running", client_process=direct)
    observed = []
    def cleanup(processes, **kwargs):
        observed.extend(processes)
        return {"cleanup": "confirmed", "processes": processes}
    monkeypatch.setattr(supervisor, "cleanup_processes", cleanup)
    supervisor.recover(store, "execution-1", cleanup_timeout=0)
    assert observed == [direct]
    attempt = store.load("execution-1")["attempts"][-1]
    assert attempt["status"] == "interrupted"
    assert attempt["exit_code"] is None


def test_zero_exit_without_contract_is_not_completion(execution):
    store, _ = execution
    store.admit("execution-1")
    store.update_attempt("execution-1", 1, status="exited", exit_code=0, cleanup="confirmed")
    report = supervisor.status(store, "execution-1")
    assert report["status"] != "completed"
    assert report["completion_allowed"] is False


def test_supervisor_persists_timeout_even_with_late_zero_exit(execution, monkeypatch, tmp_path):
    store, _ = execution
    monkeypatch.setattr(supervisor, "validate_registration", lambda *args: None)
    monkeypatch.setattr(supervisor, "client_argv", lambda *args, **kwargs: [sys.executable, "-c", "pass"])
    (tmp_path / "staging").mkdir()
    monkeypatch.setattr(supervisor, "staging_directory", lambda *args, **kwargs: tmp_path / "staging")
    def run(argv, **kwargs):
        assert kwargs["stdin_bytes"] == b"exact\x00prompt\n"
        assert kwargs["timeout"] == 30
        return {"outcome": "timed_out", "exit_code": 0, "signal": None,
                "cleanup": "confirmed", "processes": [],
                "launched_monotonic": 1.0, "deadline": 31.0}
    monkeypatch.setattr(supervisor, "run_process", run)
    report = supervisor.supervise(store, "execution-1")
    assert report["status"] == "timed_out"
    assert report["completion_allowed"] is False
    assert store.load("execution-1")["attempts"][0]["exit_code"] == 0


def test_supervisor_revalidates_identity_before_launch(execution, monkeypatch):
    store, _ = execution
    def invalid(*args):
        raise ExecutionError("registration_drift")
    monkeypatch.setattr(supervisor, "validate_registration", invalid)
    monkeypatch.setattr(supervisor, "run_process", lambda *a, **k: pytest.fail("invalid launch"))
    with pytest.raises(ExecutionError, match="registration_drift"):
        supervisor.supervise(store, "execution-1")
    assert store.load("execution-1")["attempts"] == []


def test_client_capability_failure_does_not_admit_and_fixed_reentry_can_launch(execution, monkeypatch):
    store, _ = execution
    monkeypatch.setattr(supervisor, "validate_registration", lambda *args: None)
    def unavailable(*args, **kwargs):
        raise ExecutionError("invalid_client_tools")
    monkeypatch.setattr(supervisor, "client_argv", unavailable)
    with pytest.raises(ExecutionError, match="invalid_client_tools"):
        supervisor.supervise(store, "execution-1")
    assert not store.load("execution-1")["attempts"]
    monkeypatch.setattr(supervisor, "client_argv", lambda *args, **kwargs: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(supervisor, "run_process", lambda *args, **kwargs: {
        "outcome": "timed_out", "exit_code": 0, "signal": None, "cleanup": "confirmed", "processes": []})
    assert supervisor.supervise(store, "execution-1")["status"] == "timed_out"
    assert len(store.load("execution-1")["attempts"]) == 1


def test_detached_prelaunch_refusal_is_visible_without_consuming_attempt(execution, monkeypatch, capsys):
    store, _ = execution
    monkeypatch.setattr(supervisor, "validate_registration", lambda *args: None)
    monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: ["tpo-test-missing-client"])
    assert supervisor.main(["_supervise", "--root", str(store.root), "--execution", "execution-1"]) == 1
    capsys.readouterr()
    assert supervisor.status(store, "execution-1")["status"] == "client_unavailable"
    assert not store.load("execution-1")["attempts"]
    refusal_path = store.root / "execution-1" / "launch-refusal.json"
    refusal = json.loads(refusal_path.read_text())
    refusal["generation"] = 2
    refusal_path.write_text(json.dumps(refusal))
    assert supervisor.status(store, "execution-1")["status"] == "registered"
    refusal["generation"] = 1
    refusal["registration_sha256"] = "f" * 64
    refusal_path.write_text(json.dumps(refusal))
    assert supervisor.status(store, "execution-1")["status"] == "registered"
    store.admit("execution-1")
    store.update_attempt("execution-1", 1, status="timed_out", cleanup="confirmed")
    refusal_path.write_text('{"reason":"malformed provider payload"}')
    assert supervisor.status(store, "execution-1")["status"] == "timed_out"


def test_prerequisite_disappears_between_attach_and_daemon(execution, monkeypatch, capsys):
    store, _ = execution
    monkeypatch.setattr(supervisor, "validate_registration", lambda *args: None)
    monkeypatch.setattr(supervisor, "installed_entrypoint", lambda: "/fake/supervisor")
    monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: [sys.executable, "-c", "pass"])
    spawn = supervisor.subprocess.Popen
    def daemon_spawn(argv, **kwargs):
        if "_supervise" not in argv:
            return spawn(argv, **kwargs)
        monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: ["tpo-test-missing-client"])
        supervisor.main(argv[1:])
        capsys.readouterr()  # Detached daemon stdout would normally be discarded.
    monkeypatch.setattr(supervisor.subprocess, "Popen", daemon_spawn)
    assert supervisor.main(["run", "--root", str(store.root), "--execution", "execution-1"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "client_unavailable"
    assert not store.load("execution-1")["attempts"]


def test_cli_distinguishes_real_locks_and_never_exposes_unknown_errors(execution, monkeypatch, capsys):
    from hermes_pipeline.agent_execution import LockUnconfirmed

    store, _ = execution
    command = ["run", "--root", str(store.root), "--execution", "execution-1"]
    for error, expected in [(LockUnconfirmed("locked"), "lock_unconfirmed"),
                            (ExecutionError("provider secret payload"), "execution_invalid")]:
        def failed(*args, **kwargs):
            raise error
        monkeypatch.setattr(supervisor, "attach", failed)
        assert supervisor.main(command) == 1
        output = capsys.readouterr().out
        assert json.loads(output)["status"] == expected
        assert "provider secret" not in output


def test_actual_admission_lock_remains_distinct_from_capability_refusal(execution, monkeypatch, capsys):
    import fcntl

    store, _ = execution
    monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: pytest.fail("locked preflight"))
    with store._directory_handle("execution-1") as directory:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert supervisor.main(["run", "--root", str(store.root), "--execution", "execution-1"]) == 1
            assert json.loads(capsys.readouterr().out)["status"] == "lock_unconfirmed"
        finally:
            fcntl.flock(directory, fcntl.LOCK_UN)
    assert supervisor.status(store, "execution-1")["status"] == "registered"


@pytest.mark.parametrize("daemon_admits", [False, True])
def test_explicit_recovery_waits_for_daemon_without_rewriting_terminal_attempt(execution, monkeypatch, capsys, daemon_admits):
    from types import SimpleNamespace

    from hermes_pipeline.agent_recovery import approve_recovery, prepare_recovery

    store, _ = execution
    store.admit("execution-1")
    store.update_attempt("execution-1", 1, status="timed_out", cleanup="confirmed")
    before = store.load("execution-1")["attempts"]
    preview = prepare_recovery(store, "execution-1")
    event = approve_recovery(store, "execution-1", preview)
    monkeypatch.setattr(supervisor, "validate_registration", lambda *a: None)
    monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(supervisor, "installed_entrypoint", lambda: "/fake/supervisor")
    spawn = supervisor.subprocess.Popen
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda argv, **kwargs: (
        None if "_supervise" in argv else spawn(argv, **kwargs)))
    clock = [0.0]
    def wait(interval):
        clock[0] += interval
        if daemon_admits and clock[0] >= 0.2 and len(store.load("execution-1")["attempts"]) == 1:
            supervisor.supervise(store, "execution-1", recovery_event=event)
    monkeypatch.setattr(supervisor, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=wait))
    monkeypatch.setattr(supervisor, "run_process", lambda *a, **k: {
        "outcome": "timed_out", "exit_code": None, "signal": None, "cleanup": "confirmed", "processes": []})
    result = supervisor.main(["run", "--root", str(store.root), "--execution", "execution-1",
                              "--recovery-event", event])
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == ("timed_out" if daemon_admits else "waiting_for_admission")
    assert report["generation"] == (2 if daemon_admits else 1)
    assert result == (1 if daemon_admits else 0)
    assert report["completion_allowed"] is False
    assert store.load("execution-1")["attempts"][:1] == before
    assert 0.2 <= clock[0] <= 5.1


@pytest.mark.parametrize("failure", ["entrypoint", "spawn"])
def test_cli_detach_refusal_remains_visible_to_status(execution, monkeypatch, capsys, failure):
    store, _ = execution
    monkeypatch.setattr(supervisor, "validate_registration", lambda *a: None)
    monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: [sys.executable, "-c", "pass"])
    def unavailable():
        raise ExecutionError("supervisor_unavailable")
    monkeypatch.setattr(supervisor, "installed_entrypoint", unavailable if failure == "entrypoint" else lambda: "/missing/supervisor")
    command = ["run", "--root", str(store.root), "--execution", "execution-1"]
    expected = "supervisor_unavailable" if failure == "entrypoint" else "launch_unavailable"
    assert supervisor.main(command) == 1
    assert json.loads(capsys.readouterr().out)["status"] == expected
    assert supervisor.main(["status", *command[1:]]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == expected
    assert not store.load("execution-1")["attempts"]


def test_detach_failure_cannot_write_refusal_over_concurrent_admission(execution, monkeypatch, capsys):
    store, _ = execution
    monkeypatch.setattr(supervisor, "validate_registration", lambda *a: None)
    monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: [sys.executable, "-c", "pass"])
    monkeypatch.setattr(supervisor, "installed_entrypoint", lambda: "/missing/supervisor")
    spawn = supervisor.subprocess.Popen
    def race(argv, **kwargs):
        if "_supervise" not in argv:
            return spawn(argv, **kwargs)
        store.admit("execution-1")
        store.update_attempt("execution-1", 1, status="timed_out", cleanup="confirmed")
        raise FileNotFoundError("disappeared supervisor")
    monkeypatch.setattr(supervisor.subprocess, "Popen", race)
    assert supervisor.main(["run", "--root", str(store.root), "--execution", "execution-1"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "timed_out"
    assert not (store.root / "execution-1" / "launch-refusal.json").exists()


def test_proven_spawn_failure_has_confirmed_cleanup_but_never_automatic_retry(execution, monkeypatch):
    from hermes_pipeline import agent_process

    store, _ = execution
    monkeypatch.setattr(supervisor, "validate_registration", lambda *args: None)
    monkeypatch.setattr(supervisor, "client_argv", lambda *args, **kwargs: [sys.executable, "-c", "pass"])
    original_spawn = agent_process.subprocess.Popen
    def failed_spawn(args, **kwargs):
        if args[0] in {sys.executable, "systemd-run"}:
            raise FileNotFoundError("executable vanished")
        return original_spawn(args, **kwargs)
    monkeypatch.setattr(agent_process.subprocess, "Popen", failed_spawn)
    supervisor.supervise(store, "execution-1")
    attempt = store.load("execution-1")["attempts"][-1]
    assert attempt["status"] == "blocked"
    assert attempt["cleanup"] == "confirmed"
    assert attempt["reason"] == "client_not_launched"
    assert attempt["exit_code"] is None
    supervisor.supervise(store, "execution-1")
    assert len(store.load("execution-1")["attempts"]) == 1
    from hermes_pipeline.agent_recovery import approve_recovery, prepare_recovery

    preview = prepare_recovery(store, "execution-1")
    event = approve_recovery(store, "execution-1", preview)
    monkeypatch.setattr(agent_process.subprocess, "Popen", original_spawn)
    monkeypatch.setattr(supervisor, "run_process", lambda *args, **kwargs: {
        "outcome": "timed_out", "exit_code": 0, "signal": None, "cleanup": "confirmed", "processes": []})
    assert supervisor.supervise(store, "execution-1", recovery_event=event)["generation"] == 2


def test_internal_cli_rejects_arbitrary_command(capsys):
    with pytest.raises(SystemExit):
        supervisor.main(["run", "--command", "echo unsafe"])
    assert "error" in capsys.readouterr().err


def test_missing_installed_supervisor_blocks_dispatch(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(supervisor, "sys", SimpleNamespace(executable=str(tmp_path / "missing-python")))
    monkeypatch.setattr(supervisor.shutil, "which", lambda name: None)
    with pytest.raises(ExecutionError, match="supervisor_unavailable"):
        supervisor.installed_entrypoint()


def test_worker_card_is_identity_only_and_cannot_retry():
    body = supervisor.worker_instructions("execution-1", "/state/executions")
    assert "tpo-agent-supervisor" in body
    assert "execution-1" in body
    assert "python -m" not in body and "codex exec" not in body
    assert "manual" in body and "generation" in body
    assert "metadata.tpo_result" in body
    assert "retry" in body
    json.dumps(body)


def test_binding_pins_prompt_before_replacing_card(execution, monkeypatch):
    from hermes_pipeline.kanban_tasks import PreparedPhaseTask, bind_prepared_executions
    store, worktree = execution
    captured = []
    monkeypatch.setattr(supervisor, "register_execution", lambda **kwargs: captured.append(kwargs) or "execution-1")
    prepared = [PreparedPhaseTask("development", "Develop", '{"phase_key":"development"}\nold unmanaged body', 5,
                                  rendered_prompt="exact\n", prompt_client="codex", tools="Bash")]
    bound = bind_prepared_executions(prepared, project_dir=worktree, state_dir=store.root.parent,
                                     root=store.root, tick_id="tick", worktree=worktree, todo_id="TODO-1")
    assert captured[0]["prompt"] == "exact\n"
    assert "old unmanaged body" not in bound[0].body
    assert "tpo-agent-supervisor" in bound[0].body


def _committed_profile(tmp_path, monkeypatch):
    from hermes_pipeline import agent_authority

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-b", "task", str(worktree)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "--allow-empty", "-m", "test base"], check=True, capture_output=True)
    monkeypatch.setattr(supervisor, "installed_entrypoint", lambda: str(Path(sys.executable).parent / "tpo-agent-supervisor"))
    home = tmp_path / "account"
    home.mkdir()
    monkeypatch.setattr(agent_authority, "account_home", lambda: home)
    root = agent_authority.profile_root(worktree)
    identity = supervisor.register_execution(
        project_dir=worktree, state_dir=tmp_path / "control", root=root, tick_id="tick-test",
        phase="analysis", prompt="Exact prompt: $() `echo no`\x00\n", client="codex", tools="Bash",
        worktree=worktree, timeout=10, todo_id="TODO-1")
    return ExecutionStore(root), identity, worktree


def test_registered_cli_repair_runs_fake_client_once(tmp_path, monkeypatch, capsys):
    store, identity, _ = _committed_profile(tmp_path, monkeypatch)
    fake_client = tmp_path / "fake-codex"
    original_which = supervisor.shutil.which
    monkeypatch.setattr(supervisor.shutil, "which", lambda name, **kwargs: (
        str(fake_client) if fake_client.exists() else None) if name == "codex" else original_which(name, **kwargs))
    spawn = supervisor.subprocess.Popen
    daemons = []
    def daemon_spawn(argv, **kwargs):
        if "_supervise" not in argv:
            return spawn(argv, **kwargs)
        daemons.append(argv)
        supervisor.main(argv[1:])
        capsys.readouterr()
    monkeypatch.setattr(supervisor.subprocess, "Popen", daemon_spawn)
    command = ["run", "--root", str(store.root), "--execution", identity]
    assert supervisor.main(command) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "client_unavailable"
    assert not daemons
    fake_client.write_text("#!" + sys.executable + "\nimport sys\nsys.stdin.buffer.read()\nsys.exit(17)\n")
    fake_client.chmod(0o700)
    assert supervisor.main(command) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "exited"
    assert report["exit_code"] == 17
    assert report["generation"] == 1
    first = store.load(identity)["attempts"]
    fake_client.unlink()
    monkeypatch.setattr(supervisor, "client_argv", lambda *a, **k: pytest.fail("reentry preflight"))
    assert supervisor.main(command) == 1
    capsys.readouterr()
    assert store.load(identity)["attempts"] == first
    assert len(daemons) == 1


def test_promoted_result_is_immutable_and_dirty_work_blocks_completion(tmp_path, monkeypatch):
    store, identity, worktree = _committed_profile(tmp_path, monkeypatch)
    store.admit(identity)
    store.update_attempt(identity, 1, status="exited", exit_code=0, cleanup="confirmed")
    staging = supervisor.staging_directory(store, identity, 1)
    result = dict(schema_version=1, execution_id=identity, generation=1, tick_id="tick-test",
                  todo_id="TODO-1", step_key="analysis", verdict="success",
                  head_sha=supervisor._git(worktree, "rev-parse", "HEAD"))
    (staging / "result.json").write_text(json.dumps(result))
    # Even a valid staging assertion is insufficient until supervisor promotion.
    assert supervisor.status(store, identity)["completion_allowed"] is False
    supervisor.validated_result(store, identity, 1, promote=True, deadline_monotonic=time.monotonic() + 10)
    monkeypatch.setattr(time, "monotonic", lambda: 10**12)
    (staging / "result.json").write_text('{"provider_payload":"untrusted changed staging"}')
    assert supervisor.status(store, identity)["metadata"]["tpo_result"] == result
    (worktree / "partial-work").write_text("unfinished")
    assert supervisor.status(store, identity)["completion_allowed"] is False
    assert (worktree / "partial-work").read_text() == "unfinished"


def test_result_promotion_rejects_validation_past_deadline(tmp_path, monkeypatch):
    from hermes_pipeline.agent_collector import CollectionTimedOut
    store, identity, worktree = _committed_profile(tmp_path, monkeypatch)
    store.admit(identity)
    staging = supervisor.staging_directory(store, identity, 1)
    result = dict(schema_version=1, execution_id=identity, generation=1, tick_id="tick-test",
                  todo_id="TODO-1", step_key="analysis", verdict="success",
                  head_sha=supervisor._git(worktree, "rev-parse", "HEAD"))
    (staging / "result.json").write_text(json.dumps(result))
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    original = supervisor._git

    def delayed(*args):
        value = original(*args)
        if args[1:] == ('merge-base', '--is-ancestor', result['head_sha'], result['head_sha']):
            clock[0] = 201.0
        return value

    monkeypatch.setattr(supervisor, '_git', delayed)
    with pytest.raises(CollectionTimedOut):
        supervisor.validated_result(store, identity, 1, promote=True, deadline_monotonic=110.0)
    assert not (store.root / identity / 'result-1.json').exists()


def test_supervision_preserves_timeout_when_final_validation_expires(execution, monkeypatch):
    from hermes_pipeline import agent_collector as collector
    store, _ = execution
    monkeypatch.setattr(supervisor, 'validate_registration', lambda *args: None)
    monkeypatch.setattr(supervisor, 'client_argv', lambda *args, **kwargs: [sys.executable, '-c', 'pass'])
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(supervisor, 'run_process', lambda *args, **kwargs:
                        {'outcome': 'exited', 'exit_code': 0, 'signal': None, 'cleanup': 'confirmed',
                         'processes': [], 'deadline': 110.0})

    def collect(*args, **kwargs):
        clock[0] = 201.0
        return {'complete': True}

    monkeypatch.setattr(collector, 'collect_checkpoints', collect)
    report = supervisor.supervise(store, 'execution-1')
    assert report['status'] == 'timed_out'
    assert report['completion_allowed'] is False
    assert store.load('execution-1')['attempts'][-1]['reason'] == 'checkpoint_deadline_exceeded'


@pytest.mark.parametrize('late_boundary', ['report', 'terminal_write'])
def test_supervision_decides_deadline_before_terminal_success(tmp_path, monkeypatch, late_boundary):
    store, identity, worktree = _committed_profile(tmp_path, monkeypatch)
    monkeypatch.setattr(supervisor, 'client_argv', lambda *args, **kwargs: [sys.executable, '-c', 'pass'])
    staging = supervisor.staging_directory(store, identity, 1)
    result = dict(schema_version=1, execution_id=identity, generation=1, tick_id="tick-test",
                  todo_id="TODO-1", step_key="analysis", verdict="success",
                  head_sha=supervisor._git(worktree, "rev-parse", "HEAD"))
    (staging / "result.json").write_text(json.dumps(result))
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(supervisor, 'run_process', lambda *args, **kwargs:
                        {'outcome': 'exited', 'exit_code': 0, 'signal': None, 'cleanup': 'confirmed',
                         'processes': [], 'deadline': 110.0})
    original_status = supervisor._status
    original_update = store.update_attempt

    def delayed_status(*args, **kwargs):
        report = original_status(*args, **kwargs)
        if late_boundary == 'report' and kwargs.get('revalidate') is False:
            clock[0] = 201.0
        return report

    def delayed_update(*args, **kwargs):
        value = original_update(*args, **kwargs)
        if late_boundary == 'terminal_write' and kwargs.get('status') == 'exited':
            clock[0] = 201.0
        return value

    monkeypatch.setattr(supervisor, '_status', delayed_status)
    monkeypatch.setattr(store, 'update_attempt', delayed_update)
    report = supervisor.supervise(store, identity)
    expected = 'timed_out' if late_boundary == 'report' else 'completed'
    assert report['status'] == expected
    assert report['completion_allowed'] is (late_boundary == 'terminal_write')
    # Durable status must agree, even when promotion happened before timeout.
    assert supervisor.status(store, identity)['status'] == expected


def test_copied_profile_registration_in_staging_is_not_authority(tmp_path, monkeypatch):
    import shutil

    store, identity, _ = _committed_profile(tmp_path, monkeypatch)
    forged = tmp_path / "agent-submissions" / "forged"
    shutil.copytree(store.root, forged)
    with pytest.raises(ExecutionError, match="profile_authority_root_unconfirmed"):
        supervisor.validate_registration(ExecutionStore(forged), identity)


def test_profile_registration_rejects_noncanonical_root_before_record_creation(tmp_path, monkeypatch):
    _, _, worktree = _committed_profile(tmp_path, monkeypatch)
    forged = tmp_path / "forged"
    with pytest.raises(ExecutionError, match="profile_authority_root_unconfirmed"):
        supervisor.register_execution(
            project_dir=worktree, state_dir=tmp_path / "control", root=forged, tick_id="tick-forged",
            phase="analysis", prompt="untrusted", client="codex", tools="Bash",
            worktree=worktree, timeout=10, todo_id="TODO-1")
    assert not forged.exists()


def test_existing_manifest_work_keeps_original_registered_base(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from hermes_pipeline.plan_manifest import PlanManifest, PlanTask

    project = tmp_path / "repository"
    project.mkdir()
    def git(*args, cwd=project):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.org")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    worktree = project / ".worktrees" / "task"
    git("worktree", "add", "-b", "task", str(worktree))
    git("commit", "--allow-empty", "-m", "existing implementation", cwd=worktree)
    existing = git("rev-parse", "HEAD", cwd=worktree)
    state = project / ".hermes"
    (state / "runs" / "tick").mkdir(parents=True)
    (state / "runs" / "tick" / "registration.json").write_text("{}")
    manifest = PlanManifest(1, "TODO-1", (PlanTask("task-1", "Task", "Work", ("works",), ("true",), "feat: work"),))
    monkeypatch.setattr(supervisor, "load_validated_registration", lambda *args: SimpleNamespace(
        worktree=worktree, prompt_client="codex", manifest=manifest, plan_hash="a" * 64, base_sha=base))
    monkeypatch.setattr(supervisor, "installed_entrypoint", lambda: "tpo-agent-supervisor")
    root = state / "agent-executions"
    for phase, expected in (("phase_4_development", base), ("review:0", existing)):
        identity = supervisor.register_execution(
            project_dir=project, state_dir=state, root=root, tick_id="tick", phase=phase,
            prompt="Pinned work", client="codex", tools="Bash", worktree=worktree, timeout=30, todo_id="TODO-1")
        record = ExecutionStore(root).load(identity)
        assert record["registration"]["result_contract"]["base_sha"] == expected
        assert not record["attempts"]
    assert git("rev-parse", "HEAD", cwd=worktree) == existing


def test_supervisor_survives_worker_and_preserves_prompt_bytes(tmp_path, monkeypatch):
    store, identity, worktree = _committed_profile(tmp_path, monkeypatch)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # A test account resolver avoids writing the real account's global state.
    # The separate packaging smoke exercises the unmodified installed launcher.
    executable = bindir / "tpo-agent-supervisor"
    executable.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
                          "from hermes_pipeline import agent_authority, agent_process\n"
                          "from types import SimpleNamespace\n"
                          "if sys.platform == 'linux': agent_process.sys = SimpleNamespace(platform='legacy')\n"
                          f"agent_authority.account_home = lambda: Path({str(tmp_path / 'account')!r})\n"
                          "from hermes_pipeline import _agent_supervisor as supervisor\n"
                          "supervisor.installed_entrypoint = lambda: str(Path(__file__).absolute())\n"
                          "sys.exit(supervisor.main())\n")
    executable.chmod(0o700)
    client = bindir / "codex"
    captured = tmp_path / "stdin.bin"
    head = supervisor._git(worktree, "rev-parse", "HEAD")
    client.write_text(f"#!{sys.executable}\n" +
        "import json, os, pathlib, subprocess, sys, time\n" +
        f"pathlib.Path({str(captured)!r}).write_bytes(sys.stdin.buffer.read())\n" +
        "time.sleep(0.5)\n" +
        f"result = {{'schema_version': 1, 'execution_id': {identity!r}, 'generation': int(os.environ['TPO_ATTEMPT_GENERATION']), 'tick_id': 'tick-test', 'todo_id': 'TODO-1', 'step_key': 'analysis', 'verdict': 'success', 'head_sha': {head!r}}}\n" +
        "pathlib.Path(os.environ['TPO_RESULT_PATH']).write_text(json.dumps(result))\n")
    client.chmod(0o700)
    worker = subprocess.Popen([str(executable), "run", "--root", str(store.root), "--execution", identity],
                              env={**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 8
        while not captured.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert captured.exists()
        worker.terminate()
        worker.wait(timeout=2)
        while time.monotonic() < deadline:
            report = supervisor.status(store, identity)
            if report["status"] in {"completed", "result_invalid", "cleanup_unconfirmed", "interrupted"}:
                break
            time.sleep(0.02)
        assert report["status"] == "completed"
        assert captured.read_bytes() == store.prompt(identity)
        assert store.load(identity)["attempts"][0]["exit_code"] == 0
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=2)
        supervisor.recover(store, identity, cleanup_timeout=1)


def test_worktree_admission_wait_retries_in_code_after_release(execution, monkeypatch, capsys):
    from types import SimpleNamespace

    from hermes_pipeline.agent_execution import ExecutionStore, process_identity
    store, _ = execution
    owner = store.worktree_locked('execution-1')
    owner.__enter__()
    released = False
    clock = [0.0]
    def wait(interval):
        nonlocal released
        clock[0] += interval
        if not released:
            owner.__exit__(None, None, None)
            released = True
    monkeypatch.setattr(supervisor, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=wait))
    monkeypatch.setattr(supervisor, '_prepare_launch', lambda *a: ([], {}))
    monkeypatch.setattr(supervisor, 'installed_entrypoint', lambda: '/fake/supervisor')
    launches = []
    def launch(*args, **kwargs):
        launches.append(args)
        another = ExecutionStore(store.root)
        another.admit('execution-1')
        another.update_attempt('execution-1', 1, status='running',
                               supervisor=process_identity(os.getpid()), deadline_monotonic=123.0)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', launch)
    try:
        for _ in range(2):
            assert supervisor.main(['run', '--root', str(store.root), '--execution', 'execution-1']) == 0
            report = json.loads(capsys.readouterr().out)
            assert report['status'] == 'running_detached'
            assert report['generation'] == 1
    finally:
        if not released:
            owner.__exit__(None, None, None)
    assert len(launches) == 1
    assert len(store.load('execution-1')['attempts']) == 1
    assert store.load('execution-1')['attempts'][0]['deadline_monotonic'] == 123.0
    assert clock[0] <= 10.3


def test_unadmitted_daemon_window_stays_honestly_pending(execution, monkeypatch, capsys):
    from types import SimpleNamespace
    store, _ = execution
    clock = [0.0]
    def wait(interval):
        clock[0] += interval
    monkeypatch.setattr(supervisor, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=wait))
    monkeypatch.setattr(supervisor, '_prepare_launch', lambda *a: ([], {}))
    monkeypatch.setattr(supervisor, 'installed_entrypoint', lambda: '/fake/supervisor')
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *a, **k: None)
    assert supervisor.main(['run', '--root', str(store.root), '--execution', 'execution-1']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'waiting_for_admission'
    assert report['reason'] == 'launch_pending'
    assert report['generation'] == 0
    assert not report['completion_allowed']
    assert store.load('execution-1')['attempts'] == []
    assert 5 <= clock[0] <= 5.1


def test_unsupported_worktree_admission_lock_is_not_retryable(execution, monkeypatch, capsys):
    import errno

    from hermes_pipeline import agent_execution
    store, _ = execution
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, 'unsupported locking')
    monkeypatch.setattr(agent_execution.fcntl, 'flock', unsupported)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *a, **k: pytest.fail('unsupported admission'))
    monkeypatch.setattr(supervisor.time, 'sleep', lambda *a: pytest.fail('unsupported lock retried'))
    assert supervisor.main(['run', '--root', str(store.root), '--execution', 'execution-1']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'lock_unconfirmed'
    assert not report['completion_allowed']
    assert store.load('execution-1')['attempts'] == []


@pytest.mark.parametrize('finish_at', [8.0, None])
def test_wait_cli_keeps_original_attempt_budget(execution, monkeypatch, capsys, finish_at):
    from types import SimpleNamespace

    store, _ = execution
    store.admit('execution-1')
    store.update_attempt('execution-1', 1, status='running',
                         supervisor=supervisor.process_identity(os.getpid()), deadline_monotonic=10.0)
    clock = [0.0]
    def sleep(interval):
        clock[0] += interval
        if finish_at is not None and clock[0] >= finish_at:
            store.update_attempt('execution-1', 1, status='timed_out', cleanup='confirmed')
    monkeypatch.setattr(supervisor, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *a, **k: pytest.fail('duplicate launch'))
    args = ['run', '--wait', '--root', str(store.root), '--execution', 'execution-1']
    assert supervisor.main(args) == (1 if finish_at else 0)
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == ('timed_out' if finish_at else 'running_detached')
    assert finish_at <= clock[0] <= finish_at + 0.2 if finish_at else 70 <= clock[0] <= 70.2
    assert store.load('execution-1')['attempts'][0]['deadline_monotonic'] == 10.0
    if finish_at is None:
        previous = clock[0]
        assert supervisor.main(args) == 0
        capsys.readouterr()
        assert clock[0] == previous


def test_wait_cli_bounds_unadmitted_wait(execution, monkeypatch, capsys):
    from types import SimpleNamespace

    store, _ = execution
    clock = [0.0]
    def sleep(interval):
        clock[0] += interval
    monkeypatch.setattr(supervisor, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(supervisor, '_prepare_launch', lambda *a: ([], {}))
    monkeypatch.setattr(supervisor, 'installed_entrypoint', lambda: '/fake/supervisor')
    launches = []
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *a, **k: launches.append(a))
    assert supervisor.main(['run', '--wait', '--root', str(store.root), '--execution', 'execution-1']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'waiting_for_admission'
    assert len(launches) == 1
    assert not store.load('execution-1')['attempts']
    assert 90 <= clock[0] <= 90.2


def test_worker_waits_for_command_completion_and_never_blocks_nonterminal():
    body = supervisor.worker_instructions('execution-1', '/state/executions')
    assert 'run --wait' in body
    assert 'background' in body
    assert 'Never use kanban_block for running_detached or waiting_for_admission' in body


@pytest.mark.parametrize('previous_attempt', ['previous_boot', 'previous_generation'])
def test_wait_cli_ignores_unrelated_monotonic_budget(execution, monkeypatch, capsys, previous_attempt):
    from types import SimpleNamespace

    store, _ = execution
    store.admit('execution-1')
    owner = supervisor.process_identity(os.getpid())
    if previous_attempt == 'previous_boot':
        owner['boot_id'] = 'previous-boot'
    store.update_attempt('execution-1', 1, status='timed_out', cleanup='confirmed',
                         supervisor=owner, deadline_monotonic=10.0)
    clock = [0.0]
    def sleep(interval):
        clock[0] += interval
    monkeypatch.setattr(supervisor, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    pending = {'status': 'waiting_for_admission', 'reason': 'launch_pending',
               'generation': 1, 'completion_allowed': False}
    monkeypatch.setattr(supervisor, 'attach', lambda *a, **k: pending)
    if previous_attempt == 'previous_boot':
        monkeypatch.setattr(supervisor, 'status', lambda *a, **k: pending)
    args = ['run', '--wait', '--root', str(store.root), '--execution', 'execution-1']
    if previous_attempt == 'previous_generation':
        args += ['--recovery-event', 'approved-event']
    assert supervisor.main(args) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'waiting_for_admission'
    assert 90 <= clock[0] <= 90.2
    assert len(store.load('execution-1')['attempts']) == 1


def test_worker_completion_passes_metadata_envelope_to_kanban_tool():
    body = supervisor.worker_instructions('execution-1', '/state/executions')
    assert 'set its metadata argument to the entire returned report.metadata object' in body
    assert 'metadata={"tpo_result": <validated result>}' in body
    assert 'Never pass report.metadata.tpo_result alone as the metadata argument' in body
    assert 'Keep the nested tpo_result unchanged' in body


def _cgroup_receipt(letter='a'):
    unit = 'tpo-' + letter * 32 + '.scope'
    return dict(version=1, unit=unit, path='/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/' + unit,
                device=1, inode=2, boot_id='boot', host='host')


def test_supervisor_persists_group_before_client_launch(execution, monkeypatch):
    from hermes_pipeline.agent_execution import process_identity
    store, _ = execution
    receipt = _cgroup_receipt()
    process = process_identity(os.getpid())
    monkeypatch.setattr(supervisor, 'validate_registration', lambda *a: None)
    monkeypatch.setattr(supervisor, 'confirm_process_capability', lambda: None)
    monkeypatch.setattr(supervisor, 'client_argv', lambda *a, **k: [sys.executable])
    def run(*args, **kwargs):
        kwargs['on_cgroup'](receipt)
        assert store.load('execution-1')['attempts'][-1]['owned_cgroups'] == [receipt]
        kwargs['on_launch']({'identity': process, 'launched_monotonic': 10., 'deadline': 40.})
        kwargs['on_processes']([process])
        raise supervisor.ProcessOwnershipError([process])
    monkeypatch.setattr(supervisor, 'run_process', run)
    supervisor.supervise(store, 'execution-1')
    attempt = store.load('execution-1')['attempts'][-1]
    assert attempt['client_process']['cgroup'] == receipt['unit']
    assert attempt['owned_processes'] == [{**process, 'cgroup': receipt['unit']}]


@pytest.mark.parametrize('confirmed', [True, False])
def test_recovery_cleans_groups_without_pid_scan_or_inventing_exit(execution, monkeypatch, confirmed):
    from hermes_pipeline import agent_cgroup
    from hermes_pipeline.agent_execution import process_identity
    store, _ = execution
    store.admit('execution-1')
    receipt = _cgroup_receipt()
    process = {**process_identity(os.getpid()), 'cgroup': receipt['unit']}
    store.update_attempt('execution-1', 1, status='running', owned_cgroups=[receipt], client_process=process,
                         owned_processes=[process])
    observed = []
    def cleanup(group, **kwargs):
        observed.append(group)
        return {'cleanup': 'confirmed' if confirmed else 'cleanup_unconfirmed', 'processes': []}
    monkeypatch.setattr(agent_cgroup, 'cleanup_cgroup', cleanup)
    monkeypatch.setattr(supervisor, 'cleanup_processes', lambda *a, **k: pytest.fail('cgroup members must not use PID recovery'))
    supervisor.recover(store, 'execution-1', cleanup_timeout=0)
    attempt = store.load('execution-1')['attempts'][-1]
    assert observed == [receipt]
    assert attempt['cleanup'] == ('confirmed' if confirmed else 'unconfirmed')
    assert attempt['status'] == 'interrupted'
    assert attempt['exit_code'] is None


@pytest.mark.parametrize('receipt_exists', [True, False])
def test_recovery_resolves_only_receipt_for_pending_collector_launch(execution, monkeypatch, receipt_exists):
    from hermes_pipeline import agent_cgroup, agent_collector
    store, _ = execution
    store.admit('execution-1')
    previous, current = _cgroup_receipt('a'), _cgroup_receipt('b')
    store.update_attempt('execution-1', 1, status='running', owned_cgroups=[previous])
    agent_collector._launch_marker(store, 'execution-1', 1, True)
    if receipt_exists:
        store.update_attempt('execution-1', 1, owned_cgroups=[previous, current])
    monkeypatch.setattr(agent_cgroup, 'cleanup_cgroup', lambda *a, **k: {'cleanup': 'confirmed', 'processes': []})
    for _ in range(2):
        supervisor.recover(store, 'execution-1', cleanup_timeout=0)
        attempt = store.load('execution-1')['attempts'][-1]
        assert attempt['cleanup'] == ('confirmed' if receipt_exists else 'unconfirmed')
        assert agent_collector.collector_launch_pending(store, 'execution-1') is not receipt_exists


def test_worker_command_pins_interpreter_sibling_across_changed_path(tmp_path, monkeypatch):
    import shlex
    from types import SimpleNamespace

    installed = tmp_path / 'new install with spaces' / 'bin'
    stale = tmp_path / 'old-bin'
    installed.mkdir(parents=True)
    stale.mkdir()
    interpreter = installed / 'python'
    interpreter.symlink_to(sys.executable)
    marker = tmp_path / 'selected'
    for directory, label in ((installed, 'pinned'), (stale, 'stale')):
        helper = directory / 'tpo-agent-supervisor'
        helper.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).write_text({label!r})\n')
        helper.chmod(0o700)
    monkeypatch.setattr(supervisor, 'sys', SimpleNamespace(executable=str(interpreter)), raising=False)
    monkeypatch.setenv('PATH', str(stale))
    body = supervisor.worker_instructions('execution-1', '/state with spaces/executions')
    command = shlex.split(body.splitlines()[1])
    subprocess.run(command, check=True, timeout=5)
    assert marker.read_text() == 'pinned'
    assert command == [str(installed / 'tpo-agent-supervisor'), 'run', '--wait', '--root',
                       '/state with spaces/executions', '--execution', 'execution-1']


@pytest.mark.parametrize('sibling_exists', [False, True])
def test_entrypoint_path_fallback_requires_no_executable_sibling(tmp_path, monkeypatch, sibling_exists):
    from types import SimpleNamespace

    installed = tmp_path / 'environment'
    installed.mkdir()
    if sibling_exists:
        sibling = installed / 'tpo-agent-supervisor'
        sibling.write_text('not executable')
        sibling.chmod(0o600)
    fallback = tmp_path / 'fallback' / 'tpo-agent-supervisor'
    monkeypatch.setattr(supervisor, 'sys', SimpleNamespace(executable=str(installed / 'python')))
    monkeypatch.setattr(supervisor.shutil, 'which', lambda _: str(fallback))
    assert supervisor.installed_entrypoint() == str(fallback)
