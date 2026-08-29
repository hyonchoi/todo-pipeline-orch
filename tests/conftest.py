import pytest


@pytest.fixture
def tmp_project(tmp_path):
    """A scratch project dir marked by its .hermes/pipeline.toml contract."""
    proj = tmp_path / "demo"
    (proj / ".hermes").mkdir(parents=True)
    (proj / ".hermes" / "pipeline.toml").write_text(
        'schema_version = 2\nassignee = "default"\n'
        'capabilities = ["Read", "Write", "Edit", "Bash"]\n'
    )
    (proj / ".hermes" / "todo_id_counter").write_text("0")
    return proj

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A scratch ~/.hermes/ replacement."""
    sd = tmp_path / "state"
    (sd / "pipeline_locks").mkdir(parents=True)
    return sd


@pytest.fixture
def fake_gh(monkeypatch):
    """Patch ``hermes_pipeline.github_issues._run`` (its subprocess seam) with a FakeGh recorder."""
    from tests.gh_fakes import FakeGh

    monkeypatch.delenv("TPO_GH_BIN", raising=False)
    fake = FakeGh()
    from hermes_pipeline import github_issues

    monkeypatch.setattr(github_issues, "_run", fake)
    return fake
