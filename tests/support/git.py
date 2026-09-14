"""Shared git helpers for test fixtures."""
import os
import subprocess
from pathlib import Path


def run_git(cwd: Path, *args: str, errors: str | None = None, isolated_env: bool = False) -> str:
    """Run git with isolated environment (if requested) and return stdout stripped.

    Args:
        cwd: Working directory for git command
        *args: Git arguments
        errors: Error handling strategy for text encoding (e.g., "surrogateescape")
        isolated_env: If True, use isolated environment with GIT_TERMINAL_PROMPT=0 and GIT_CONFIG_GLOBAL=/dev/null

    Returns:
        Stripped stdout of git command
    """
    env = None
    if isolated_env:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null"}

    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        errors=errors,
        env=env,
    )
    return result.stdout.strip()


def run_git_cp(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run git and return CompletedProcess with bytes output (not stripped)."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
    )


def init_repo(
    path: Path,
    *,
    branch: str | None = None,
    files: dict[str, str] | None = None,
    origin: str | None = None,
) -> tuple[Path, str | None]:
    """Initialize a git repository with optional files and remote.

    Args:
        path: Repository directory (created with mkdir -p)
        branch: Initial branch name (if None, use git default)
        files: Dict mapping relative paths to content; creates commits if provided
        origin: Remote URL to add as origin

    Returns:
        Tuple of (path, commit_sha or None). sha is None when files=None or files={}.
    """
    path.mkdir(parents=True, exist_ok=True)

    git_args = ["init", "-q"]
    if branch:
        git_args.extend(["-b", branch])
    run_git(path, *git_args)

    run_git(path, "config", "user.email", "test@example.com")
    run_git(path, "config", "user.name", "Test")

    sha = None
    if files:
        for rel, content in files.items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        run_git(path, "add", ".")
        run_git(path, "commit", "-qm", "base")
        sha = run_git(path, "rev-parse", "HEAD")

    if origin:
        run_git(path, "remote", "add", "origin", origin)

    return path, sha


def make_bare_remote(tmp_path: Path, files: dict[str, str], *, branch: str = "main") -> Path:
    """Bare remote on *branch* tracking *files*; no commits at all when *files* is empty."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    run_git(bare, "init", "--bare", "-b", branch, isolated_env=True)
    if files:
        push_files(bare, tmp_path / "seed", files, branch=branch)
    return bare


def push_files(bare: Path, work: Path, files: dict[str, str], *, branch: str, subject: str = "seed") -> str:
    """Init *work*, commit *files* on *branch*, push to *bare*; return the commit sha."""
    work.mkdir()
    run_git(work, "init", "-b", branch, isolated_env=True)
    run_git(work, "config", "user.email", "seed@localhost", isolated_env=True)
    run_git(work, "config", "user.name", "Seed", isolated_env=True)
    for rel, content in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    run_git(work, "add", ".", isolated_env=True)
    run_git(work, "commit", "-m", subject, isolated_env=True)
    run_git(work, "remote", "add", "origin", f"file://{bare}", isolated_env=True)
    run_git(work, "push", "origin", branch, isolated_env=True)
    return run_git(work, "rev-parse", "HEAD", isolated_env=True)


def advance_remote(bare: Path, tmp_path: Path, branch: str) -> str:
    """Add one commit on *branch* of *bare* from a fresh clone; return the new tip."""
    work = tmp_path / "advance"
    run_git(tmp_path, "clone", "-b", branch, f"file://{bare}", str(work), isolated_env=True)
    run_git(work, "config", "user.email", "other@localhost", isolated_env=True)
    run_git(work, "config", "user.name", "Other", isolated_env=True)
    (work / "RACE.txt").write_text("racing commit\n")
    run_git(work, "add", "RACE.txt", isolated_env=True)
    run_git(work, "commit", "-m", "race", isolated_env=True)
    run_git(work, "push", "origin", branch, isolated_env=True)
    return run_git(work, "rev-parse", "HEAD", isolated_env=True)


def remote_branches(bare: Path) -> list[str]:
    """List all branches in bare remote."""
    return run_git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads", isolated_env=True).splitlines()


def remote_tree(bare: Path, branch: str) -> dict[str, str]:
    """Get all files in bare remote at branch."""
    names = run_git(bare, "ls-tree", "-r", "--name-only", branch, isolated_env=True).splitlines()
    return {n: run_git(bare, "show", f"{branch}:{n}", isolated_env=True) for n in names}


