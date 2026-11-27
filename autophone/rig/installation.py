"""Install a Driver Build through the Assignment's existing device authority."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import socket
from typing import Any, Mapping

from autophone.device.stage_guard import OperationClass, StageGuard
from autophone.device.stage_threads import start_device_thread
from .provisioning import RigProvisioner, ProvisioningError

_CURRENT: ContextVar[Any] = ContextVar('assignment_installation', default=None)
_CHANNEL = 'AUTOPHONE_INSTALL_CHANNEL_FD'
_LIMIT = 1024 * 1024


class AssignmentInstallation:
    def __init__(self, device, *, app: str, capability_state: Mapping[str, Any]):
        self.device = device
        self.app = app
        self.capability_state = dict(capability_state)

    def ensure(self, request: Mapping[str, Any]) -> dict[str, Any]:
        target = self.device.target
        guard = StageGuard.current()
        if guard is None:
            raise ProvisioningError('installation requires an active Assignment StageGuard')
        with guard.operation(OperationClass.APP, rig_id=target.rig_id,
                             generation=target.rig_generation):
            fence = self.device.write_fence
            if (guard.owner_id != f"{fence.assignment_id}:{fence.attempt_no}"
                    or guard.socket_path != str(target.socket.resolve())):
                raise ProvisioningError('installation StageGuard identity mismatch')
            if fence.target != target:
                raise ProvisioningError('installation fence target mismatch')
            fence.check()
            if (request.get('device') != target.rig_id
                    or Path(str(request.get('socket') or '')).resolve() != target.socket.resolve()):
                raise ProvisioningError('installation requested a different Rig or socket')
            ref = dict(request['build_ref'])
            if ref.get('app') != self.app:
                raise ProvisioningError('installation requested a different App')
            bundle_id = str(request.get('bundle_id') or '')
            if not bundle_id:
                raise ProvisioningError('installation requires bundle_id')
            transport = fence.transport
            publication = {}

            def publish(state, capabilities):
                fence.check()
                row = transport.publish_assignment_observed_state(
                    target, fence.assignment_id, fence.attempt_no, fence.fencing,
                    state, capabilities,
                )
                if (row.get('host_id') != target.host_id or row.get('rig_id') != target.rig_id
                        or row.get('generation') != target.rig_generation
                        or row.get('observed_state') != state):
                    raise ProvisioningError('installation observation publication was not acknowledged')
                publication.update(row)

            result = RigProvisioner(self.device, publish).ensure(
                {'build_ref': ref}, app=self.app, bundle_id=bundle_id,
                capability_state=self.capability_state,
                observed_state=transport.read_observed_state(target),
            ).to_dict()
            return {
                **result,
                'authority': {
                    'host_id': target.host_id, 'rig_id': target.rig_id,
                    'rig_generation': target.rig_generation,
                    'assignment_id': fence.assignment_id,
                    'attempt_no': fence.attempt_no, 'fencing': fence.fencing,
                },
                'observed_state': publication['observed_state'],
            }


@contextmanager
def installation_scope(installation):
    token = _CURRENT.set(installation)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _receive(stream):
    raw = stream.readline(_LIMIT + 1)
    if len(raw) > _LIMIT or not raw.endswith(b'\n'):
        raise ProvisioningError('installation channel response missing or oversized')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ProvisioningError('installation channel requires an object')
    return value


def install_in_assignment(*, build_ref, bundle_id: str, device: str, socket_path: str):
    request = dict(build_ref=dict(build_ref), bundle_id=bundle_id,
                   device=device, socket=socket_path)
    installation = _CURRENT.get()
    if installation is not None:
        return installation.ensure(request)
    fd = os.environ.get(_CHANNEL)
    if not fd:
        raise ProvisioningError('Driver installation requires the current Assignment installation channel')
    with socket.socket(fileno=os.dup(int(fd))) as channel, channel.makefile('rwb', buffering=0) as stream:
        channel.sendall(json.dumps(request).encode() + b'\n')
        reply = _receive(stream)
    if reply.get('ok') is not True:
        raise ProvisioningError(str(reply.get('error') or 'installation channel rejected'))
    return reply['result']


@contextmanager
def installation_channel(build_evidence: Mapping[str, Any]):
    """One deploy subprocess gets one operation; no socket path or reusable credential."""
    installation = _CURRENT.get()
    if installation is None:
        yield {}
        return
    parent, child = socket.socketpair()

    def serve():
        with parent, parent.makefile('rwb', buffering=0) as stream:
            try:
                request = _receive(stream)
                ref = request.get('build_ref') or {}
                if any(ref.get(key) != build_evidence.get(key)
                       for key in ('build_id', 'artifact_sha256')):
                    raise ProvisioningError('Driver install differs from coordinator Build evidence')
                reply = {'ok': True, 'result': installation.ensure(request)}
            except Exception as exc:  # structured failure returned to the Driver
                reply = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
            try:
                parent.sendall(json.dumps(reply).encode() + b'\n')
            except (BrokenPipeError, ConnectionResetError):
                pass

    worker = None
    try:
        worker = start_device_thread(serve, name='driver-installation', operation=OperationClass.APP)
        target = installation.device.target
        # 基底用 `code_root_subprocess_env()` 而不是 `os.environ`：deploy 子进程同样是
        # 被当脚本拉起的 App 驱动器，仓库根不进 PYTHONPATH 它就 import 不到 autophone。
        from autophone.paths import code_root_subprocess_env

        yield {'env': {**code_root_subprocess_env(), _CHANNEL: str(child.fileno()),
                       'AP_DEVICE': target.rig_id, 'VPHONE_SOCK': str(target.socket),
                       'VPHONE_CLI_DIR': str(target.cli_dir), 'VM_DIR': str(target.vm_dir)},
               'pass_fds': (child.fileno(),)}
    finally:
        # EOF also terminates the server when the child exits before requesting installation.
        child.close()
        if worker is not None:
            worker.join()
        else:
            parent.close()
