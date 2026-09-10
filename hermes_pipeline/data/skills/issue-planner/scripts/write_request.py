#!/usr/bin/env python3
"""Durably create one private todo-manager CLI input without replacing files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import NoReturn

MAX_RETAINED_REQUEST_BYTES = 4 * 1024 * 1024


def fail(message: str) -> NoReturn:
    raise SystemExit(message)


def _checkpoint(_point: str) -> None:
    """Test seam for deterministic directory-replacement checks."""


def _open_child_directory(parent_fd: int, name: str) -> tuple[int, os.stat_result]:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    child_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(child_path.st_mode) or stat.S_ISLNK(child_path.st_mode):
        fail(f"{name} must be a non-symlink directory")
    child_fd = os.open(
        name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    opened = os.fstat(child_fd)
    if (child_path.st_dev, child_path.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(child_fd)
        fail(f"{name} directory identity changed")
    if name in {"todo-create-input", "issue-planner-batches"} and stat.S_IMODE(opened.st_mode) != 0o700:
        os.close(child_fd)
        fail(f"{name} must have mode 0700")
    if created:
        os.fsync(parent_fd)
    return child_fd, opened


def _verify_child_directory(
    parent_fd: int, name: str, opened: os.stat_result
) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        fail(f"{name} directory identity changed")


def write_request(project: Path, transaction: str, raw: bytes) -> Path:
    return _write_private(project, transaction, raw, "todo-create-input", "transaction_id")


def _write_private(
    project: Path, transaction: str, raw: bytes, directory: str, identity: str
) -> Path:
    project = project.resolve(strict=True)
    if not project.is_dir():
        fail("project root is not a directory")
    try:
        parsed = uuid.UUID(transaction)
    except ValueError:
        fail("transaction ID must be a canonical lowercase UUIDv4")
    if parsed.version != 4 or str(parsed) != transaction:
        fail("transaction ID must be a canonical lowercase UUIDv4")

    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        fail("request must be valid UTF-8 JSON")
    if not isinstance(payload, dict) or payload.get(identity) != transaction:
        fail("request transaction ID does not match the filename")

    project_path = project.lstat()
    project_fd = os.open(
        project,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    project_opened = os.fstat(project_fd)
    if (project_path.st_dev, project_path.st_ino) != (
        project_opened.st_dev,
        project_opened.st_ino,
    ):
        os.close(project_fd)
        fail("project directory identity changed")
    state_fd = directory_fd = -1
    descriptor = -1
    name = f"{transaction}.json"
    try:
        state_fd, state_opened = _open_child_directory(project_fd, ".hermes")
        _checkpoint("state-opened")
        _verify_child_directory(project_fd, ".hermes", state_opened)
        directory_fd, input_opened = _open_child_directory(
            state_fd, directory
        )
        _checkpoint("input-opened")
        _verify_child_directory(state_fd, directory, input_opened)
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o600:
            fail("request target is not a private regular file")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(directory_fd)
        _verify_child_directory(state_fd, directory, input_opened)
        _verify_child_directory(project_fd, ".hermes", state_opened)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            os.close(directory_fd)
        if state_fd >= 0:
            os.close(state_fd)
        os.close(project_fd)
    return project / ".hermes" / directory / name


def _fields(value: object, names: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != names:
        fail(f"invalid {label} fields")
    return value


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


def _key(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value) is not None


def _title(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= 256 and all(
        ord(character) >= 32 for character in value
    )


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def validate_batch(record: object, batch_id: str) -> None:
    """Validate immutable publication evidence; this never grants permission."""
    record = _fields(record, {
        "schema_version", "batch_id", "repository", "source_plan_sha256",
        "parent", "issues", "dependencies", "terminal_validator", "strategy",
        "manual_handoff", "reviews", "exceptions", "approval_digest",
    }, "batch")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        fail("batch schema_version must be integer 1")
    if not _uuid(batch_id) or record["batch_id"] != batch_id:
        fail("batch identity must match its canonical UUIDv4 filename")
    if not isinstance(record["repository"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", record["repository"]
    ):
        fail("batch repository must be literal OWNER/REPO")
    if not _sha(record["source_plan_sha256"]):
        fail("invalid source plan digest")
    if record["strategy"] not in ("incremental", "integration") or type(record["manual_handoff"]) is not bool:
        fail("invalid delivery strategy")
    if (record["strategy"] == "integration") != record["manual_handoff"]:
        fail("integration requires manual handoff; incremental releases require false")
    issues = record["issues"]
    if not isinstance(issues, list) or not issues:
        fail("batch requires at least one issue")
    parent = record["parent"]
    parent_key = None
    if parent is not None:
        parent = _fields(parent, {"key", "title", "transaction_marker", "body", "body_sha256"}, "parent")
        parent_key = parent["key"]
        if not _key(parent_key) or not _title(parent["title"]) or not _sha(parent["body_sha256"]):
            fail("invalid parent identity")
        if parent["transaction_marker"] != f"<!-- issue-planner-batch: {batch_id} -->":
            fail("parent marker must bind the exact batch identity")
        if not isinstance(parent["body"], str) or not parent["body"].strip() or hashlib.sha256(parent["body"].encode()).hexdigest() != parent["body_sha256"]:
            fail("parent body does not match its approved digest")
        if re.findall(r"<!-- issue-planner-batch: .*? -->", parent["body"]) != [parent["transaction_marker"]]:
            fail("parent body must contain exactly its transaction marker once")
        if parent["body"].splitlines().count(parent["transaction_marker"]) != 1:
            fail("parent transaction marker must occupy its own exact line")
        if re.search(r"```\s*json\s+tpo-plan\b", parent["body"], re.IGNORECASE):
            fail("parent must not contain an executable Plan manifest")
    if (len(issues) > 1) != (parent is not None):
        fail("split groups require a parent; single issues must not have a parent")
    if parent is None and record["strategy"] == "integration":
        fail("integration strategy requires a group")
    by_key = {}
    transactions = set()
    request_hashes = set()
    for issue in issues:
        issue = _fields(issue, {
            "key", "title", "parent_key", "transaction_id", "request_sha256",
            "body", "body_sha256", "role", "size", "hold",
        }, "issue")
        key = issue["key"]
        if not _key(key) or key in by_key or key == parent_key or not _title(issue["title"]):
            fail("invalid or duplicate issue identity")
        if issue["parent_key"] != parent_key:
            fail("child parent identity differs from the batch parent")
        if not _uuid(issue["transaction_id"]) or issue["transaction_id"] in transactions or issue["transaction_id"] == batch_id:
            fail("invalid or duplicate request transaction")
        if not _sha(issue["request_sha256"]) or not _sha(issue["body_sha256"]) or issue["request_sha256"] in request_hashes:
            fail("invalid or duplicate approved request digest")
        if not isinstance(issue["body"], str) or not issue["body"].strip() or hashlib.sha256(issue["body"].encode()).hexdigest() != issue["body_sha256"]:
            fail("child preview body does not match its approved digest")
        if issue["role"] not in ("implementation", "scoped-validation", "final-validation") or issue["size"] not in ("Small", "Medium", "Large"):
            fail("invalid issue role or size")
        if issue["hold"] is not True:
            fail("all executable issues must initially be held")
        by_key[key] = issue
        transactions.add(issue["transaction_id"])
        request_hashes.add(issue["request_sha256"])
    graph = {key: set() for key in by_key}
    if not isinstance(record["dependencies"], list):
        fail("dependencies must be an array")
    for edge in record["dependencies"]:
        edge = _fields(edge, {"issue_key", "requires"}, "dependency")
        child, prerequisite = edge["issue_key"], edge["requires"]
        if not _key(child) or not _key(prerequisite) or child not in graph or prerequisite not in graph or child == prerequisite:
            fail("dependency must join distinct executable children")
        if prerequisite in graph[child]:
            fail("duplicate dependency")
        graph[child].add(prerequisite)
    remaining = set(graph)
    while remaining:
        ready = {key for key in remaining if not graph[key] & remaining}
        if not ready:
            fail("dependency cycle")
        remaining -= ready
    terminal = record["terminal_validator"]
    validators = [key for key, issue in by_key.items() if issue["role"] == "final-validation"]
    if parent is None:
        if terminal is not None or validators or issues[0]["role"] != "implementation":
            fail("single issue must be an implementation without a terminal validator")
    elif not _key(terminal) or validators != [terminal] or graph[terminal] != set(by_key) - {terminal}:
        fail("group requires exactly one terminal validator depending on every child")
    if parent is not None and not any(issue["role"] == "implementation" for issue in issues):
        fail("group requires implementation work")
    if terminal is not None and by_key[terminal]["size"] == "Large":
        fail("terminal validation must remain Small or Medium")
    if not isinstance(record["exceptions"], list):
        fail("exceptions must be an array")
    large_exceptions = set()
    reduced_review = None
    for exception in record["exceptions"]:
        if not isinstance(exception, dict):
            fail("invalid exception")
        if exception.get("kind") == "atomic-large":
            _fields(exception, {"kind", "issue_key", "rationale_sha256", "permission_sha256"}, "Large exception")
            key = exception["issue_key"]
            if not _key(key) or key not in by_key or key in large_exceptions or by_key[key]["size"] != "Large":
                fail("Large exception must identify one Large issue")
            if not _sha(exception["rationale_sha256"]) or not _sha(exception["permission_sha256"]):
                fail("Large exception requires rationale and explicit permission evidence")
            large_exceptions.add(key)
        elif exception.get("kind") == "reduced-review":
            _fields(exception, {"kind", "unavailable_engine", "disclosure_sha256", "alternate_route_sha256"}, "review exception")
            if reduced_review is not None or exception["unavailable_engine"] not in ("codex", "claude") or not _sha(exception["disclosure_sha256"]) or not _sha(exception["alternate_route_sha256"]):
                fail("invalid reduced review evidence")
            reduced_review = exception["unavailable_engine"]
        else:
            fail("unsupported exception")
    if large_exceptions != {key for key, issue in by_key.items() if issue["size"] == "Large"}:
        fail("each atomic Large issue requires its own approved exception")
    packet = {key: value for key, value in record.items() if key not in {"reviews", "approval_digest"}}
    packet_digest = _digest(packet)
    coverage = {engine: set() for engine in ("codex", "claude")}
    expected_keys = set(by_key) | ({parent_key} if parent_key else set())
    if not isinstance(record["reviews"], list) or not record["reviews"]:
        fail("independent review evidence is required")
    for review in record["reviews"]:
        _fields(review, {"engine", "verdict", "issue_keys", "packet_digest", "route", "evidence_sha256"}, "sanitized review")
        engine = review["engine"]
        if engine not in ("codex", "claude") or engine == reduced_review or review["verdict"] != "PASS" or review["route"] != "read-only" or not _sha(review["evidence_sha256"]):
            fail("review evidence must be independent read-only PASS")
        keys = review["issue_keys"]
        if not isinstance(keys, list) or not keys or not all(_key(key) for key in keys) or len(set(keys)) != len(keys) or not set(keys) <= expected_keys or coverage[engine] & set(keys):
            fail("invalid or duplicate per-issue review verdicts")
        if review["packet_digest"] != packet_digest:
            fail("review packet digest is stale")
        coverage[engine].update(keys)
    for engine, keys in coverage.items():
        if engine != reduced_review and keys != expected_keys:
            fail("each available engine must PASS the parent and every child")
    if record["approval_digest"] != _digest({key: value for key, value in record.items() if key != "approval_digest"}):
        fail("approval digest is stale")


def _verify_requests(project: Path, record: dict) -> None:
    """Read retained request identities through no-follow directory descriptors."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    project = project.resolve(strict=True)
    project_fd = os.open(project, flags)
    state_fd = input_fd = -1
    try:
        state_fd = os.open(".hermes", flags, dir_fd=project_fd)
        state_opened = os.fstat(state_fd)
        input_fd = os.open("todo-create-input", flags, dir_fd=state_fd)
        input_opened = os.fstat(input_fd)
        if stat.S_IMODE(input_opened.st_mode) != 0o700:
            fail("retained request directory must have mode 0700")
        for issue in record["issues"]:
            name = f"{issue['transaction_id']}.json"
            fd = os.open(
                name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=input_fd,
            )
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1:
                    fail("approved request must be a private regular file")
                if before.st_size > MAX_RETAINED_REQUEST_BYTES:
                    fail("approved request exceeds the 4 MiB size limit")
                raw = handle.read(MAX_RETAINED_REQUEST_BYTES + 1)
                if len(raw) > MAX_RETAINED_REQUEST_BYTES:
                    fail("approved request exceeds the 4 MiB size limit")
                after = os.fstat(handle.fileno())
            current = os.stat(name, dir_fd=input_fd, follow_symlinks=False)
            def identity(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity(before) != identity(after) or identity(after) != identity(current):
                fail("approved request changed while reading")
            if hashlib.sha256(raw).hexdigest() != issue["request_sha256"]:
                fail("approved request bytes differ from the batch")
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("transaction_id") != issue["transaction_id"] or request.get("title") != issue["title"] or request.get("hold") is not True:
                fail("approved request identity, title or initial hold differs")
        _verify_child_directory(state_fd, "todo-create-input", input_opened)
        _verify_child_directory(project_fd, ".hermes", state_opened)
    finally:
        if input_fd >= 0:
            os.close(input_fd)
        if state_fd >= 0:
            os.close(state_fd)
        os.close(project_fd)


def write_batch(project: Path, batch_id: str, raw: bytes) -> Path:
    """Keep approved bytes even after uncertain writes; never overwrite/recreate."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail("batch JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        record = json.loads(raw, object_pairs_hook=unique_object)
        validate_batch(record, batch_id)
        _verify_requests(project, record)
    except (UnicodeError, ValueError, TypeError):
        fail("batch must be valid UTF-8 JSON with a valid schema")
    return _write_private(project, batch_id, raw, "issue-planner-batches", "batch_id")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--batch":
        path = write_batch(Path(sys.argv[2]), sys.argv[3], sys.stdin.buffer.read())
        print(path)
        return 0
    if len(sys.argv) != 3:
        fail("usage: write_request.py [--batch] PROJECT_ROOT TRANSACTION_UUID")
    path = write_request(Path(sys.argv[1]), sys.argv[2], sys.stdin.buffer.read())
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
