"""Autophone-owned process observations over the selected generic device channel.

Hashes identify executable files at observation time, not process memory. Socket
and process identities are sampled again to reject drift during collection.
"""
from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

from .session import DeviceProtocolError

SOURCE = 'autophone-process-observation/v1'

# The shell runs through the selected vphoned connection. Its parent is vphoned;
# selecting by parent avoids accepting an unrelated daemon with the same name.
GUEST_PROCESS = r'''set -eu
export PATH=/var/jb/usr/sbin:/var/jb/sbin:/var/jb/usr/bin:/var/jb/bin:/iosbinpack64/usr/sbin:/iosbinpack64/sbin:/iosbinpack64/usr/bin:/iosbinpack64/bin:/usr/sbin:/usr/bin:/sbin:/bin
runtime_pid=$PPID
runtime_start=$(ps -p "$runtime_pid" -o lstart=)
runtime_path=$(ps -p "$runtime_pid" -o comm=)
[ -n "$runtime_start" ] && [ -f "$runtime_path" ]
case "$runtime_path" in /*/vphoned) ;; *) exit 65 ;; esac
if command -v sha256sum >/dev/null 2>&1; then
  runtime_hash=$(sha256sum "$runtime_path")
elif command -v shasum >/dev/null 2>&1; then
  runtime_hash=$(shasum -a 256 "$runtime_path")
else
  echo 'SHA-256 utility unavailable' >&2
  exit 69
fi
runtime_hash=${runtime_hash%% *}
printf '%s\n' "$runtime_pid" "$runtime_start" "$runtime_path" "$runtime_hash"
sysctl -n kern.boottime
'''


def _peer_pid(path: Path, timeout_s: float) -> int:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(timeout_s)
        peer.connect(str(path))
        if sys.platform == 'darwin':
            # sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2.
            return struct.unpack('i', peer.getsockopt(0, 2, 4))[0]
        if sys.platform.startswith('linux'):
            return struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
        raise DeviceProtocolError('runtime_host_platform_unsupported')


def _executable(pid: int) -> Path:
    if sys.platform == 'darwin':
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        buf = ctypes.create_string_buffer(4096)
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
        if lib.proc_pidpath(pid, buf, len(buf)) <= 0:
            raise DeviceProtocolError('runtime_host_executable_unavailable')
        return Path(buf.value.decode())
    return Path(f'/proc/{pid}/exe').resolve(strict=True)


def observe_host(socket_path: Path, *, timeout_s: float) -> dict:
    pid = _peer_pid(socket_path, timeout_s)
    if pid <= 0:
        raise DeviceProtocolError('runtime_host_pid_invalid')
    path = _executable(pid)
    def start():
        return subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], check=True,
                              capture_output=True, text=True, timeout=timeout_s).stdout.strip()
    before = start()
    def file_identity():
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    stat = file_identity()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not before or start() != before or file_identity() != stat or _executable(pid) != path:
        raise DeviceProtocolError('runtime_host_process_drift')
    if _peer_pid(socket_path, timeout_s) != pid:
        raise DeviceProtocolError('runtime_socket_owner_drift')
    return dict(pid=pid, process_start=before, path=str(path), binary_sha256=digest)


def observe_runtime(session, *, timeout_s: float) -> dict:
    if timeout_s <= 0:
        raise ValueError('positive runtime observation timeout required')
    deadline = time.monotonic() + timeout_s
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise DeviceProtocolError('runtime_observation_deadline_exceeded')
        return value
    path = Path(session.target.socket).resolve()
    def socket_identity():
        info = path.stat()
        return dict(path=str(path), device=info.st_dev, inode=info.st_ino)
    def guest():
        result = session.shell(GUEST_PROCESS, timeout_ms=max(1, int(remaining() * 1000)))
        if result.code != 0 or result.timed_out or result.truncated:
            raise DeviceProtocolError(f'runtime_guest_process_unavailable: exit={result.code}; '
                                      f'{getattr(result, "stderr", "").strip()[:300]}')
        parts = result.stdout.strip().splitlines()
        if len(parts) != 5:
            raise DeviceProtocolError('runtime_guest_process_invalid')
        return dict(pid=int(parts[0]), process_start=parts[1].strip(), path=parts[2].strip(),
                    binary_sha256=parts[3].strip(), boot_id=parts[4].strip())
    binding = socket_identity()
    host = observe_host(path, timeout_s=remaining())
    first = guest()
    after = guest()
    if first != after or host != observe_host(path, timeout_s=remaining()) or binding != socket_identity():
        raise DeviceProtocolError('runtime_identity_drift')
    remaining()
    return dict(ok=True, source=SOURCE, contract_version='v1', host=host, guest=first,
                binding=dict(**binding, rig_id=session.target.rig_id,
                             rig_generation=session.target.rig_generation))
