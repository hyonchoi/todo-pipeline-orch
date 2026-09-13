"""Shared helpers for supervisor testing."""

import subprocess
import sys
from pathlib import Path

from hermes_pipeline import _agent_supervisor as supervisor
from hermes_pipeline import agent_authority
from hermes_pipeline.agent_execution import ExecutionStore


def committed_profile(tmp_path, monkeypatch):
    """Create a test supervisor execution with a committed worktree.

    Returns (ExecutionStore, identity, worktree).
    """
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
