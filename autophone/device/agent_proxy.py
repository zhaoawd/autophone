"""Forward managed MCP device requests through the parent's live authority."""
from __future__ import annotations

from contextvars import copy_context
import json
import os
from pathlib import Path
import socket
import tempfile
import threading

from .stage_guard import StageGuard, OperationClass


class AgentDeviceProxy:
    def __init__(self):
        self.binding = StageGuard.binding()
        if self.binding is None:
            raise RuntimeError('agent_device_authority_missing')
        self.binding.guard.assert_allowed(OperationClass.FRIDA)
        from .target import resolve_target
        self.target = self.binding.guard.socket_path or str(resolve_target().sock)
        # Keep the UNIX path below macOS's sockaddr_un limit.
        self.directory = tempfile.TemporaryDirectory(prefix='ap-agent-', dir='/tmp')
        self.path = str(Path(self.directory.name) / 'device.sock')
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.path)
        os.chmod(self.path, 0o600)
        self.listener.listen(4)
        self.listener.settimeout(.05)
        self.stop = threading.Event()
        self.closed = False
        context = copy_context()
        self.thread = threading.Thread(target=lambda: context.run(self._serve),
                                       name='agent-device-proxy', daemon=True)
        self.release = self.binding.guard.register_resource(self.close, requires_lease=False)
        self.thread.start()

    def _serve(self):
        from .uds import request
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    data = bytearray()
                    while b'\n' not in data:
                        block = conn.recv(65536)
                        if not block:
                            break
                        data.extend(block)
                        if len(data) > 1024 * 1024:
                            raise ValueError('agent_request_too_large')
                    message = json.loads(bytes(data).split(b'\n', 1)[0])
                    # uds.request checks command class, endpoint, generation and
                    # the captured token before opening the real device socket.
                    response, _ = request(message, sock_path=self.target,
                                          timeout=120, parse_image=False)
                except Exception as exc:
                    response = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
                try:
                    conn.sendall(json.dumps(response).encode() + b'\n')
                except OSError:
                    pass

    def close(self):
        if self.closed:
            return
        self.stop.set()
        self.listener.close()
        self.thread.join()
        self.directory.cleanup()
        self.closed = True
        self.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
