from pathlib import Path

from hermes_pipeline.config import Config


def test_defaults():
    c = Config.default()
    assert c.projects_dir == Path.home() / "projects"
    assert c.state_dir == Path.home() / ".hermes"
    assert c.log_file_subpath == "pipeline.log"
    assert c.log_retention_days == 7
    assert c.slack_channel == ""

def test_pipeline_env_vars_do_not_override_config(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"projects_dir: {tmp_path / 'from-file'}\n"
        f"state_dir: {tmp_path / 'state-from-file'}\n"
        "slack_channel: '#from-file'\n"
    )
    monkeypatch.setenv("TPO_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("PIPELINE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PIPELINE_SLACK_CHANNEL", "#alerts")
    c = Config.from_env()
    assert c.projects_dir == tmp_path / "from-file"
    assert c.state_dir == tmp_path / "state-from-file"
    assert c.slack_channel == "#from-file"
