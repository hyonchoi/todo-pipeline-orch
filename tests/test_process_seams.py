"""Tests for late-bound subprocess seams (subcutaneous-seams refactor).

Validates that _run wrappers in kanban_tasks, ship, todos_completion, and
review_reconciliation route subprocess calls through module-level late-bound
wrappers that preserve patching of subprocess.run in existing tests.
"""
from __future__ import annotations

import types
import unittest.mock
from collections.abc import Callable

from hermes_pipeline import (
    kanban_tasks,
    review_reconciliation,
    ship,
    todos_completion,
)


def forbid_real_subprocess(monkeypatch):
    """Patch subprocess.run globally to fail if reached (not mocked)."""
    def _raise(*args, **kwargs):
        raise AssertionError("real subprocess.run reached (missing mock)")
    monkeypatch.setattr("subprocess.run", _raise)


class CallRecorder:
    """Callable that records (argv, kwargs) and returns a synthetic result."""

    def __init__(self, stdout: str = "", *, dispatcher: Callable[[list], str] | None = None):
        self.calls = []
        self.stdout = stdout
        self.dispatcher = dispatcher

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        stdout = self.dispatcher(argv) if self.dispatcher else self.stdout
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


# Test a: kanban_tasks.get_todo_kanban_tasks routes through _run with late binding.
def test_kanban_get_todo_kanban_tasks_uses_seam(monkeypatch, tmp_path):
    """kanban_tasks.get_todo_kanban_tasks calls hermes kanban list via _run."""
    forbid_real_subprocess(monkeypatch)
    recorder = CallRecorder(stdout="[]")
    monkeypatch.setattr(kanban_tasks, "_run", recorder)

    result = kanban_tasks.get_todo_kanban_tasks("acme", "tick-1")

    assert len(recorder.calls) == 1
    argv, kwargs = recorder.calls[0]
    assert argv == ["hermes", "kanban", "list", "--tenant", "acme", "--json"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == kanban_tasks.HERMES_COMMAND_TIMEOUT
    assert result == {}


# Test b: ship.git_tree_clean routes through _run with late binding.
def test_ship_git_tree_clean_uses_seam(monkeypatch, tmp_path):
    """ship.git_tree_clean calls git status via _run."""
    forbid_real_subprocess(monkeypatch)
    recorder = CallRecorder(stdout="")
    monkeypatch.setattr(ship, "_run", recorder)

    result = ship.git_tree_clean(tmp_path)

    assert len(recorder.calls) == 1
    argv, kwargs = recorder.calls[0]
    assert argv == ["git", "status", "--porcelain"]
    assert kwargs.get("cwd") == str(tmp_path)
    assert kwargs.get("timeout") == ship.GIT_TIMEOUT
    assert result is True


# Test c: todos_completion._remote_head routes through _run with late binding.
def test_todos_completion_remote_head_uses_seam(monkeypatch, tmp_path):
    """todos_completion._remote_head calls git ls-remote via _run."""
    forbid_real_subprocess(monkeypatch)
    recorder = CallRecorder(stdout="abc123\trefs/heads/feature\n")
    monkeypatch.setattr(todos_completion, "_run", recorder)

    result = todos_completion._remote_head(tmp_path, "feature")

    assert len(recorder.calls) == 1
    argv, kwargs = recorder.calls[0]
    assert argv == ["git", "ls-remote", "--heads", "origin", "refs/heads/feature"]
    assert kwargs.get("cwd") == tmp_path
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == 60
    assert result == "abc123"


# Test d: review_reconciliation._create_task routes hermes kanban create through _run.
def test_review_reconciliation_create_task_uses_seam(monkeypatch, tmp_path):
    """review_reconciliation._create_task calls hermes kanban create via _run."""
    forbid_real_subprocess(monkeypatch)

    # Dispatcher: "list" argv -> "[]" (no existing tasks), "create" argv -> JSON with task id
    def dispatcher(argv):
        if "list" in argv:
            return "[]"
        elif "create" in argv:
            return '{"id": "t_abcdef01"}'
        return ""

    review_recorder = CallRecorder(dispatcher=dispatcher)
    kanban_recorder = CallRecorder(dispatcher=dispatcher)

    monkeypatch.setattr(review_reconciliation, "_run", review_recorder)
    monkeypatch.setattr(kanban_tasks, "_run", kanban_recorder)

    # Create .hermes/runs directory so _persist_pending_create can write
    (tmp_path / ".hermes" / "runs" / "tick-1").mkdir(parents=True)

    # Mock only the two doubles used in existing tests
    monkeypatch.setattr(
        "hermes_pipeline._agent_supervisor.register_execution",
        lambda **kw: "exec_id_001"
    )

    mock_store = types.SimpleNamespace(
        load=lambda id: {
            "registration": {"timeout": 1800, "manifest": None}
        }
    )
    monkeypatch.setattr(
        "hermes_pipeline.agent_execution.ExecutionStore",
        lambda root: mock_store
    )

    # Call _create_task
    task_id = review_reconciliation._create_task(
        project_dir=tmp_path,
        tenant="acme",
        tick_id="tick-1",
        todo_id="TODO-1",
        key="phase_5_review",
        title="Review this",
        prompt="Please review",
        result_template="Result: {result}",
        worktree=tmp_path,
        assignee=None,
        prompt_client="claude",
        tools="",
        turns=1,
        timeout=10,
    )

    # Verify review_reconciliation._run was called with create command
    assert len(review_recorder.calls) >= 1
    create_calls = [call for call in review_recorder.calls if "create" in call[0]]
    assert len(create_calls) >= 1
    argv, kwargs = create_calls[0]
    assert argv[0:3] == ["hermes", "kanban", "create"]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == kanban_tasks.KANBAN_QUERY_TIMEOUT
    assert kwargs.get("check") is False
    assert task_id == "t_abcdef01"


# Test e: compatibility -- existing global subprocess.run patch still works (late binding).
def test_compatibility_global_subprocess_patch_still_works(tmp_path):
    """With only global patch("subprocess.run"), late binding intercepts via module's lookup."""
    with unittest.mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        ship.git_tree_clean(tmp_path)
        # If _run is late-bound, it looks up subprocess.run at call time
        # and finds the mock, so it should have been called.
        assert mock_run.called, "global subprocess.run patch was not intercepted"
