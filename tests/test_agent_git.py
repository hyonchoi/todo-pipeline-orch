"""Real Git regressions: inspecting worker output must not execute worker code."""
import subprocess

import pytest

from hermes_pipeline.agent_checkpoint import ProgressJournal
from hermes_pipeline.agent_execution import ExecutionStore


def git(tree, *args):
    return subprocess.check_output(['git', '-C', str(tree), *args], text=True, errors='surrogateescape').strip()


@pytest.mark.parametrize('attack', ['fsmonitor', 'filter'])
def test_journal_inspection_does_not_execute_repository_configuration(tmp_path, attack):
    tree = tmp_path / 'work'
    tree.mkdir()
    git(tree, 'init', '-b', 'task')
    git(tree, 'config', 'user.name', 'Test')
    git(tree, 'config', 'user.email', 'test@example.invalid')
    (tree / 'file').write_text('original\n')
    git(tree, 'add', 'file')
    git(tree, 'commit', '-m', 'base')
    store = ExecutionStore(tmp_path / 'private')
    store.register('execution', registration_id='card', plan_identity='a' * 64,
                   phase='development', prompt=b'plan', client={'name': 'codex', 'tools': []},
                   worktree=str(tree), branch='task', result_contract={}, timeout=30, manifest=None)
    progress = ProgressJournal(store, 'execution')
    progress.initialize()
    store.admit('execution')
    marker = store.root / 'forged-authority'
    script = tree / 'attack.sh'
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n')
    script.chmod(0o755)
    if attack == 'fsmonitor':
        git(tree, 'config', 'core.fsmonitor', str(script))
    else:
        (tree / '.gitattributes').write_text('file filter=malicious\n')
        git(tree, 'config', 'filter.malicious.clean', str(script))
        (tree / 'file').write_text('modified\n')
    progress.recovery_context(1)
    assert not marker.exists()


def test_configuration_environment_and_history_overrides_are_ignored(tmp_path, monkeypatch):
    from hermes_pipeline.agent_git import run_git
    tree = tmp_path / 'work'
    tree.mkdir()
    git(tree, 'init', '-b', 'task')
    git(tree, 'config', 'user.name', 'Test')
    git(tree, 'config', 'user.email', 'test@example.invalid')
    git(tree, 'commit', '--allow-empty', '-m', 'base')
    base = git(tree, 'rev-parse', 'HEAD')
    git(tree, 'commit', '--allow-empty', '-m', 'next')
    head = git(tree, 'rev-parse', 'HEAD')
    git(tree, 'replace', head, base)
    (tree / '.git' / 'info' / 'grafts').write_text(head + '\n')
    marker = tmp_path / 'marker'
    config = tmp_path / 'config'
    config.write_text(f'[core]\nfsmonitor = touch {marker}\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.fsmonitor')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', f'touch {marker}')
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'wrong'))
    result = run_git(tree, ['rev-list', '--parents', '-n', '1', 'HEAD'], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == f'{head} {base}'
    run_git(tree, ['status', '--porcelain'], capture_output=True, check=True)
    assert not marker.exists()


def test_linked_worktree_metadata_queries_return_original_lock_locations(tmp_path):
    from hermes_pipeline.agent_git import metadata_paths, run_git
    tree = tmp_path / 'work'
    tree.mkdir()
    git(tree, 'init', '-b', 'main')
    git(tree, 'config', 'user.name', 'Test')
    git(tree, 'config', 'user.email', 'test@example.invalid')
    git(tree, 'commit', '--allow-empty', '-m', 'base')
    linked = tmp_path / 'linked'
    git(tree, 'worktree', 'add', '-b', 'task', str(linked))
    _, directory, common = metadata_paths(linked)
    for arguments, expected in [
        (['rev-parse', '--git-path', 'index.lock'], directory / 'index.lock'),
        (['rev-parse', '--git-common-dir'], common),
        (['rev-parse', '--git-dir'], directory),
    ]:
        assert run_git(linked, arguments, capture_output=True, text=True, check=True).stdout.strip() == str(expected)
    assert run_git(linked, ['branch', '--show-current'], capture_output=True, text=True, check=True).stdout.strip() == 'task'


def test_git_inspection_cannot_modify_refs(tmp_path):
    from hermes_pipeline.agent_git import run_git
    for command in (['branch', '-D', 'task'], ['symbolic-ref', 'HEAD', 'refs/heads/other'], ['update-ref', 'HEAD', 'a' * 40]):
        with pytest.raises(OSError):
            run_git(tmp_path, command)


def test_inspection_rejects_symlinked_private_scratch(tmp_path):
    from hermes_pipeline.agent_git import run_git
    tree = tmp_path / 'work'
    tree.mkdir()
    git(tree, 'init', '-b', 'task')
    (tree / '.git' / 'tpo-inspection').symlink_to(tmp_path)
    with pytest.raises(OSError):
        run_git(tree, ['status'], capture_output=True)


def test_submodule_status_blocks_instead_of_running_nested_git(tmp_path):
    from hermes_pipeline.agent_git import run_git
    tree = tmp_path / 'work'
    tree.mkdir()
    git(tree, 'init', '-b', 'task')
    git(tree, 'config', 'user.name', 'Test')
    git(tree, 'config', 'user.email', 'test@example.invalid')
    git(tree, 'commit', '--allow-empty', '-m', 'base')
    commit = git(tree, 'rev-parse', 'HEAD')
    git(tree, 'update-index', '--add', '--cacheinfo', f'160000,{commit},nested')
    with pytest.raises(OSError, match='submodule cleanliness'):
        run_git(tree, ['status', '--porcelain'], capture_output=True)


@pytest.mark.parametrize('suffix', ['metadata ', 'meta\ndata ', 'meta\udcffdata '])
def test_pointer_paths_preserve_whitespace_and_filesystem_bytes(tmp_path, suffix):
    import os

    from hermes_pipeline.agent_git import metadata_paths, run_git

    tree = tmp_path / 'work'
    tree.mkdir()
    metadata = tmp_path / suffix
    git(tree, 'init', '-b', 'main', '--separate-git-dir', str(metadata))
    git(tree, 'config', 'user.name', 'Test')
    git(tree, 'config', 'user.email', 'test@example.invalid')
    git(tree, 'commit', '--allow-empty', '-m', 'base')
    # Git's gitfile grammar allows embedded newline bytes and strips only its
    # terminal CR/LF delimiters. The pathname's trailing space is significant.
    (tree / '.git').write_bytes(b'gitdir: ' + os.fsencode(metadata) + b'\r\n')
    assert git(tree, 'rev-parse', '--is-inside-work-tree') == 'true'
    assert metadata_paths(tree) == (tree, metadata, metadata)
    linked = tmp_path / 'linked'
    git(tree, 'worktree', 'add', '-b', 'task', str(linked))
    directory = metadata / 'worktrees' / 'linked'
    (directory / 'commondir').write_bytes(os.fsencode(metadata) + b'\n')
    assert git(linked, 'rev-parse', '--is-inside-work-tree') == 'true'
    assert metadata_paths(linked) == (linked, directory, metadata)
    assert run_git(linked, ['rev-parse', '--git-dir'], capture_output=True, check=True).stdout == os.fsencode(directory) + b'\n'
    assert run_git(linked, ['rev-parse', '--git-common-dir'], capture_output=True, text=True, check=True).stdout == str(metadata) + '\n'
    assert run_git(linked, ['branch', '--show-current'], capture_output=True, check=True).stdout == b'task\n'


@pytest.mark.parametrize('pointer', ['gitdir', 'commondir'])
def test_metadata_pointer_rejects_symlink_components_before_resolution(tmp_path, pointer):
    from hermes_pipeline.agent_git import metadata_paths

    tree = tmp_path / 'work'
    tree.mkdir()
    git(tree, 'init', '-b', 'main')
    git(tree, 'config', 'user.name', 'Test')
    git(tree, 'config', 'user.email', 'test@example.invalid')
    git(tree, 'commit', '--allow-empty', '-m', 'base')
    linked = tmp_path / 'linked'
    git(tree, 'worktree', 'add', '-b', 'task', str(linked))
    alias = tmp_path / 'alias'
    alias.symlink_to(tree / '.git', target_is_directory=True)
    directory = tree / '.git' / 'worktrees' / 'linked'
    if pointer == 'gitdir':
        (linked / '.git').write_text(f'gitdir: {alias}/worktrees/linked\n')
    else:
        (directory / 'commondir').write_text(f'{alias}/worktrees/..\n')
    # Git accepts the alias, but supervisor identity must not silently resolve
    # it away before applying its no-symlink boundary.
    assert git(linked, 'rev-parse', '--is-inside-work-tree') == 'true'
    with pytest.raises(OSError):
        metadata_paths(linked)


@pytest.mark.parametrize('name', ['--delete', '--output-notes'])
def test_option_shaped_filenames_after_separator_are_inspected(tmp_path, name):
    from hermes_pipeline.agent_git import run_git

    tree = tmp_path / 'work'
    tree.mkdir()
    git(tree, 'init', '-b', 'main')
    git(tree, 'config', 'user.name', 'Test')
    git(tree, 'config', 'user.email', 'test@example.invalid')
    (tree / name).write_text('original\n')
    git(tree, 'add', '--', name)
    git(tree, 'commit', '-m', 'base')
    head = git(tree, 'rev-parse', 'HEAD')
    (tree / name).write_text('changed\n')
    listing = run_git(tree, ['ls-files', '-z', '--', name], capture_output=True, check=True)
    assert listing.stdout == name.encode() + b'\0'
    diff = run_git(tree, ['diff', '--name-only', '-z', '--', name], capture_output=True, check=True)
    assert diff.stdout == name.encode() + b'\0'
    assert (tree / name).read_text() == 'changed\n'
    assert git(tree, 'rev-parse', 'HEAD') == head
    assert not (tree / 'notes').exists()


@pytest.mark.parametrize('arguments', [
    ['symbolic-ref', '--delete', 'HEAD'],
    ['diff', '--output=notes', '--', 'file'],
    ['diff', '--output', 'notes', '--', 'file'],
])
def test_unsafe_options_before_separator_still_block(tmp_path, arguments):
    from hermes_pipeline.agent_git import run_git

    with pytest.raises(OSError, match='unsafe Git inspection option'):
        run_git(tmp_path, arguments, capture_output=True)
    assert not (tmp_path / 'notes').exists()
