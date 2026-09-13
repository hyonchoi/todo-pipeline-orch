"""Tests for support/git.py helpers."""
import subprocess
from pathlib import Path

import pytest

from tests.support.git import init_repo, run_git, run_git_cp


def test_run_git_strips_stdout_and_honors_errors(tmp_path: Path):
    """run_git strips stdout and forwards ``errors`` to the text decoder."""
    repo, _ = init_repo(tmp_path / "repo", files={"file.txt": "content"})

    result = run_git(repo, "rev-parse", "HEAD")
    assert result == result.strip()
    assert not result.startswith("\n") and not result.endswith("\n")

    # A committed blob holding a raw invalid UTF-8 byte reaches stdout verbatim via ``git show``.
    (repo / "bad.bin").write_bytes(b"bad-\xff")
    run_git(repo, "add", "bad.bin")
    run_git(repo, "commit", "-qm", "add bad blob")
    assert run_git(repo, "show", "HEAD:bad.bin", errors="surrogateescape") == "bad-\udcff"
    with pytest.raises(UnicodeDecodeError):
        run_git(repo, "show", "HEAD:bad.bin")



def test_run_git_isolated_env_isolates_config(tmp_path: Path, monkeypatch):
    """isolated_env=True isolates git config from the environment."""
    config_file = tmp_path / "gitconfig"
    config_file.write_text("[user]\n    name = Ambient\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_file))

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")

    result = run_git(repo, "config", "--get", "user.name")
    assert result == "Ambient"

    with pytest.raises(subprocess.CalledProcessError):
        run_git(repo, "config", "--get", "user.name", isolated_env=True)


def test_run_git_cp_returns_bytes(tmp_path: Path):
    """run_git_cp returns subprocess.CompletedProcess with bytes."""
    repo, _ = init_repo(tmp_path / "repo", files={"file.txt": "content"})

    result = run_git_cp(repo, "rev-parse", "HEAD")
    assert isinstance(result, subprocess.CompletedProcess)
    assert isinstance(result.stdout, bytes)
    assert isinstance(result.stderr, bytes)
    assert result.returncode == 0


def test_init_repo_returns_sha_and_honors_params(tmp_path: Path):
    """init_repo returns correct sha, honors branch and origin, returns None when no files."""
    repo1, sha1 = init_repo(tmp_path / "repo1", branch="main", files={"file.txt": "content"})
    assert repo1 == tmp_path / "repo1"
    assert sha1 is not None
    assert len(sha1) == 40

    head_sha = run_git(repo1, "rev-parse", "HEAD")
    assert sha1 == head_sha

    current_branch = run_git(repo1, "branch", "--show-current")
    assert current_branch == "main"

    repo2, sha2 = init_repo(tmp_path / "repo2", branch="develop")
    assert repo2 == tmp_path / "repo2"
    assert sha2 is None

    repo3, sha3 = init_repo(
        tmp_path / "repo3",
        branch="main",
        files={"plan.md": "# Plan\n"},
        origin="https://github.com/acme/repo.git"
    )
    assert sha3 is not None
    origin = run_git(repo3, "remote", "get-url", "origin")
    assert origin == "https://github.com/acme/repo.git"
