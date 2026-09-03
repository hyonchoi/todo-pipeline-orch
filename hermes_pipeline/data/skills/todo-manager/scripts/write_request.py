#!/usr/bin/env python3
"""Durably create one private todo-manager CLI input without replacing files."""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import NoReturn


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
    if name == "todo-create-input" and stat.S_IMODE(opened.st_mode) != 0o700:
        os.close(child_fd)
        fail("todo-create-input must have mode 0700")
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
    if not isinstance(payload, dict) or payload.get("transaction_id") != transaction:
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
            state_fd, "todo-create-input"
        )
        _checkpoint("input-opened")
        _verify_child_directory(state_fd, "todo-create-input", input_opened)
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
        _verify_child_directory(state_fd, "todo-create-input", input_opened)
        _verify_child_directory(project_fd, ".hermes", state_opened)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            os.close(directory_fd)
        if state_fd >= 0:
            os.close(state_fd)
        os.close(project_fd)
    return project / ".hermes" / "todo-create-input" / name


def main() -> int:
    if len(sys.argv) != 3:
        fail("usage: write_request.py PROJECT_ROOT TRANSACTION_UUID")
    path = write_request(Path(sys.argv[1]), sys.argv[2], sys.stdin.buffer.read())
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
