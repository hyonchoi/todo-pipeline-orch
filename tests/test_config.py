import os
from pathlib import Path
from hermes_pipeline.config import Config

def test_defaults():
    c = Config.default()
    assert c.lock_dir == Path.home() / ".hermes" / "pipeline_locks"
    assert c.projects_dir == Path.home() / "projects"
    assert c.claude_cmd == "claude"
    assert c.kanban_adapter == "null"

def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "projs"))
    monkeypatch.setenv("PIPELINE_CLAUDE_CMD", "/usr/bin/claude")
    monkeypatch.setenv("PIPELINE_KANBAN_ADAPTER", "hermes")
    c = Config.from_env()
    assert c.lock_dir == tmp_path / "locks"
    assert c.projects_dir == tmp_path / "projs"
    assert c.claude_cmd == "/usr/bin/claude"
    assert c.kanban_adapter == "hermes"
