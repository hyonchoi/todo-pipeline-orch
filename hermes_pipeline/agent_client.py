"""Direct installed-client commands; prompts are delivered separately on stdin."""
from __future__ import annotations

import json
from pathlib import Path

from .agent_execution import ExecutionError, _no_symlinks
from .agent_git import metadata_paths

_CLAUDE_TOOLS = frozenset({"Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite", "Agent"})


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


def build_client_argv(registration: dict, staging: Path) -> list[str]:
    """Construct the pinned client directly, without shell interpolation."""
    validate_git_metadata(registration)
    _directory(staging, reason="client_path_unconfirmed")
    return _render_argv(registration["client"], review=False)


def build_review_argv(client: dict, snapshot: Path, staging: Path) -> list[str]:
    """Launch a fresh reviewer; its prompt defines the review-only task."""
    _directory(snapshot, reason="client_path_unconfirmed")
    _directory(staging, reason="client_path_unconfirmed")
    review_client = {"name": client["name"], "tools": ["Read", "Write", "Bash", "Glob", "Grep"]}
    return _render_argv(review_client, review=True)


def _render_argv(client: dict, *, review: bool) -> list[str]:
    if client["name"] == "codex":
        argv = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
        if review:
            argv += ["--skip-git-repo-check", "--ephemeral"]
        return [*argv, "-"]
    if client["name"] != "claude":
        raise ExecutionError("invalid_client")
    requested = client["tools"] or sorted(_CLAUDE_TOOLS - {"Agent"})
    if any(tool not in _CLAUDE_TOOLS for tool in requested):
        raise ExecutionError("invalid_client_tools")
    return ["claude", "-p", "--dangerously-skip-permissions", "--setting-sources", "",
            "--settings", json.dumps({"disableAllHooks": True}, separators=(",", ":")),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "mcp__*", "--tools", ",".join(requested)]
