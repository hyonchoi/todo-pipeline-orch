"""Bounded installed-client configuration; prompts are delivered separately on stdin.

The installed clients and administrator policy are trusted. Claude's Bash
sandbox and native file-tool permissions are separate enforcement layers;
neither is a sandbox around a malicious replacement client executable.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from .agent_execution import ExecutionError, _no_symlinks
from .agent_git import inspection_root, metadata_paths

# A new client release needs explicit qualification of these security settings.
# Unknown versions must not silently ignore a required sandbox setting.
_CLAUDE_SANDBOX_VERSIONS = {(2, 1, 267)}
_CLAUDE_TOOLS = frozenset({"Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite", "Agent"})


def _confirm_claude_sandbox() -> None:
    if sys.platform.startswith("linux"):
        if any(shutil.which(name) is None for name in ("bwrap", "socat")):
            raise ExecutionError("client_sandbox_unavailable")
    elif sys.platform != "darwin":
        raise ExecutionError("client_sandbox_unavailable")
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, timeout=10, check=False)
        match = re.fullmatch(rb"(\d+)\.(\d+)\.(\d+) \(Claude Code\)\n?", result.stdout)
        if result.returncode or match is None or tuple(map(int, match.groups())) not in _CLAUDE_SANDBOX_VERSIONS:
            raise ExecutionError("client_sandbox_unconfirmed")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionError("client_sandbox_unconfirmed") from exc


def _directory(path: Path, *, reason: str) -> Path:
    try:
        # Do not normalize before checking: that could hide a symlink traversal.
        str(path).encode("utf-8")
        if not path.is_absolute() or not path.is_dir():
            raise ExecutionError(reason)
        _no_symlinks(path)
        return path.resolve(strict=True)
    except (OSError, ValueError, UnicodeError, ExecutionError) as exc:
        raise ExecutionError(reason) from exc


def git_metadata_identity(worktree: Path) -> dict[str, str]:
    """Capture actual metadata locations without reading executable Git config."""
    worktree = _directory(worktree, reason="git_permissions_unconfirmed")
    try:
        tree, directory, common = metadata_paths(worktree)
        if _directory(tree, reason="git_permissions_unconfirmed") != worktree.resolve():
            raise ExecutionError("git_permissions_unconfirmed")
        return {
            "worktree_git_dir": str(_directory(directory, reason="git_permissions_unconfirmed")),
            "common_dir": str(_directory(common, reason="git_permissions_unconfirmed")),
        }
    except (OSError, UnicodeError, ValueError) as exc:
        raise ExecutionError("git_permissions_unconfirmed") from exc


def validate_git_metadata(registration: dict) -> dict[str, str]:
    pinned = registration.get("result_contract", {}).get("git_metadata")
    if not isinstance(pinned, dict) or set(pinned) != {"worktree_git_dir", "common_dir"}:
        raise ExecutionError("git_metadata_unconfirmed")
    actual = git_metadata_identity(Path(registration["worktree"]))
    if pinned != actual:
        raise ExecutionError("git_metadata_drift")
    return actual


def _write_directories(registration: dict, staging: Path, authority_root: Path) -> tuple[Path, list[Path], Path, Path]:
    worktree = _directory(Path(registration["worktree"]), reason="client_path_unconfirmed")
    authority = _directory(authority_root, reason="client_path_unconfirmed")
    metadata = validate_git_metadata(registration)
    directories = [Path(metadata["common_dir"]), Path(metadata["worktree_git_dir"])]
    reserved = inspection_root(Path(metadata["common_dir"]))
    staging = _directory(staging, reason="client_path_unconfirmed")
    directories.append(staging)
    if staging.is_relative_to(reserved) or reserved.is_relative_to(staging):
        raise ExecutionError("client_authority_overlap")
    for directory in [worktree, *directories]:
        if authority.is_relative_to(directory) or directory.is_relative_to(authority):
            raise ExecutionError("client_authority_overlap")
    return worktree, list(dict.fromkeys(directories)), authority, reserved


def _claude_path(path: Path) -> str:
    # Native tool rules are glob-like syntax, not JSON path literals. Reject
    # unrepresentable names rather than accidentally expanding their authority.
    value = str(path)
    if any(character in value for character in "*?[]{}()\\") or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ExecutionError("client_path_unrepresentable")
    return "/" + value + "/**"


def build_client_argv(registration: dict, staging: Path, *, authority_root: Path) -> list[str]:
    """Construct only the pinned supported client, with no shell interpolation."""
    worktree, directories, authority, reserved = _write_directories(registration, staging, authority_root)
    return _render_argv(registration["client"], worktree, directories, authority, staging,
                        read_only=False, private_paths=[reserved])


def build_review_argv(client: dict, snapshot: Path, staging: Path, *, authority_root: Path,
                      private_paths: list[Path] | None = None) -> list[str]:
    """A fresh reviewer may only write its separate response directory."""
    worktree = _directory(snapshot, reason="client_path_unconfirmed")
    staging = _directory(staging, reason="client_path_unconfirmed")
    authority = _directory(authority_root, reason="client_path_unconfirmed")
    paths = [worktree, staging, authority]
    if any(left.is_relative_to(right) or right.is_relative_to(left)
           for index, left in enumerate(paths) for right in paths[index + 1:]):
        raise ExecutionError("client_authority_overlap")
    review_client = {"name": client["name"], "tools": ["Read", "Write", "Bash", "Glob", "Grep"]}
    for path in private_paths or []:
        if not path.is_absolute() or staging.is_relative_to(path) or path.is_relative_to(staging):
            raise ExecutionError("client_authority_overlap")
    return _render_argv(review_client, worktree, [staging], authority, staging,
                        read_only=True, private_paths=private_paths)


def _render_argv(client: dict, worktree: Path, directories: list[Path], authority: Path,
                 staging: Path, *, read_only: bool, private_paths: list[Path] | None = None) -> list[str]:
    denied = [authority, *(private_paths or [])]
    if client["name"] == "codex":
        # Filesystem keys also accept globs; JSON/TOML quoting alone cannot
        # turn a metacharacter into a literal path in that second grammar.
        if any(any(character in str(path) for character in "*?[]{}\\") for path in [*directories, *denied, worktree]):
            raise ExecutionError("client_path_unrepresentable")

        def quoted(path: Path) -> str:
            return json.dumps(str(path), ensure_ascii=False).replace("\x7f", "\\u007f")

        grants = ",".join(quoted(directory) + '="write"' for directory in directories)
        grants += "".join("," + quoted(path) + '="deny"' for path in denied)
        name = "tpo-review" if read_only else "tpo-worktree"
        parent = ":read-only" if read_only else ":workspace"
        network = "false" if read_only else "true"
        override = 'permissions.' + name + '={extends="' + parent + '",filesystem={' + grants + '},network={enabled=' + network + '}}'
        tomllib.loads(override)
        argv = ["codex", "exec", "-c", 'approval_policy="never"', "-c", 'default_permissions="' + name + '"', "-c", override]
        if read_only:
            argv += ["--skip-git-repo-check", "--ephemeral"]
        return [*argv, "-"]
    if client["name"] != "claude":
        raise ExecutionError("invalid_client")
    requested = client["tools"] or sorted(_CLAUDE_TOOLS - {"Agent"})
    if any(tool not in _CLAUDE_TOOLS for tool in requested):
        raise ExecutionError("invalid_client_tools")
    paths = [_claude_path(path) for path in (directories if read_only else [worktree, *directories])]
    private = [_claude_path(path) for path in denied]
    permissions = []
    for tool in requested:
        if tool in {"Write", "Edit"}:
            permissions.extend(f"{tool}({path})" for path in paths)
        else:
            permissions.append(tool)
    _confirm_claude_sandbox()
    settings = {
        "disableAllHooks": True,
        "permissions": {
            "allow": permissions,
            "deny": [f"{tool}({path})" for path in private for tool in ("Read", "Write", "Edit")],
        },
        "sandbox": {
            "enabled": True, "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False, "excludedCommands": [],
            "enableWeakerNestedSandbox": False,
            "filesystem": {
                "disabled": False,
                "allowWrite": [str(path) for path in directories],
                "denyWrite": [str(path) for path in denied] + ([str(worktree)] if read_only else []),
                "denyRead": [str(path) for path in denied],
            },
            "network": {"allowedDomains": [] if read_only else ["*"], "strictAllowlist": read_only,
                        "allowAllUnixSockets": False},
        },
    }
    return ["claude", "-p", "--permission-mode", "dontAsk", "--setting-sources", "",
            "--settings", json.dumps(settings, ensure_ascii=True, separators=(",", ":")),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "mcp__*", "--tools", ",".join(requested),
            "--add-dir", str(staging)]
