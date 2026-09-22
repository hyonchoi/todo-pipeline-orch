import os

import pytest

from tests.gh_fakes import seed_project_issues, todo_payload
from tests.support.projects import make_project


@pytest.fixture(autouse=True)
def isolated_git_environment(tmp_path, monkeypatch):
    """Ignore developer Git state while allowing explicit test-local overrides."""
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    template = tmp_path / "empty-git-template"
    template.mkdir()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))


@pytest.fixture
def fake_gh_factory():
    """Track every fake, including unexpected calls caught by application code."""
    from tests.gh_fakes import FakeGh

    instances = []

    def create():
        fake = FakeGh()
        instances.append(fake)
        return fake

    yield create
    unexpected = sum(fake.unmatched_calls for fake in instances)
    assert unexpected == 0, f"{unexpected} unexpected FakeGh call(s); configure explicit rules"


@pytest.fixture
def tmp_project(tmp_path):
    """A scratch project dir marked by its .hermes/pipeline.toml contract."""
    return make_project(tmp_path, "demo")

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A scratch ~/.hermes/ replacement."""
    sd = tmp_path / "state"
    (sd / "pipeline_locks").mkdir(parents=True)
    return sd


@pytest.fixture
def fake_gh(monkeypatch, fake_gh_factory):
    """Patch ``hermes_pipeline.github_issues._run`` (its subprocess seam) with a FakeGh recorder."""
    monkeypatch.delenv("TPO_GH_BIN", raising=False)
    fake = fake_gh_factory()
    # ``check_auth`` verifies the gh version after auth; serve a supported one by default.
    fake.on("gh", "--version", stdout="gh version 2.60.0 (2025-01-01)\nhttps://github.com/cli/cli/releases/tag/v2.60.0\n")
    from hermes_pipeline import github_issues

    monkeypatch.setattr(github_issues, "_run", fake)
    return fake


@pytest.fixture
def github_todo(fake_gh):
    """Factory for seeding GitHub issues with todo_payload."""
    def seed(number=10, title="test"):
        return seed_project_issues(fake_gh, [todo_payload(number, title=title)])

    return seed
