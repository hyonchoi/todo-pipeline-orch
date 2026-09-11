"""Provider-free registered supervisor execution through native Linux cgroups."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_pipeline import _agent_supervisor as supervisor
from hermes_pipeline import agent_authority
from hermes_pipeline.agent_execution import ExecutionStore
from tests.test_agent_cgroup import native_cgroup  # noqa: F401


@pytest.mark.parametrize("failure", ["missing-tool", "remount-denied", "python-error"])
def test_required_namespace_probe_cannot_skip(tmp_path, monkeypatch, failure):
    from tests.test_agent_cgroup import (
        test_remounted_cgroup_namespace_cannot_confirm_hidden_live_scope,
    )

    monkeypatch.setenv("REQUIRE_NATIVE_CGROUP", "0" if failure == "python-error" else "1")
    (tmp_path / "cgroup.procs").write_text("123\n")

    def run_process(*args, **kwargs):
        kwargs["on_cgroup"]({"path": str(tmp_path)})

    def denied(*args, **kwargs):
        if failure == "missing-tool":
            raise FileNotFoundError()
        stderr = "tpo-namespace-remounted\nModuleNotFoundError: missing module" if failure == "python-error" else "denied"
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr=stderr)

    monkeypatch.setattr("hermes_pipeline.agent_process.run_process", run_process)
    monkeypatch.setattr(subprocess, "run", denied)
    expected = {
        "missing-tool": "native namespace tooling unavailable",
        "remount-denied": "native namespace remount unavailable: returncode=1; stderr='denied'",
        "python-error": "native namespace Python probe failed: returncode=1; stderr=",
    }[failure]
    with pytest.raises(pytest.fail.Exception, match=expected):
        try:
            test_remounted_cgroup_namespace_cannot_confirm_hidden_live_scope(tmp_path, None)
        except pytest.skip.Exception:
            raise AssertionError("required lane silently skipped the namespace probe") from None


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("valid_result", [True, False], ids=["valid-result", "missing-result"])
@pytest.mark.usefixtures("native_cgroup")
def test_registered_supervisor_native_cgroup(tmp_path, monkeypatch, client, valid_result):
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-b", "task", str(worktree)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.org",
         "commit", "--allow-empty", "-m", "test base"], check=True, capture_output=True,
    )
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.setattr(agent_authority, "account_home", lambda: account)
    root = agent_authority.profile_root(worktree)
    identity = supervisor.register_execution(
        project_dir=worktree, state_dir=tmp_path / "control", root=root, tick_id="tick-native",
        phase="analysis", prompt="Exact prompt: $() `echo no`\x00\n", client=client,
        tools="Bash,Read", worktree=worktree, timeout=20, todo_id="TODO-1",
    )
    store = ExecutionStore(root)
    record_path = root / identity / "record.json"
    observed_path = tmp_path / "observed.json"
    executable = tmp_path / "bin" / client
    executable.parent.mkdir()
    executable.write_text(f"#!{sys.executable}\n" + f'''
import base64
import json
import os
import sys
from pathlib import Path

# Read the durable file directly before client work; never use a mocked store.
record = json.loads(Path({str(record_path)!r}).read_text())
attempt = record["attempts"][-1]
assert attempt["status"] == "running"
assert len(attempt["owned_cgroups"]) == 1
group = attempt["owned_cgroups"][0]
assert attempt["client_process"]["cgroup"] == group["unit"]
assert attempt["client_process"]["pid"] == os.getpid()
assert str(os.getpid()) in (Path(group["path"]) / "cgroup.procs").read_text().split()
stdin = sys.stdin.buffer.read()
assert stdin == base64.b64decode(record["registration"]["prompt_base64"])
Path({str(observed_path)!r}).write_text(json.dumps({{
    "stdin": base64.b64encode(stdin).decode(), "argv": sys.argv[1:],
    "cgroup": group, "client_process": attempt["client_process"],
}}))
if {valid_result!r}:
    contract = record["registration"]["result_contract"]
    result = dict(schema_version=1, execution_id={identity!r},
                  generation=int(os.environ["TPO_ATTEMPT_GENERATION"]),
                  tick_id=contract["tick_id"], todo_id=contract["todo_id"],
                  step_key=contract["phase"], verdict="success", head_sha=contract["base_sha"])
    Path(os.environ["TPO_RESULT_PATH"]).write_text(json.dumps(result))
''')
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])

    report = supervisor.supervise(store, identity)

    assert observed_path.exists(), report
    observed = json.loads(observed_path.read_text())
    record = store.load(identity)
    attempt = record["attempts"][-1]
    assert base64.b64decode(observed["stdin"]) == base64.b64decode(record["registration"]["prompt_base64"])
    if client == "codex":
        assert observed["argv"] == ["exec", "--dangerously-bypass-approvals-and-sandbox", "-"]
    else:
        assert observed["argv"][:2] == ["-p", "--dangerously-skip-permissions"]
        assert observed["argv"][observed["argv"].index("--tools") + 1] == "Bash,Read"
    assert attempt["owned_cgroups"] == [observed["cgroup"]]
    assert attempt["client_process"] == observed["client_process"]
    assert attempt["exit_code"] == 0
    assert attempt["cleanup"] == "confirmed"
    try:
        events = (Path(observed["cgroup"]["path"]) / "cgroup.events").read_text()
    except FileNotFoundError:
        pass  # systemd already removed the empty scope.
    else:
        assert "populated 0" in events.splitlines()
    assert report["completion_allowed"] is valid_result
    assert report["status"] == ("completed" if valid_result else "result_invalid")
    assert (root / identity / "result-1.json").exists() is valid_result
