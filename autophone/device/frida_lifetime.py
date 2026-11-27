"""Retain owned Frida attachments until detach is positively confirmed."""
from __future__ import annotations

from functools import wraps
import threading

from .frida_cleanup import release_call

from .stage_guard import OperationClass, StageAccessDenied, StageGuard


class FridaLifetime:
    def __init__(self, session, *, host: str, before_dispose=None):
        self.before_dispose = before_dispose
        self.session = session
        self.host = host
        self.guard = StageGuard.for_device_call()
        self.scripts = []
        self.closed = False
        self.closing = False
        self._mutex = threading.RLock()
        self._idle = threading.Condition(self._mutex)
        self._active_rpc = 0
        self._dispose_mutex = threading.Lock()
        self._release = self.guard.register_resource(self.dispose) if self.guard else lambda: None
        self.receipt = None

    def add_script(self, script):
        self.scripts.append(script)

    def exports(self, exports):
        return _OwnedExports(self, exports) if self.guard else exports

    def accept_event(self, append):
        with self._mutex:
            if not self.closed and not self.closing:
                append()

    def dispose(self):
        """Only unload/detach previously owned objects; never attach or run RPC."""
        if self.closed:
            return
        if self.guard is not None and not self.guard.disposing_resources():
            with self.guard.operation(OperationClass.FRIDA, frida_endpoint=self.host):
                return self._dispose()
        return self._dispose()

    def _dispose(self):
        with self._dispose_mutex:
            with self._idle:
                if self.closed:
                    return
                self.closing = True
                while self._active_rpc:
                    self._idle.wait()
            if self.before_dispose is not None:
                self.before_dispose()
            errors = []
            detached = getattr(self.session, 'is_detached', False) is True
            unloaded = detached
            if not detached:
                unloaded = True
                for script in reversed(self.scripts):
                    try:
                        release_call(script.unload)
                    except Exception as exc:
                        unloaded = False
                        errors.append(f'unload: {exc}')
                try:
                    release_call(self.session.detach)
                    detached = True
                except Exception as exc:
                    errors.append(f'detach: {exc}')
            self.receipt = dict(unloaded=unloaded, detached=detached, errors=errors)
            if not detached:
                raise RuntimeError('Frida detach unverified: ' + '; '.join(errors))
            self.closed = True
            self._release()


class _OwnedExports:
    def __init__(self, lifetime, exports):
        self._lifetime, self._exports = lifetime, exports

    def __getattr__(self, name):
        lifetime = self._lifetime
        method = getattr(self._exports, name)
        if not callable(method):
            return method

        @wraps(method)
        def invoke(*args, **kwargs):
            with lifetime.guard.operation(OperationClass.FRIDA, frida_endpoint=lifetime.host):
                with lifetime._mutex:
                    if lifetime.closed or lifetime.closing:
                        raise StageAccessDenied('frida_resource_closed')
                    lifetime._active_rpc += 1
                try:
                    return method(*args, **kwargs)
                finally:
                    with lifetime._idle:
                        lifetime._active_rpc -= 1
                        lifetime._idle.notify_all()
        return invoke
