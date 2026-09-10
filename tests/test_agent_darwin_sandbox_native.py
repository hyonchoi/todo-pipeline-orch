"""Required macOS enforcement evidence; never treat Linux mocks as qualification."""
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hermes_pipeline import agent_collector as collector


def require_native():
    if platform.system() != 'Darwin':
        if os.environ.get('REQUIRE_NATIVE_DARWIN') == '1':
            pytest.fail('native Darwin sandbox qualification requires macOS')
        pytest.skip('native Darwin sandbox qualification requires macOS')
    # On native runners missing tools, unsupported policy, or failed isolation
    # always fail; REQUIRE_NATIVE_DARWIN additionally forbids off-platform skips.
    assert shutil.which('sandbox-exec'), 'required sandbox-exec unavailable'
    collector.confirm_verification_capability()


def run_sandbox(snapshot, authority, script, *arguments):
    argv = collector.verification_argv([sys.executable, '-c', script, *map(str, arguments)],
                                      snapshot, authority_root=authority, seccomp_fd=None)
    result = subprocess.run(argv, cwd=snapshot, env={}, capture_output=True,
                            text=True, timeout=15, close_fds=True)
    assert result.returncode == 0, result.stdout + result.stderr


DENIED = '''
def denied(action):
    try:
        action()
    except OSError as exc:
        assert exc.errno in (1, 13, 30), repr(exc)
    else:
        raise AssertionError('sandbox allowed forbidden operation')
'''


def test_native_darwin_files_aliases_and_descendants(tmp_path):
    require_native()
    snapshot, authority = tmp_path / 'snapshot', tmp_path / 'authority'
    snapshot.mkdir()
    authority.mkdir()
    secret = authority / 'private'
    secret.write_text('trusted')
    outside = tmp_path / 'host'
    outside.write_text('trusted')
    (snapshot / 'alias').symlink_to(authority, target_is_directory=True)
    script = 'import os, subprocess, sys; from pathlib import Path\n' + DENIED + '''
Path('output').write_text('allowed')
Path(os.environ['TMPDIR'], 'temporary').write_text('allowed')
assert Path(sys.argv[2]).read_text() == 'trusted'
denied(lambda: Path(sys.argv[1]).read_text())
denied(lambda: Path(sys.argv[1]).write_text('forged'))
denied(lambda: Path('alias/private').read_text())
denied(lambda: Path(sys.argv[2]).write_text('forged'))
denied(lambda: Path('alias/new').write_text('forged'))
# A hardlink into the writable root must not make a host inode writable.
try:
    os.link(sys.argv[2], 'host-link')
except OSError as exc:
    assert exc.errno in (1, 13, 18, 30)
else:
    denied(lambda: Path('host-link').write_text('forged'))
# exec descendants must retain the exact boundary.
child = (
    "from pathlib import Path; import sys\\n"
    "try: Path(sys.argv[1]).write_text('forged')\\n"
    "except OSError as exc:\\n"
    "    assert exc.errno in (1, 13, 30)\\n"
    "    print('descendant-write-denied')\\n"
    "else: raise AssertionError('descendant write allowed')\\n"
)
result = subprocess.run([sys.executable, '-c', child, sys.argv[2]], capture_output=True, timeout=5)
assert result.returncode == 0, result.stderr
assert result.stdout == b'descendant-write-denied\\n'
'''
    run_sandbox(snapshot, authority, script, secret, outside)
    assert secret.read_text() == outside.read_text() == 'trusted'
    assert (snapshot / 'output').read_text() == 'allowed'


@pytest.mark.parametrize('kind', [socket.SOCK_STREAM, socket.SOCK_DGRAM])
def test_native_darwin_host_unix_endpoints_and_socketpair_sendto(tmp_path, kind):
    require_native()
    snapshot, authority = tmp_path / 'snapshot', tmp_path / 'authority'
    snapshot.mkdir()
    authority.mkdir()
    # macOS AF_UNIX sockaddr paths are short. /tmp also exercises /private/tmp.
    with tempfile.TemporaryDirectory(prefix='tpo-ipc-', dir='/tmp') as temporary:
        endpoint = str(Path(temporary) / 'host.sock')
        with socket.socket(socket.AF_UNIX, kind) as server:
            server.bind(endpoint)
            if kind == socket.SOCK_STREAM:
                server.listen(1)
            script = 'import socket, sys\n' + DENIED + '''
def host_connection():
    with socket.socket(socket.AF_UNIX, int(sys.argv[2])) as connection:
        connection.connect(sys.argv[1])
denied(host_connection)
if int(sys.argv[2]) == socket.SOCK_DGRAM:
    # A stream-only socketpair exception on Linux prevents this alternative;
    # Seatbelt must check the addressed destination for datagram pairs too.
    try:
        pair = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    except OSError as exc:
        assert exc.errno in (1, 13)
    else:
        left, right = pair
        with left, right:
            denied(lambda: left.sendto(b'forged', sys.argv[1]))
'''
            run_sandbox(snapshot, authority, script, endpoint, int(kind))
            server.settimeout(0.05)
            with pytest.raises(TimeoutError):
                server.accept() if kind == socket.SOCK_STREAM else server.recv(16)


@pytest.mark.parametrize('family', [socket.AF_INET, socket.AF_INET6])
@pytest.mark.parametrize('kind', [socket.SOCK_STREAM, socket.SOCK_DGRAM])
def test_native_darwin_host_network_denied(tmp_path, family, kind):
    require_native()
    snapshot, authority = tmp_path / 'snapshot', tmp_path / 'authority'
    snapshot.mkdir()
    authority.mkdir()
    address = '127.0.0.1' if family == socket.AF_INET else '::1'
    with socket.socket(family, kind) as server:
        server.bind((address, 0))
        if kind == socket.SOCK_STREAM:
            server.listen(1)
        script = 'import socket, sys\n' + DENIED + '''
def host_connection():
    with socket.socket(int(sys.argv[1]), int(sys.argv[2])) as connection:
        connection.settimeout(2)
        target = (sys.argv[3], int(sys.argv[4]))
        if connection.type == socket.SOCK_STREAM:
            connection.connect(target)
        else:
            connection.sendto(b'forged', target)
denied(host_connection)
'''
        run_sandbox(snapshot, authority, script, int(family), int(kind), address, server.getsockname()[1])
        server.settimeout(0.05)
        with pytest.raises(TimeoutError):
            server.accept() if kind == socket.SOCK_STREAM else server.recv(16)


def test_native_darwin_uv_offline_pytest_anonymous_runtime_ipc(tmp_path):
    require_native()
    uv = shutil.which('uv')
    assert uv, 'required uv unavailable'
    snapshot, authority = tmp_path / 'snapshot', tmp_path / 'authority'
    snapshot.mkdir()
    authority.mkdir()
    (snapshot / 'pyproject.toml').write_text(
        '[project]\nname="seatbelt-probe"\nversion="0.0.0"\nrequires-python=">=3.12"\n')
    (snapshot / 'test_ipc.py').write_text('''import asyncio
import os
import socket
import subprocess
import sys

def test_local_runtime_ipc():
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b'local')
        assert right.recv(5) == b'local'
    async def local():
        return await asyncio.to_thread(lambda: 42)
    assert asyncio.run(local()) == 42
    os.kill(os.getpid(), 0)
    subprocess.run([sys.executable, '-c', 'pass'], check=True, timeout=5)
''')
    argv = collector.verification_argv(
        [uv, 'run', '--no-sync', '--offline', '--python', sys.executable,
         'python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_ipc.py'],
        snapshot, authority_root=authority, seccomp_fd=None)
    argv.insert(argv.index('-i') + 1, 'UV_PROJECT_ENVIRONMENT=' + sys.prefix)
    result = subprocess.run(argv, cwd=snapshot, env={}, capture_output=True,
                            text=True, timeout=30, close_fds=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout


def test_native_darwin_host_signals_and_mach_services_denied(tmp_path):
    require_native()
    snapshot, authority = tmp_path / 'snapshot', tmp_path / 'authority'
    snapshot.mkdir()
    authority.mkdir()
    script = 'import ctypes, os, sys\n' + DENIED + '''
os.kill(os.getpid(), 0)
denied(lambda: os.kill(int(sys.argv[1]), 0))
lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
bootstrap = ctypes.c_uint.in_dll(lib, 'bootstrap_port').value
port = ctypes.c_uint()
lib.bootstrap_look_up.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
lib.bootstrap_look_up.restype = ctypes.c_int
assert lib.bootstrap_look_up(bootstrap, b'com.apple.cfprefsd.daemon', ctypes.byref(port)) != 0
'''
    # sandbox_check distinguishes actual policy denial from an absent service.
    script += '''
sandbox = ctypes.CDLL('/usr/lib/libsandbox.dylib')
sandbox.sandbox_check.restype = ctypes.c_int
# SANDBOX_FILTER_GLOBAL_NAME = 2: WebKit Source/WTF/wtf/spi/darwin/SandboxSPI.h.
assert sandbox.sandbox_check(os.getpid(), b'mach-lookup', 2, b'com.apple.cfprefsd.daemon') == 1
'''
    run_sandbox(snapshot, authority, script, os.getpid())
