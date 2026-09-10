"""Seatbelt construction tests; native enforcement lives in a separate suite."""
import subprocess
from pathlib import Path

import pytest

from hermes_pipeline import agent_collector as collector
from hermes_pipeline.agent_execution import ExecutionError


def directories(tmp_path):
    snapshot, authority = tmp_path / 'snapshot', tmp_path / 'authority'
    snapshot.mkdir()
    authority.mkdir()
    return snapshot, authority


def test_darwin_filter_does_not_construct_linux_bpf(monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(collector.platform, 'machine', lambda: 'arm64')
    with collector.verification_filter() as descriptor:
        assert descriptor is None


def test_darwin_profile_denies_default_and_limits_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(collector.shutil, 'which', lambda name: '/usr/bin/sandbox-exec')
    snapshot, authority = directories(tmp_path)
    command = collector.verification_argv(['python', '-m', 'pytest'], snapshot,
                                         authority_root=authority, seccomp_fd=None)
    assert command[:2] == ['/usr/bin/sandbox-exec', '-p']
    profile = command[2]
    assert '(deny default)' in profile
    assert '(allow signal (target same-sandbox))' in profile
    assert '(allow mach-' not in profile
    assert '(allow network' not in profile
    assert '(require-not (subpath (param "AUTHORITY")))' in profile
    assert 'SNAPSHOT=' + str(snapshot.resolve()) in command
    assert 'AUTHORITY=' + str(authority.resolve()) in command
    assert 'UV_OFFLINE=1' in command
    assert command[-3:] == ['python', '-m', 'pytest']


def test_darwin_paths_are_canonical_but_overlap_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(collector.shutil, 'which', lambda name: '/usr/bin/sandbox-exec')
    snapshot, authority = directories(tmp_path)
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    command = collector.verification_argv(['true'], alias / snapshot.name,
                                         authority_root=alias / authority.name, seccomp_fd=None)
    assert 'SNAPSHOT=' + str(snapshot.resolve()) in command
    with pytest.raises(ExecutionError, match='containment'):
        collector.verification_argv(['true'], snapshot, authority_root=alias, seccomp_fd=None)


def test_unknown_verification_platform_fails_closed(monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Plan9')
    with pytest.raises(ExecutionError, match='unsupported'):
        with collector.verification_filter():
            pass


def test_darwin_capability_probe_is_bounded_and_failure_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(collector.shutil, 'which', lambda name: '/usr/bin/sandbox-exec')
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1, b'', b'secret provider body')

    monkeypatch.setattr(collector.subprocess, 'run', run)
    with pytest.raises(ExecutionError, match='sandbox unavailable') as caught:
        collector.confirm_verification_capability()
    assert 'secret' not in str(caught.value)
    assert calls[0][1]['timeout'] <= 10
    assert calls[0][1]['env'] == {}
    assert calls[0][1]['close_fds'] is True
    assert Path(calls[0][1]['cwd']).is_absolute()


def test_darwin_missing_sandbox_fails_before_command(monkeypatch, tmp_path):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(collector.shutil, 'which', lambda name: None)
    snapshot, authority = directories(tmp_path)
    with pytest.raises(ExecutionError, match='sandbox unavailable'):
        collector.verification_argv(['true'], snapshot, authority_root=authority, seccomp_fd=None)


def test_darwin_refuses_runtime_symlink_from_previous_check(monkeypatch, tmp_path):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(collector.shutil, 'which', lambda name: '/usr/bin/sandbox-exec')
    snapshot, authority = directories(tmp_path)
    (snapshot / '.tpo-runtime').symlink_to(authority, target_is_directory=True)
    with pytest.raises((ExecutionError, OSError)):
        collector.verification_argv(['true'], snapshot, authority_root=authority, seccomp_fd=None)


def test_darwin_capability_timeout_fails_closed(monkeypatch):
    monkeypatch.setattr(collector.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(collector.shutil, 'which', lambda name: '/usr/bin/sandbox-exec')

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])

    monkeypatch.setattr(collector.subprocess, 'run', timeout)
    with pytest.raises(ExecutionError, match='sandbox unavailable'):
        collector.confirm_verification_capability()
