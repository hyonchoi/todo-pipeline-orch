"""Inspect agent-controlled Git state without loading executable repo settings.

Each query gets a private metadata view: object and ref reads use the original
repository, but config, hooks, info attributes, grafts, and index writes never
use agent-controlled paths. Enumerating dangerous config keys is insufficient:
filter driver names can change between an audit and the next Git invocation.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .agent_execution import ExecutionError

_READ_COMMANDS = frozenset({
    'branch', 'cat-file', 'check-ignore', 'check-ref-format', 'diff', 'diff-tree', 'ls-files',
    'ls-tree', 'merge-base', 'rev-list', 'rev-parse', 'show', 'show-ref',
    'status', 'symbolic-ref',
})



class CollectionTimedOut(ExecutionError):
    """Collection consumed its deadline (the original attempt deadline, or the bounded deadline-time collection budget)."""


_collection_deadline = ContextVar('collection_deadline', default=None)


def check_collection_deadline():
    deadline = _collection_deadline.get()
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionTimedOut('checkpoint deadline exceeded')
        return remaining
    return None


@contextmanager
def collection_deadline(deadline):
    """Scope a budget to this execution context, never to later status queries."""
    current = _collection_deadline.get()
    effective = current if deadline is None else deadline if current is None else min(current, deadline)
    token = _collection_deadline.set(effective)
    try:
        check_collection_deadline()
        yield
        check_collection_deadline()
    finally:
        _collection_deadline.reset(token)


def _run_query(*args, **kwargs):
    remaining = check_collection_deadline()
    if remaining is not None:
        kwargs['timeout'] = min(kwargs.get('timeout', 60), remaining)
    try:
        result = subprocess.run(*args, **kwargs)
    except subprocess.SubprocessError:
        check_collection_deadline()
        raise
    check_collection_deadline()
    return result


def _file(path: Path, maximum: int = 64 * 1024 * 1024) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise OSError('unsupported Git metadata')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise OSError('oversized Git metadata')
        return value
    finally:
        os.close(descriptor)


def _directory_path(path: Path) -> Path:
    """Check every original component before normalizing ``..`` segments.

    Resolving first would conceal symlinks, including a symlink traversed and
    then followed by ``..``. Directory descriptors keep each lookup anchored.
    """
    if not path.is_absolute():
        path = Path.cwd() / path
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return Path(os.path.normpath(path))


def metadata_paths(cwd: Path) -> tuple[Path, Path, Path]:
    """Find ordinary/linked repository metadata without invoking Git config."""
    tree = Path(cwd).resolve()
    while not os.path.lexists(tree / '.git'):
        if tree == tree.parent:
            raise OSError('Git repository missing')
        tree = tree.parent
    entry = tree / '.git'
    if entry.is_symlink():
        raise OSError('symlink Git metadata is unsupported')
    if entry.is_dir():
        directory = entry
    else:
        value = _file(entry, 4096).rstrip(b'\r\n')
        if not value.startswith(b'gitdir: ') or len(value) == 8 or b'\0' in value:
            raise OSError('invalid Git metadata pointer')
        directory = _directory_path(tree / os.fsdecode(value[8:]))
    common = directory
    if os.path.lexists(directory / 'commondir'):
        value = _file(directory / 'commondir', 4096).rstrip(b'\r\n')
        if not value or b'\0' in value:
            raise OSError('invalid Git common directory')
        common = _directory_path(directory / os.fsdecode(value))
    return tree, directory, common


def inspection_root(common: Path) -> Path:
    """Reserved path for the supervisor's Git inspection metadata."""
    return common / 'tpo-inspection'


def _private_root(common: Path) -> Path:
    root = inspection_root(common)
    descriptor = os.open(common, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ('tpo-inspection',):
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    return root


def inspection_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(('GIT_', 'LD_', 'DYLD_'))
        and key not in {'BASH_ENV', 'ENV'}
    }
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM='1',
                       GIT_NO_REPLACE_OBJECTS='1', GIT_GRAFT_FILE=os.devnull,
                       GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0',
                       GIT_TERMINAL_PROMPT='0', GIT_PAGER='cat', LC_ALL='C',
                       PATH=os.defpath)
    return environment


def run_git(cwd: Path, arguments, **kwargs):
    """Run only internal read queries; never use this for worktree mutations."""
    check_collection_deadline()
    arguments = list(arguments)
    prefix = []
    if arguments[:2] == ['-c', 'core.quotePath=false']:
        prefix, arguments = arguments[:2], arguments[2:]
    if not arguments or arguments[0] not in _READ_COMMANDS:
        raise OSError('unsupported Git inspection command')
    command = arguments[0]
    separator = arguments.index('--') if '--' in arguments else len(arguments)
    options = arguments[1:separator]
    operands = [arg for arg in options if not arg.startswith('-')] + arguments[separator + 1:]
    if (command == 'branch' and arguments[1:] != ['--show-current']) or (
        command == 'symbolic-ref' and len(operands) != 1
    ) or any(arg.startswith(('--recurse-submodules', '--submodule=', '--output')) for arg in options) or any(arg in {'--filters', '--textconv', '--show-signature', '--delete', '-d', '--write-bitmap-index'} for arg in options):
        raise OSError('unsafe Git inspection option')
    if command in {'diff', 'diff-tree', 'show'}:
        arguments[1:1] = ['--no-ext-diff', '--no-textconv']
    if command == 'status':
        arguments.insert(1, '--ignore-submodules=all')
    environment = inspection_environment()
    kwargs.pop('env', None)
    kwargs.setdefault('timeout', 60)
    if kwargs.get('text') or kwargs.get('universal_newlines'):
        kwargs.setdefault('errors', 'surrogateescape')
    executable = shutil.which('git', path=os.defpath)
    if executable is None:
        raise OSError('Git is unavailable')
    if command == 'check-ref-format':
        return _run_query([executable, *prefix, *arguments], cwd=cwd, env=environment, **kwargs)
    tree, directory, common = metadata_paths(Path(cwd))
    with tempfile.TemporaryDirectory(prefix='query-', dir=_private_root(common)) as scratch:
        view = Path(scratch)
        (view / 'config').write_text('[core]\nrepositoryformatversion = 0\nfsmonitor = false\n'
                                   'hooksPath = /dev/null\nattributesFile = /dev/null\n'
                                   '[diff]\nignoreSubmodules = all\n[submodule]\nrecurse = false\n')
        if (common / 'info' / 'exclude').exists():
            (view / 'info').mkdir()
            (view / 'info' / 'exclude').write_bytes(_file(common / 'info' / 'exclude'))
        (view / 'HEAD').write_bytes(_file(directory / 'HEAD', 4096))
        for name in ('objects', 'refs', 'packed-refs', 'shallow'):
            source = common / name
            if source.exists():
                (view / name).symlink_to(source)
        total_index_bytes = 0
        source = directory / 'index'
        if source.exists():
            data = _file(source)
            total_index_bytes += len(data)
            (view / 'index').write_bytes(data)
        # Split indexes locate these next to the index. Copy rather than link:
        # no optional refresh may touch the worker's original metadata.
        for count, source in enumerate(directory.glob('sharedindex.*')):
            if count >= 64:
                raise OSError('too many split Git indexes')
            data = _file(source)
            total_index_bytes += len(data)
            if total_index_bytes > 128 * 1024 * 1024:
                raise OSError('Git indexes exceed inspection limit')
            (view / source.name).write_bytes(data)
        environment.update(GIT_DIR=str(view), GIT_WORK_TREE=str(tree))
        if command == 'status':
            with tempfile.TemporaryFile(dir=view) as tracked:
                _run_query([executable, 'ls-files', '--stage', '-z'], cwd=cwd,
                               env=environment, stdout=tracked, stderr=subprocess.DEVNULL,
                               check=True, timeout=kwargs['timeout'])
                if tracked.tell() > 64 * 1024 * 1024:
                    raise OSError('Git index listing exceeds inspection limit')
                tracked.seek(0)
                if any(entry.startswith(b'160000 ') for entry in tracked.read().split(b'\0')):
                    raise OSError('submodule cleanliness requires isolated validation')
        result = _run_query([executable, *prefix, *arguments], cwd=cwd, env=environment, **kwargs)
        if command == 'rev-parse' and result.returncode == 0 and result.stdout is not None:
            # Lock/merge-state checks must inspect the real paths, not our view.
            text = os.fsdecode(result.stdout) if isinstance(result.stdout, bytes) else result.stdout
            if '--git-common-dir' in arguments:
                text = str(common) + '\n'
            elif '--git-dir' in arguments or '--absolute-git-dir' in arguments:
                text = str(directory) + '\n'
            elif '--git-path' in arguments:
                requested = arguments[arguments.index('--git-path') + 1]
                shared = requested.split('/')[0] in {'objects', 'refs', 'packed-refs', 'config', 'hooks', 'info'}
                text = str((common if shared else directory) / requested) + '\n'
            result.stdout = os.fsencode(text) if isinstance(result.stdout, bytes) else text
        return result
