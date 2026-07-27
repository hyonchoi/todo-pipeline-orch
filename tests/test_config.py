from pathlib import Path

from hermes_pipeline.config import Config


def test_defaults():
    c = Config.default()
    assert c.projects_dir == Path.home() / "projects"
    assert c.state_dir == Path.home() / ".hermes"
    assert c.log_file_subpath == "pipeline.log"
    assert c.log_retention_days == 7
    assert c.slack_channel == ""

def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#alerts")
    c = Config.from_env()
    assert c.projects_dir == tmp_path / "projects"
    assert c.state_dir == tmp_path / "state"
    assert c.slack_channel == "#alerts"
