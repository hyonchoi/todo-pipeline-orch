#!/usr/bin/env python3
"""Offline stand-in for the ``gh`` subset used by todo-pipeline-orchestrator.

State lives in the JSON file named by ``TPO_FAKE_GH_STATE``::

    {"repo": "owner/repo", "issues": {"1": {...REST issue...}},
     "comments": {"1": [{"body": "..."}]}, "labels": ["tpo:todo"],
     "dependencies": [[blocked_number, blocker_id]]}

Supported: ``auth status``; ``api`` list/single issue/comments/dependencies POST;
``issue edit|comment|close|create``; ``label list|create``. Anything else exits 1
with ``fake gh: unsupported: <argv>``. Stdlib only; no network.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

STATE_ENV = "TPO_FAKE_GH_STATE"
_ISSUES_RE = re.compile(r"^repos/([^/]+/[^/]+)/issues(?:\?(.*))?$")
_ISSUE_RE = re.compile(r"^repos/([^/]+/[^/]+)/issues/(\d+)(/comments|/dependencies/blocked_by)?$")


class Failure(Exception):
    def __init__(self, message: str, rc: int = 1) -> None:
        super().__init__(message)
        self.rc = rc


def _lock(path: Path):
    """Exclusive advisory lock on ``<state>.lock`` for one load->mutate->save cycle."""
    handle = open(path.with_name(path.name + ".lock"), "a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _load() -> tuple[Path, dict]:
    raw = os.environ.get(STATE_ENV)
    if not raw:
        raise Failure(f"fake gh: {STATE_ENV} is not set")
    path = Path(raw)
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("issues", {})
    state.setdefault("comments", {})
    state.setdefault("labels", [])
    state.setdefault("dependencies", [])
    return path, state


def _save(path: Path, state: dict) -> None:
    _recompute_dependencies(state)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _recompute_dependencies(state: dict) -> None:
    issues = state["issues"].values()
    by_id = {issue["id"]: issue for issue in issues if "id" in issue}
    by_number = {issue["number"]: issue for issue in issues}
    for issue in issues:
        issue_id = issue.get("id")
        blockers = [by_id.get(blocker) for num, blocker in state["dependencies"] if num == issue["number"]]
        blocking = [by_number.get(num) for num, blocker in state["dependencies"] if blocker == issue_id]
        issue["issue_dependencies_summary"] = {
            "blocked_by": sum(1 for blocker in blockers if blocker and blocker["state"] == "open"),
            "blocking": sum(1 for blocked in blocking if blocked and blocked["state"] == "open"),
            "total_blocked_by": len(blockers),
            "total_blocking": len(blocking),
        }


def _issue(state: dict, number: str | int) -> dict:
    issue = state["issues"].get(str(number))
    if issue is None:
        raise Failure(f"gh: Not Found (HTTP 404) repos/{state['repo']}/issues/{number}")
    return issue


def _check_repo(state: dict, repo: str | None) -> None:
    if repo is not None and repo.lower() != state["repo"].lower():
        raise Failure(f"gh: Not Found (HTTP 404) unknown repository {repo}")


def _flags(argv: list[str], *, valued: tuple[str, ...], boolean: tuple[str, ...] = ()) -> tuple[dict[str, list[str]], list[str]]:
    """Split ``argv`` into ``{flag: [values]}`` and positionals; ``--`` ends flag parsing."""
    flags: dict[str, list[str]] = {}
    positionals: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            positionals.extend(argv[index + 1:])
            break
        if token in valued:
            if index + 1 >= len(argv):
                raise Failure(f"fake gh: flag {token} requires a value")
            flags.setdefault(token, []).append(argv[index + 1])
            index += 2
            continue
        if token in boolean:
            flags.setdefault(token, [])
        elif token.startswith("-"):
            raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")
        else:
            positionals.append(token)
        index += 1
    return flags, positionals


def _labels(issue: dict) -> list[str]:
    return [label["name"] for label in issue.get("labels", [])]


def _split_labels(values: list[str]) -> list[str]:
    """``gh`` accepts repeated flags and comma-separated lists alike."""
    return [name.strip() for value in values for name in value.split(",") if name.strip()]


def _api(state: dict, path: Path, argv: list[str]) -> str:
    flags, positionals = _flags(argv, valued=("-H", "--method", "-F", "-f"), boolean=("--paginate", "--slurp"))
    if len(positionals) != 1:
        raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")
    endpoint = positionals[0]
    method = (flags.get("--method") or ["GET"])[0].upper()
    wrap = "--slurp" in flags

    def _emit(data: object) -> str:
        return json.dumps([data] if wrap else data)

    listing = _ISSUES_RE.match(endpoint)
    if listing and method == "GET":
        _check_repo(state, listing.group(1))
        query = parse_qs(listing.group(2) or "")
        wanted_state = (query.get("state") or ["open"])[0]
        wanted_labels = [label for raw in query.get("labels", []) for label in raw.split(",") if label]
        issues = [
            issue for issue in sorted(state["issues"].values(), key=lambda item: item["number"])
            if (wanted_state == "all" or issue["state"] == wanted_state)
            and all(label.lower() in {name.lower() for name in _labels(issue)} for label in wanted_labels)
        ]
        return _emit(issues)
    single = _ISSUE_RE.match(endpoint)
    if single is None:
        raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")
    _check_repo(state, single.group(1))
    issue = _issue(state, single.group(2))
    suffix = single.group(3)
    if suffix is None and method == "GET":
        return _emit(issue)
    if suffix == "/comments" and method == "GET":
        return _emit(state["comments"].get(str(issue["number"]), []))
    if suffix == "/dependencies/blocked_by" and method == "POST":
        fields = dict(item.split("=", 1) for item in flags.get("-F", []) + flags.get("-f", []))
        try:
            blocker_id = int(fields["issue_id"])
        except (KeyError, ValueError):
            raise Failure("gh: Validation Failed (HTTP 422) issue_id is required") from None
        if not any(other.get("id") == blocker_id for other in state["issues"].values()):
            raise Failure("gh: Not Found (HTTP 404) unknown issue_id")
        edge = [issue["number"], blocker_id]
        if edge in state["dependencies"]:
            raise Failure("gh: Validation Failed (HTTP 422) dependency already exists")
        state["dependencies"].append(edge)
        _save(path, state)
        return json.dumps(issue)
    raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")


def _issue_cmd(state: dict, path: Path, argv: list[str]) -> str:
    if not argv:
        raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")
    verb, rest = argv[0], argv[1:]
    valued = ("--repo", "--add-label", "--remove-label", "--body-file", "--body", "--reason", "--title", "--label")
    flags, positionals = _flags(rest, valued=valued)
    repo = (flags.get("--repo") or [None])[0]
    _check_repo(state, repo)
    if verb == "create":
        if positionals or "--title" not in flags:
            raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")
        number = max((issue["number"] for issue in state["issues"].values()), default=0) + 1
        body = Path(flags["--body-file"][0]).read_text(encoding="utf-8") if "--body-file" in flags else (flags.get("--body") or [""])[0]
        url = f"https://github.com/{state['repo']}/issues/{number}"
        state["issues"][str(number)] = {
            "id": 1000 + number,
            "number": number,
            "title": flags["--title"][0],
            "body": body,
            "state": "open",
            "labels": [{"name": name} for name in flags.get("--label", [])],
            "assignees": [],
            "html_url": url,
        }
        _save(path, state)
        return url + "\n"
    if len(positionals) != 1 or not positionals[0].isdigit():
        raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")
    issue = _issue(state, positionals[0])
    if verb == "edit":
        names = _labels(issue)
        for name in _split_labels(flags.get("--add-label", [])):
            if name.lower() not in {existing.lower() for existing in names}:
                names.append(name)
        removals = {name.lower() for name in _split_labels(flags.get("--remove-label", []))}
        issue["labels"] = [{"name": name} for name in names if name.lower() not in removals]
        _save(path, state)
        return f"{issue['html_url']}\n"
    if verb == "comment":
        if "--body-file" in flags:
            body = Path(flags["--body-file"][0]).read_text(encoding="utf-8")
        elif "--body" in flags:
            body = flags["--body"][0]
        else:
            raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")
        comments = state["comments"].setdefault(str(issue["number"]), [])
        comments.append({"id": 5000 + sum(len(items) for items in state["comments"].values()), "body": body})
        _save(path, state)
        return f"{issue['html_url']}#issuecomment-{comments[-1]['id']}\n"
    if verb == "close":
        if issue["state"] == "closed":
            sys.stderr.write(f"! Issue #{issue['number']} ({issue['title']}) is already closed\n")
            return ""
        issue["state"] = "closed"
        issue["state_reason"] = (flags.get("--reason") or ["completed"])[0]
        _save(path, state)
        return f"✓ Closed issue #{issue['number']} ({issue['title']})\n"
    raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")


def _label_cmd(state: dict, path: Path, argv: list[str]) -> str:
    if not argv:
        raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")
    verb, rest = argv[0], argv[1:]
    flags, positionals = _flags(rest, valued=("--repo", "--json", "--limit", "--color", "--description"), boolean=("--force",))
    _check_repo(state, (flags.get("--repo") or [None])[0])
    if verb == "list" and not positionals:
        return json.dumps([{"name": name} for name in state["labels"]]) + "\n"
    if verb == "create" and len(positionals) == 1:
        name = positionals[0]
        if name.lower() in {existing.lower() for existing in state["labels"]}:
            if "--force" not in flags:
                raise Failure("gh: Validation Failed (HTTP 422) label already exists")
        else:
            state["labels"].append(name)
            _save(path, state)
        return f"✓ Label \"{name}\" created in {state['repo']}\n"
    raise Failure(f"fake gh: unsupported: {sys.argv[1:]}")


def main(argv: list[str]) -> int:
    lock = None
    try:
        if argv[:2] == ["auth", "status"]:
            sys.stderr.write("github.com\n  ✓ Logged in to github.com account fake-gh\n")
            return 0
        raw = os.environ.get(STATE_ENV)
        if not raw:
            raise Failure(f"fake gh: {STATE_ENV} is not set")
        lock = _lock(Path(raw))
        path, state = _load()
        if argv[:1] == ["api"]:
            out = _api(state, path, argv[1:])
        elif argv[:1] == ["issue"]:
            out = _issue_cmd(state, path, argv[1:])
        elif argv[:1] == ["label"]:
            out = _label_cmd(state, path, argv[1:])
        else:
            raise Failure(f"fake gh: unsupported: {argv}")
    except Failure as exc:
        sys.stderr.write(f"{exc}\n")
        return exc.rc
    finally:
        if lock is not None:
            lock.close()  # releases the flock
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
