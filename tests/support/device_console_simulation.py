"""模拟 B 端：真实 HTTP/WebSocket、会话锁及独立 PostgreSQL schema，替身手机驱动。

运行：AUTOPHONE_TEST_POSTGRES_DSN=postgresql:///postgres python -m
 tests.support.device_console_simulation --port 8976 --b-port 8977
只监听 loopback；退出后删除本次 schema 和临时设备锁。不会连接真实 VM。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import uuid

import httpx2 as httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from PIL import Image, ImageDraw
import uvicorn

from autophone.application.access_grants import ControlPlaneGrantStore
from autophone.application.device_access import DeviceAccessService
from autophone.application.device_console import DeviceConsoleManager, ControlPlaneReservations
from autophone.application.device_session import DeviceSessionOps, RemoteDeviceSessions
from autophone.application.embed_access import AccessPlatform, EmbedAccessRegistry, credential_hash
from autophone.control import ControlPlane, PostgresControlStore
from autophone.control.device_session import mount_device_session_channel
from autophone.device.lock import ScreenLock
from autophone.rig.device_session import DeviceSessionAgent


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise RuntimeError('simulation startup timed out')


class PhoneDriver:
    """每台设备独立保存动作和画面；不包含任何真实设备调用。"""
    def __init__(self, device):
        self.device = device
        self.calls = []
        self.frame_count = 0
        self.guard = threading.Lock()
        self.fail_cleanup = False
        self.action_started = None
        self.action_release = None

    def screenshot(self):
        with self.guard:
            self.frame_count += 1
            count = len(self.calls)
        picture = Image.new('RGB', (430, 932), (225, 235, 250))
        draw = ImageDraw.Draw(picture)
        draw.text((35, 60), f'SIMULATED PHONE: {self.device}', fill='black')
        draw.text((35, 110), f'ACTIONS: {count}', fill='black')
        draw.rectangle((40, 180, 390, 420), fill=(60 + count * 17 % 160, 100, 160))
        out = io.BytesIO()
        picture.save(out, format='JPEG')
        return out.getvalue(), 430, 932

    def _act(self, *event):
        if self.action_started is not None:
            self.action_started.set()
        if self.action_release is not None and not self.action_release.wait(timeout=10):
            raise RuntimeError("simulation action release timed out")
        with self.guard:
            self.calls.append(event)

    def tap(self, x, y): self._act('tap', x, y)
    def swipe(self, x1, y1, x2, y2, *, ms): self._act('swipe', x1, y1, x2, y2, ms)
    def type_text(self, text): self._act('type', text)
    def press_key(self, key): self._act('key', key)
    def cleanup(self, device, reason):
        if self.fail_cleanup:
            raise RuntimeError('simulated cleanup failure')
        self.press_key('home')


class Simulation:
    """一实例一随机 schema；HTTP API 的生产 lifespan 在实验中禁用。"""
    def __init__(self, dsn, *, port=0, b_port=0, count=3, drivers=None, lock=None):
        if not dsn:
            raise ValueError('AUTOPHONE_TEST_POSTGRES_DSN is required')
        self.port, self.b_port = port or free_port(), b_port or free_port()
        self.url, self.b_url = f'http://127.0.0.1:{self.port}', f'http://127.0.0.1:{self.b_port}'
        self.schema = 'test_sim_' + uuid.uuid4().hex[:16]
        self.store = PostgresControlStore(dsn, schema=self.schema)
        if (drivers is None) != (lock is None):
            raise ValueError('Injected drivers require an explicit shared ScreenLock')
        self.injected_drivers, self.injected_lock = drivers, lock
        self.devices = list(drivers) if drivers is not None else [f'sim-{i+1}' for i in range(count)]
        self.admin_auth = ('simulation-admin', secrets.token_urlsafe(20))
        self.secret = secrets.token_urlsafe(32)
        self.headers = {'Authorization': 'Bearer ' + self.secret}
        self.drivers, self.managers, self.agents = {}, {}, []
        self.stack = contextlib.ExitStack()

    def _server(self, app, port):
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                              log_level='error', lifespan='off'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        def close():
            server.should_exit = True
            thread.join(timeout=10)
        self.stack.callback(close)
        wait_for(lambda: server.started)

    def __enter__(self):
        try:
            return self._start()
        except BaseException:
            self.stack.close()
            raise

    def _start(self):
        from autophone.adapters.http.app import dependencies as api
        self.store.initialize()
        self.stack.callback(self.store._drop_schema_for_test)
        self.control = ControlPlane(self.store)
        self.store.register_host('sim-host')
        for device in self.devices:
            self.store.register_rig(device, host_id='sim-host', generation=1)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='console-sim-'))
        self.lock = self.injected_lock or ScreenLock(root=Path(self.temp) / 'locks')
        channel = FastAPI()
        mount_device_session_channel(channel, host_snapshot=lambda _: {
            'rigs': [{'host_id': 'sim-host', 'rig_id': d, 'generation': 1} for d in self.devices],
            'targets': [{'host_id': 'sim-host', 'rig_id': d, 'rig_generation': 1,
                         'vm_dir': '/simulation/' + d} for d in self.devices],
        })
        channel_port = free_port()
        self._server(channel, channel_port)
        for device in self.devices:
            driver = self.drivers[device] = (
                self.injected_drivers[device] if self.injected_drivers is not None else PhoneDriver(device))
            manager = self.managers[device] = DeviceConsoleManager(
                lock=self.lock, driver_factory=lambda drv=driver: drv,
                reservations=ControlPlaneReservations(lambda: self.control),
                release_cleanup=driver.cleanup,
            )
            self.stack.callback(manager.shutdown)
            manager.start_scanner()
            ops = DeviceSessionOps(console=manager, screenshot=lambda drv=driver: drv.screenshot()[0],
                status=lambda d: {'device': d, 'online': True, 'lock': self.lock.live_status(device=d)},
                target=lambda d=device: {'device': d})
            agent = DeviceSessionAgent(
                f'ws://127.0.0.1:{channel_port}/api/control/device-session/ws',
                host_id='sim-host', rig_id=device, rig_generation=1, devices=[device], ops=ops)
            agent.start()
            self.agents.append(agent)
            self.stack.callback(agent.stop)
            wait_for(agent.connected.is_set)
        remote = RemoteDeviceSessions(f'http://127.0.0.1:{channel_port}')
        self.registry = EmbedAccessRegistry({'sim-b': AccessPlatform('sim-b', (self.b_url,),
            credential_hash=credential_hash(self.secret), device_pool=tuple(self.devices), shared_account=True)},
            grants=ControlPlaneGrantStore(lambda: self.control), token_key=secrets.token_bytes(32))
        self.service = DeviceAccessService(access=self.registry, call=remote.call,
            connected=lambda: set(self.devices), embed_base_url=self.url)
        for key, value in {'embed_access': self.registry, 'device_access': self.service,
                           '_configured_control_or_http_error': lambda: self.control,
                           'control_http_base_url': lambda: f'http://127.0.0.1:{channel_port}',
                           '_BASIC_AUTH_USERS': dict([self.admin_auth])}.items():
            previous = getattr(api, key)
            setattr(api, key, value)
            self.stack.callback(setattr, api, key, previous)
        self._server(api.app, self.port)
        self._server(self.b_app(), self.b_port)
        return self

    def b_app(self):
        app = FastAPI()
        @app.get('/', response_class=HTMLResponse)
        def page():
            html = Path(__file__).with_name('device_console_simulation.html').read_text()
            if self.injected_drivers is not None:
                html = html.replace('设备使用替身驱动', '设备使用真实 UDS 驱动（临时验收）')
            return html

        @app.api_route('/b/{path:path}', methods=['GET', 'POST', 'DELETE'])
        async def proxy(path: str, request: Request):
            # 仅本机模拟后端使用平台凭证；页面既不接收也不保存该凭证。
            if path != 'devices' and path != 'sessions' and not (
                path.startswith('sessions/') and len(path.split('/')) == 2):
                return Response(status_code=404)
            async with httpx.AsyncClient() as client:
                result = await client.request(request.method, self.url + '/api/access/' + path,
                    headers={**self.headers, 'Content-Type': 'application/json'},
                    content=await request.body(), timeout=30)
            return Response(result.content, status_code=result.status_code, media_type='application/json')
        return app

    def __exit__(self, *_):
        self.stack.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8976)
    parser.add_argument('--b-port', type=int, default=8977)
    args = parser.parse_args()
    with Simulation(os.environ.get('AUTOPHONE_TEST_POSTGRES_DSN'), port=args.port, b_port=args.b_port) as sim:
        print(f'Simulated B platform: {sim.b_url}', flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
