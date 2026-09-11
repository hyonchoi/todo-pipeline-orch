"""Darwin birth identities and kernel PID-version-checked signaling.

The optional libproc audit-token wrapper is required; older kernels/libraries
fail closed. ABI: XNU proc_bsdinfowithuniqid (136 + 56 bytes), flavor 18.
The stable p_uniqueid survives exec; the audit token uses a freshly read
p_idversion because exec changes that version. No numeric-PID signal fallback.
"""
from __future__ import annotations

import ctypes
import errno
import os
import socket
import struct
import uuid

_PROC_FLAG_INEXIT = 0x4
_PROC_FLAG_SLEADER = 0x20


class Backend:
    def __init__(self):
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        try:
            self._info = library.proc_pidinfo
            self._send = library.proc_signal_with_audittoken
            self._sysctl = ctypes.CDLL(None, use_errno=True).sysctlbyname
        except AttributeError:
            raise OSError('Darwin ownership capability unavailable') from None
        self._info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                               ctypes.c_void_p, ctypes.c_int]
        self._info.restype = ctypes.c_int
        self._send.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
        self._send.restype = ctypes.c_int
        self._sysctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        self._sysctl.restype = ctypes.c_int

    def boot_id(self) -> str:
        buffer = ctypes.create_string_buffer(64)
        size = ctypes.c_size_t(len(buffer))
        if self._sysctl(b'kern.bootsessionuuid', buffer, ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), 'boot identity unavailable')
        if size.value != 37 or buffer.raw[36] != 0:
            raise OSError('invalid boot identity')
        try:
            value = str(uuid.UUID(buffer.raw[:36].decode('ascii')))
        except (ValueError, UnicodeError):
            raise OSError('invalid boot identity') from None
        return 'darwin:' + value

    def _record(self, pid: int) -> dict | None:
        if type(pid) is not int or not 0 < pid <= 0x7fffffff:
            raise OSError('invalid process identity')
        buffer = ctypes.create_string_buffer(192)
        ctypes.set_errno(0)
        size = self._info(pid, 18, 1, buffer, len(buffer))
        if size == 0 and ctypes.get_errno() == errno.ESRCH:
            return None
        if size != 192:
            raise OSError(ctypes.get_errno(), 'process identity unavailable')
        data = buffer.raw
        flags = struct.unpack_from('=I', data, 0)[0]
        state = struct.unpack_from('=I', data, 4)[0]
        actual_pid, ppid = struct.unpack_from('=II', data, 12)
        unique, parent_unique, version = struct.unpack_from('=QQi', data, 152)
        if actual_pid != pid or unique == 0:
            raise OSError('invalid process identity')
        return {'pid': pid, 'start_ticks': unique, 'ppid': ppid,
                'pgrp': struct.unpack_from('=I', data, 100)[0],
                'state': 'Z' if state == 5 else 'T' if state == 4 else 'R',
                '_parent_unique': parent_unique, '_version': version,
                '_in_exit': bool(flags & _PROC_FLAG_INEXIT),
                '_session_leader': bool(flags & _PROC_FLAG_SLEADER)}

    @staticmethod
    def _snapshot(record: dict, boot: str, session: int) -> dict:
        # Internal kernel fields never enter durable schema-v1 evidence.
        return {key: value for key, value in record.items() if not key.startswith('_')} | {
            'boot_id': boot, 'host': socket.gethostname(), 'session': session}

    def snapshot(self, pid: int) -> dict | None:
        boot = self.boot_id()
        first = self._record(pid)
        if first is None:
            return None
        if first['state'] == 'Z':
            # getsid excludes zombies. Zero means no current session authority;
            # a kernel-reported session leader still establishes its own SID.
            return self._snapshot(first, boot, pid if first['_session_leader'] else 0)
        try:
            session = os.getsid(pid)
        except ProcessLookupError:
            session = None
        second = self._record(pid)
        if second is None:
            return None
        if first['start_ticks'] != second['start_ticks']:
            raise OSError('process changed during session lookup')
        if second['state'] == 'Z':
            return self._snapshot(second, boot, pid if second['_session_leader'] else 0)
        if first['pgrp'] != second['pgrp']:
            raise OSError('process changed during session lookup')
        if session is None:
            if not second['_in_exit']:
                raise OSError('process changed during session lookup')
            session = pid if second['_session_leader'] else 0
        return self._snapshot(second, boot, session)

    def signal(self, identity: dict, sig: int) -> bool | None:
        """None means delivery is pending for a reverified, unchanged birth.

        It is not successful delivery or evidence of death. The cleanup caller
        must retry within its deadline and independently establish termination.
        """
        if sig <= 0:
            return False  # Darwin's audit-token API explicitly rejects signal 0.
        try:
            if (identity.get('boot_id') != self.boot_id()
                    or identity.get('host') != socket.gethostname()):
                return False
            for _ in range(3):
                current = self._record(identity.get('pid'))
                if current is None:
                    return True
                if current['start_ticks'] != identity.get('start_ticks'):
                    return False
                if current['state'] == 'Z':
                    return True
                token = (ctypes.c_uint32 * 8)()
                token[5] = current['pid']
                token[7] = current['_version'] & 0xffffffff
                result = self._send(token, sig)
                if result == 0:
                    return True
                if result != errno.ESRCH:
                    return False
                # Exec can replace idversion; revalidate stable uniqueid before
                # obtaining another version. Reused PIDs never pass this check.
            # After the final ESRCH, distinguish an exit/exec transition from
            # lost ownership. Audit signaling can lose its live-process lookup
            # while zombie-inclusive proc_pidinfo still observes this birth.
            current = self._record(identity.get("pid"))
            if current is None:
                return True
            if current["start_ticks"] != identity.get("start_ticks"):
                return False
            return True if current["state"] == "Z" else None
        except (OSError, ValueError, TypeError):
            return False
