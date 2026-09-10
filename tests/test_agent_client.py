"""Provider-free launch configuration and authority-boundary regressions."""
import json
import os
import subprocess
import sys
import tomllib

import pytest

from hermes_pipeline import agent_client
from hermes_pipeline.agent_execution import ExecutionError


@pytest.fixture
def layout(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", str(worktree)], check=True, capture_output=True)
    staging = tmp_path / "submissions"
    staging.mkdir()
    authority = tmp_path / "executions"
    authority.mkdir()
    registration = {"worktree": str(worktree), "client": {"name": "codex", "tools": ["Read", "Write", "Edit", "Bash"]}}
    registration["result_contract"] = {"git_metadata": {
        "worktree_git_dir": str(worktree / ".git"), "common_dir": str(worktree / ".git")}}
    return registration, staging, authority


def build(layout):
    registration, staging, authority = layout
    return agent_client.build_client_argv(registration, staging, authority_root=authority)


def test_native_profile_registration_enables_implementer_without_widening_reviewer(layout, monkeypatch):
    from hermes_pipeline.agent_execution import ExecutionStore
    from hermes_pipeline.phases import load_phases, resolve_profile_phases_path

    registration, staging, authority = layout
    phases = load_phases(resolve_profile_phases_path("native-sdd"))
    development = next(phase for phase in phases if phase.phase_key == "phase_4_development")
    store = ExecutionStore(authority)
    record = store.register(
        "native", registration_id="tick", plan_identity="a" * 64, phase=development.phase_key,
        prompt=development.prompt.encode(), client={"name": "claude", "tools": development.tools.split(",")},
        worktree=registration["worktree"], branch="task", result_contract=registration["result_contract"], timeout=30)
    monkeypatch.setattr(agent_client, "_confirm_claude_sandbox", lambda: None)
    argv = agent_client.build_client_argv(record["registration"], staging, authority_root=authority)
    assert "Agent" in argv[argv.index("--tools") + 1].split(",")
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "Agent" in settings["permissions"]["allow"]
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert str(authority) in settings["sandbox"]["filesystem"]["denyRead"]
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert all("Agent" not in phase.tools.split(",") for phase in phases if phase is not development)


@pytest.mark.parametrize("platform_name", ["linux", "darwin"])
@pytest.mark.parametrize("client_name", ["claude", "codex"])
def test_client_platform_argv_and_prerequisites(layout, monkeypatch, platform_name, client_name):
    """Configuration matrix only; Darwin and installed-client probes are simulated."""
    from types import SimpleNamespace

    registration, _, _ = layout
    registration["client"]["name"] = client_name
    monkeypatch.setattr(agent_client, "sys", SimpleNamespace(platform=platform_name))
    run = subprocess.run
    version_checks = []
    def client_probe(argv, **kwargs):
        if argv == ["claude", "--version"]:
            version_checks.append(argv)
            return SimpleNamespace(returncode=0, stdout=b"2.1.267 (Claude Code)\n")
        return run(argv, **kwargs)
    monkeypatch.setattr(agent_client.subprocess, "run", client_probe)
    which = agent_client.shutil.which
    dependencies = []
    def dependency(name, **kwargs):
        if name in {"bwrap", "socat"}:
            dependencies.append(name)
            return "/fake/" + name
        return which(name, **kwargs)
    monkeypatch.setattr(agent_client.shutil, "which", dependency)
    argv = build(layout)
    assert argv[0] == client_name
    if client_name == "claude":
        assert len(version_checks) == 1
        assert set(dependencies) == ({"bwrap", "socat"} if platform_name == "linux" else set())
        assert argv[argv.index("--tools") + 1] == "Read,Write,Edit,Bash"
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["sandbox"]["failIfUnavailable"] is True
        assert settings["sandbox"]["allowUnsandboxedCommands"] is False
        monkeypatch.setattr(agent_client.subprocess, "run", lambda args, **kwargs: (
            SimpleNamespace(returncode=1, stdout=b"untrusted provider response")
            if args == ["claude", "--version"] else run(args, **kwargs)))
        with pytest.raises(ExecutionError, match="^client_sandbox_unconfirmed$"):
            build(layout)
    else:
        assert not version_checks and not dependencies
        assert argv[-1] == "-"
        assert 'approval_policy="never"' in argv
        assert 'default_permissions="tpo-worktree"' in argv


def test_same_head_clone_cannot_substitute_registered_git_directory(layout, tmp_path):
    from pathlib import Path
    registration, staging, authority = layout
    worktree = Path(registration["worktree"])
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "--allow-empty", "-m", "base"], check=True, capture_output=True)
    registered_git = worktree / ".git"
    registration["result_contract"] = {"git_metadata": {
        "worktree_git_dir": str(registered_git), "common_dir": str(registered_git)}}
    clone = tmp_path / "unrelated"
    subprocess.run(["git", "clone", str(worktree), str(clone)], check=True, capture_output=True)
    original_head = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"])
    assert subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"]) == original_head
    registered_git.rename(worktree / "original-git")
    registered_git.write_text("gitdir: " + str(clone / ".git") + "\n")
    with pytest.raises(ExecutionError, match="git_metadata_drift"):
        build((registration, staging, authority))


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_private_inspection_scratch_is_denied_inside_git_write_grant(layout, monkeypatch, name):
    from pathlib import Path
    registration, _, _ = layout
    registration["client"]["name"] = name
    monkeypatch.setattr(agent_client, "_confirm_claude_sandbox", lambda: None)
    argv = build(layout)
    reserved = str(Path(registration["worktree"]) / ".git" / "tpo-inspection")
    if name == "codex":
        config = tomllib.loads(next(arg for arg in argv if arg.startswith("permissions.tpo-worktree=")))
        assert config["permissions"]["tpo-worktree"]["filesystem"][reserved] == "deny"
    else:
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert reserved in settings["sandbox"]["filesystem"]["denyRead"]
        assert reserved in settings["sandbox"]["filesystem"]["denyWrite"]
        assert f"Read(/{reserved}/**)" in settings["permissions"]["deny"]


def test_codex_fixed_argv_preserves_network_and_stdin(layout):
    argv = build(layout)
    assert argv[:2] == ["codex", "exec"]
    assert argv[-1] == "-"
    assert 'approval_policy="never"' in argv
    config = tomllib.loads(next(arg for arg in argv if arg.startswith("permissions.tpo-worktree=")))
    profile = config["permissions"]["tpo-worktree"]
    assert profile["extends"] == ":workspace"
    assert profile["network"] == {"enabled": True}
    assert profile["filesystem"] == {layout[0]["worktree"] + "/.git": "write",
                                     str(layout[1]): "write", str(layout[2]): "deny",
                                     layout[0]["worktree"] + "/.git/tpo-inspection": "deny"}


@pytest.mark.parametrize("target", ["staging", "worktree", "git"])
def test_authority_cannot_be_inside_a_write_grant(layout, target):
    registration, staging, authority = layout
    from pathlib import Path
    parent = {"staging": staging, "worktree": Path(registration["worktree"]),
              "git": Path(registration["worktree"]) / ".git"}[target]
    authority = parent / "private"
    authority.mkdir()
    with pytest.raises(ExecutionError, match="client_authority_overlap"):
        build((registration, staging, authority))


@pytest.mark.parametrize("raw", [b"", b"relative\n", b"/missing-path\n", b"/bad\xff\n"])
def test_git_metadata_must_be_real_absolute_encodable_directory(layout, monkeypatch, raw):
    from pathlib import Path
    bad = Path(os.fsdecode(raw.removesuffix(b"\n")))
    monkeypatch.setattr(agent_client, "metadata_paths", lambda *args: (Path(layout[0]["worktree"]), bad, bad))
    with pytest.raises(ExecutionError, match="git_permissions_unconfirmed"):
        build(layout)


def test_git_metadata_preserves_whitespace_and_toml_characters(layout, monkeypatch, tmp_path):
    from pathlib import Path
    metadata = tmp_path / 'git "quoted" 🌲 \n'
    metadata.mkdir()
    monkeypatch.setattr(agent_client, "metadata_paths", lambda *args: (Path(layout[0]["worktree"]), metadata, metadata))
    layout[0]["result_contract"]["git_metadata"] = {"worktree_git_dir": str(metadata), "common_dir": str(metadata)}
    argv = build(layout)
    config = tomllib.loads(next(arg for arg in argv if arg.startswith("permissions.tpo-worktree=")))
    assert str(metadata) in config["permissions"]["tpo-worktree"]["filesystem"]


def test_legacy_missing_metadata_pin_cannot_launch(layout):
    layout[0]["result_contract"].pop("git_metadata")
    with pytest.raises(ExecutionError, match="git_metadata_unconfirmed"):
        build(layout)


def test_staging_cannot_reopen_private_inspection_directory(layout):
    from pathlib import Path
    staging = Path(layout[0]["worktree"]) / ".git/tpo-inspection/submissions"
    staging.mkdir(parents=True)
    with pytest.raises(ExecutionError, match="client_authority_overlap"):
        build((layout[0], staging, layout[2]))


@pytest.fixture
def claude(layout, monkeypatch):
    layout[0]["client"]["name"] = "claude"
    monkeypatch.setattr(agent_client, "_confirm_claude_sandbox", lambda: None)
    return layout


def test_claude_has_strict_sandbox_and_scoped_native_tools(claude):
    argv = build(claude)
    assert argv[:4] == ["claude", "-p", "--permission-mode", "dontAsk"]
    assert argv[argv.index("--setting-sources") + 1] == ""
    settings = json.loads(argv[argv.index("--settings") + 1])
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"]
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["excludedCommands"] == []
    assert sandbox["filesystem"]["disabled"] is False
    assert str(claude[2]) in sandbox["filesystem"]["denyWrite"]
    assert str(claude[2]) in sandbox["filesystem"]["denyRead"]
    assert sandbox["network"]["allowedDomains"] == ["*"]
    allowed = settings["permissions"]["allow"]
    assert "Bash" in allowed and "Read" in allowed
    assert "Write" not in allowed and "Edit" not in allowed
    assert f"Write(/{claude[1]}/**)" in allowed
    assert settings["disableAllHooks"] is True
    assert "--strict-mcp-config" in argv
    # Keep repository instructions/skills available; they execute only through
    # the bounded tools. Hooks and MCP are disabled separately.
    assert "--safe-mode" not in argv


def test_claude_rejects_permission_pattern_metacharacters(claude, tmp_path):
    staging = tmp_path / "wild*card"
    staging.mkdir()
    with pytest.raises(ExecutionError, match="client_path_unrepresentable"):
        build((claude[0], staging, claude[2]))


@pytest.mark.parametrize("tools", [["Bash;echo unsafe"], ["mcp__custom"], ["UnqualifiedTool"]])
def test_unconfirmed_claude_tools_block(claude, tools):
    claude[0]["client"]["tools"] = tools
    with pytest.raises(ExecutionError, match="invalid_client_tools"):
        build(claude)


def test_claude_missing_dependency_fails_closed(monkeypatch):
    monkeypatch.setattr(agent_client.sys, "platform", "linux")
    monkeypatch.setattr(agent_client.shutil, "which", lambda name: None if name == "socat" else "/bin/" + name)
    with pytest.raises(ExecutionError, match="client_sandbox_unavailable"):
        agent_client._confirm_claude_sandbox()


def test_claude_unknown_version_fails_closed(monkeypatch):
    monkeypatch.setattr(agent_client.sys, "platform", "linux")
    monkeypatch.setattr(agent_client.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(agent_client.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, b"2.0.0 (Claude Code)\n", b""))
    with pytest.raises(ExecutionError, match="client_sandbox_unconfirmed"):
        agent_client._confirm_claude_sandbox()


def test_fake_installed_claude_receives_exact_stdin_and_settings(layout, monkeypatch, tmp_path):
    """A real subprocess boundary, without a model or a sandbox success claim."""
    binary = tmp_path / "bin"
    binary.mkdir()
    claude = binary / "claude"
    claude.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('2.1.267 (Claude Code)')\n"
        "else:\n"
        "    print(json.dumps(sys.argv[1:]), file=sys.stderr)\n"
        "    sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    )
    claude.chmod(0o700)
    for name in ("socat", "bwrap"):
        dependency = binary / name
        dependency.write_text("#!/bin/sh\nexit 0\n")
        dependency.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    layout[0]["client"]["name"] = "claude"
    argv = build(layout)
    prompt = b"exact\x00prompt\n$(not shell)\n\xff"
    result = subprocess.run(argv, cwd=layout[0]["worktree"], input=prompt, capture_output=True, timeout=5, check=True)
    assert result.stdout == prompt
    assert json.loads(result.stderr) == argv[1:]


@pytest.mark.parametrize("name", ["codex", "claude"])
def test_reviewer_can_only_write_response_directory(layout, monkeypatch, name):
    from pathlib import Path
    registration, staging, authority = layout
    monkeypatch.setattr(agent_client, "_confirm_claude_sandbox", lambda: None)
    argv = agent_client.build_review_argv({"name": name}, Path(registration["worktree"]), staging, authority_root=authority)
    if name == "codex":
        config = tomllib.loads(next(arg for arg in argv if arg.startswith("permissions.tpo-review=")))
        profile = config["permissions"]["tpo-review"]
        assert profile["extends"] == ":read-only"
        assert profile["filesystem"] == {str(staging): "write", str(authority): "deny"}
        assert profile["network"]["enabled"] is False
    else:
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings["sandbox"]["filesystem"]["denyWrite"] == [str(authority), registration["worktree"]]
        assert settings["sandbox"]["filesystem"]["allowWrite"] == [str(staging)]
        assert settings["sandbox"]["network"]["allowedDomains"] == []
        assert settings["permissions"]["allow"] == ["Read", f"Write(/{staging}/**)", "Bash", "Glob", "Grep"]
