"""Provider-free launch configuration and authority-boundary regressions."""


import json
import os
import subprocess

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
    return agent_client.build_client_argv(registration, staging)


@pytest.mark.parametrize("profile_name,client_name", [
    ("native-sdd", "codex"), ("superpowers", "claude"), (None, "claude"),
])
def test_preparation_does_not_grant_native_claude_delegation_to_other_contexts(profile_name, client_name):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    from hermes_pipeline.phases import resolve_profile_phases_path

    prepared = prepare_todo_phases(
        todo_id="TODO-41", tick_id="tick", board_slug="demo",
        phases_path=resolve_profile_phases_path("native-sdd"),
        plan_path="plan.md", profile_name=profile_name, prompt_client=client_name,
    )
    assert all("Agent" not in phase.tools.split(",") for phase in prepared)


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


@pytest.mark.parametrize("raw", [b"", b"relative\n", b"/missing-path\n", b"/bad\xff\n"])
def test_git_metadata_must_be_real_absolute_encodable_directory(layout, monkeypatch, raw):
    from pathlib import Path
    bad = Path(os.fsdecode(raw.removesuffix(b"\n")))
    monkeypatch.setattr(agent_client, "metadata_paths", lambda *args: (Path(layout[0]["worktree"]), bad, bad))
    with pytest.raises(ExecutionError, match="git_permissions_unconfirmed"):
        build(layout)


def test_legacy_missing_metadata_pin_cannot_launch(layout):
    layout[0]["result_contract"].pop("git_metadata")
    with pytest.raises(ExecutionError, match="git_metadata_unconfirmed"):
        build(layout)


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_direct_client_has_no_sandbox_prerequisites(layout, monkeypatch, name):
    import shutil
    layout[0]["client"]["name"] = name
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = build(layout)
    expected = "--dangerously-skip-permissions" if name == "claude" else "--dangerously-bypass-approvals-and-sandbox"
    assert expected in argv
    assert not any("permissions.tpo" in arg for arg in argv)
    if name == "claude":
        assert argv[argv.index("--tools") + 1] == "Read,Write,Edit,Bash"
        assert "sandbox" not in json.loads(argv[argv.index("--settings") + 1])


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_installed_client_boundary_preserves_prompt_bytes(layout, monkeypatch, tmp_path, name):
    import sys
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / name
    executable.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]),file=sys.stderr)\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary))
    layout[0]["client"]["name"] = name
    argv = build(layout)
    prompt = b"exact\x00prompt\n$(not shell)\n\xff"
    result = subprocess.run(argv, input=prompt, capture_output=True, timeout=5, check=True)
    assert result.stdout == prompt
    assert json.loads(result.stderr) == argv[1:]


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_reviewer_is_fresh_direct_client(layout, name):
    from pathlib import Path
    registration, staging, authority = layout
    argv = agent_client.build_review_argv({"name": name}, Path(registration["worktree"]), staging)
    if name == "codex":
        assert argv == ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--ephemeral", "-"]
    else:
        assert "--dangerously-skip-permissions" in argv
        assert argv[argv.index("--tools") + 1] == "Read,Write,Bash,Glob,Grep"


@pytest.mark.parametrize("tools", [["Bash;echo unsafe"], ["mcp__custom"], ["UnqualifiedTool"]])
def test_unconfirmed_claude_tools_block(layout, tools):
    layout[0]["client"] = {"name": "claude", "tools": tools}
    with pytest.raises(ExecutionError, match="invalid_client_tools"):
        build(layout)


def test_native_profile_retains_configured_claude_agent_tool(layout):
    from hermes_pipeline.kanban_tasks import prepare_todo_phases
    from hermes_pipeline.phases import resolve_profile_phases_path

    prepared = prepare_todo_phases(
        todo_id="TODO-41", tick_id="tick", board_slug="demo",
        phases_path=resolve_profile_phases_path("native-sdd"),
        plan_path="plan.md", profile_name="native-sdd", prompt_client="claude",
    )
    development = next(phase for phase in prepared if phase.phase_key == "phase_4_development")
    layout[0]["client"] = {"name": "claude", "tools": development.tools.split(",")}
    argv = build(layout)
    assert "Agent" in argv[argv.index("--tools") + 1].split(",")
    assert all("Agent" not in phase.tools.split(",") for phase in prepared if phase is not development)
