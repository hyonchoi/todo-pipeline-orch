"""Exercise repository fixture guarantees in fresh, bounded pytest processes."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _child_pytest(tmp_path, source, extra_env=None):
    test = tmp_path / "test_child.py"
    test.write_text(textwrap.dedent(source))
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("PYTEST", "COV_CORE", "COVERAGE_"))
    }
    env.update(extra_env or {})
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-o", "addopts=", "-q", str(test)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


def test_real_git_fixtures_ignore_hostile_ambient_configuration(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    hook.chmod(0o700)
    template = tmp_path / "template"
    template.mkdir()
    (template / "ambient-template").write_text("unexpected")
    config = tmp_path / "gitconfig"
    config.write_text(f"[commit]\n gpgSign = true\n[core]\n hooksPath = {hooks}\n[init]\n defaultBranch = hostile\n")
    result = _child_pytest(tmp_path, r'''
        import os
        from tests.support.git import init_repo, run_git
        from hermes_pipeline.github_issues import repository_identity

        def test_real_fixture(tmp_path, monkeypatch):
            repo, sha = init_repo(tmp_path / "repo", branch="main", files={"a": "base"},
                                  origin="https://example.invalid/base.git")
            assert sha and run_git(repo, "branch", "--show-current") == "main"
            assert run_git(repo, "show", "-s", "--format=%an <%ae>") == "Test <test@example.com>"
            assert not (repo / ".git" / "ambient-template").exists()
            assert "GIT_DIR" not in os.environ and "GIT_INDEX_FILE" not in os.environ
            assert os.environ["GIT_TERMINAL_PROMPT"] == "0"
            config = tmp_path / "explicit-config"
            config.write_text('[url "https://github.com/local/override.git"]\n insteadOf = https://example.invalid/base.git\n')
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
            assert repository_identity(repo) == "local/override"
    ''', {
        "GIT_DIR": str(tmp_path / "missing-git-dir"),
        "GIT_WORK_TREE": str(tmp_path / "wrong-work-tree"),
        "GIT_INDEX_FILE": str(tmp_path / "wrong-index"),
        "GIT_CONFIG_SYSTEM": str(config), "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "0", "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks),
        "GIT_AUTHOR_NAME": "Ambient", "GIT_AUTHOR_EMAIL": "ambient@example.com",
        "GIT_TEMPLATE_DIR": str(template), "GIT_TERMINAL_PROMPT": "1",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("fixture", ["fake_gh", "fake_gh_factory"])
def test_unmatched_fake_calls_fail_even_when_caught(tmp_path, fixture):
    result = _child_pytest(tmp_path, f'''
        import os
        from hermes_pipeline import github_issues

        def test_swallowed({fixture}, tmp_path, monkeypatch):
            fake = {"fake_gh_factory()" if fixture == "fake_gh_factory" else "fake_gh"}
            monkeypatch.setattr(github_issues, "_run", fake)
            try:
                github_issues.add_comment(tmp_path, 7, os.environ["PRIVATE_PAYLOAD"], repo="acme/repo")
            except Exception:
                pass
    ''', {"PRIVATE_PAYLOAD": "private-payload-should-not-appear"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "unexpected FakeGh call" in result.stdout
    assert "1 passed, 1 error" in result.stdout
    assert "private-payload-should-not-appear" not in result.stdout + result.stderr



@pytest.mark.parametrize("fixture", ["fake_gh", "fake_gh_factory"])
def test_uncaught_fake_calls_reject_without_payload_diagnostics(tmp_path, fixture):
    result = _child_pytest(tmp_path, f'''
        import os

        def test_unexpected({fixture}):
            fake = {"fake_gh_factory()" if fixture == "fake_gh_factory" else "fake_gh"}
            fake.on("gh", "allowed", stdout=os.environ["PRIVATE_PAYLOAD"])
            fake(["gh", os.environ["PRIVATE_PAYLOAD"]], input=os.environ["PRIVATE_PAYLOAD"])
    ''', {"PRIVATE_PAYLOAD": "private-payload-should-not-appear"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "unexpected FakeGh call" in result.stdout
    assert "1 failed, 1 error" in result.stdout
    assert "private-payload-should-not-appear" not in result.stdout + result.stderr

def test_fake_matching_precedence_and_explicit_failure(fake_gh_factory):
    fake = fake_gh_factory().on("gh", rc=1).on("gh", "api", stdout="first")
    fake.on("gh", "api", stdout="override")
    assert fake(["gh", "api", "user"]).stdout == "override"
    assert fake(["gh", "auth", "status"]).returncode == 1
    assert fake.calls == [["gh", "api", "user"], ["gh", "auth", "status"]]


def test_ruff_preserves_default_exclusions_and_source_discovery(tmp_path):
    (tmp_path / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    for name in (".git/hook.py", ".venv/local.py", "docs/old-code/legacy.py", "ordinary.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n")
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--show-files", "."],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert {Path(line).relative_to(tmp_path).as_posix() for line in result.stdout.splitlines()} == {"ordinary.py", "pyproject.toml"}
