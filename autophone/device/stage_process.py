"""Parent-owned authority and lifetime for spawned device workers.

The child never receives a copied lease checker. It asks the parent to validate
its immutable token at startup and dispatch. The parent retains an in-flight
reservation until the OS confirms worker exit, including after revocation.
"""
from __future__ import annotations

from dataclasses import asdict
import os
import threading
import time

from .stage_guard import OperationClass, StageAccessDenied, StageGuard
from .stage_threads import start_device_thread


def _authorized_worker(channel, token, identity, target, args):
    mutex = threading.Lock()
    stopped = threading.Event()

    def check_parent():
        with mutex:
            channel.send(token)
            if not channel.poll(5.0) or channel.recv() is not True:
                raise StageAccessDenied('worker_authority_denied')

    def watch_authority():
        # The parent's supervisor cannot terminate us after a hard parent crash.
        # Even a worker blocked inside an SDK must stop using that lost authority.
        while not stopped.wait(.1):
            try:
                check_parent()
            except BaseException:
                if not stopped.is_set():
                    os._exit(73)
                return

    guard = StageGuard(
        owner_id=token['owner_id'], rig_id=token['rig_id'],
        generation=token['generation'], check_lease=check_parent,
        expires_at=token['expires_at'], recovery=token['stage'] == 'recovery',
        **identity,
    )
    try:
        with guard.scope(token['stage']):
            watchdog = threading.Thread(target=watch_authority, name='device-parent-watchdog', daemon=True)
            watchdog.start()
            binding = (
                StageGuard.producer_binding(token['operations'])
                if token['operations'] is not None else StageGuard.binding()
            )
            with StageGuard.bind(binding), guard.operation(OperationClass.FRIDA):
                target(*args)
    finally:
        stopped.set()
        channel.close()


class _ManagedProcess:
    def __init__(self, context, *, target, args, daemon):
        self.binding = StageGuard.binding()
        assert self.binding is not None
        guard = self.binding.guard
        guard.assert_allowed(OperationClass.FRIDA)
        self.token = asdict(self.binding.token)
        self.parent, self.child = context.Pipe(duplex=True)
        identity = dict(socket_path=guard.socket_path, frida_endpoint=guard.frida_endpoint,
                        ssh_endpoint=guard.ssh_endpoint)
        self.process = context.Process(
            target=_authorized_worker,
            args=(self.child, self.token, identity, target, args), daemon=daemon,
        )
        self._process_lock = threading.RLock()
        self.monitor = None

    def __getattr__(self, name):
        return getattr(self.process, name)

    def is_alive(self):
        with self._process_lock:
            return self.process.is_alive()

    def join(self, timeout=None):
        # multiprocessing.Process waitpid/poll is not thread-safe. Serialize
        # OS status reads without blocking authorization service for a full join.
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            with self._process_lock:
                self.process.join(.05 if remaining is None else min(.05, remaining))
                if not self.process.is_alive():
                    return
            if deadline is not None and time.monotonic() >= deadline:
                return

    def terminate(self):
        with self._process_lock:
            self.process.terminate()

    def kill(self):
        with self._process_lock:
            self.process.kill()

    def _stop_and_join(self):
        if self.is_alive():
            self.terminate()
            self.join(.5)
        if self.is_alive():
            self.kill()
        # Do not release the stage reservation while the child can do I/O.
        while self.is_alive():
            self.join(.1)

    def _supervise(self):
        try:
            while self.is_alive():
                self.binding.guard.assert_allowed(OperationClass.FRIDA)
                if self.parent.poll(.05):
                    requested = self.parent.recv()
                    self.binding.guard.assert_allowed(OperationClass.FRIDA)
                    if requested != self.token:
                        raise StageAccessDenied('worker_token_mismatch')
                    self.parent.send(True)
        except Exception:
            self._stop_and_join()
        finally:
            self.parent.close()

    def start(self):
        current = StageGuard.binding()
        if current is not None and current.guard is not self.binding.guard:
            raise StageAccessDenied('stage_context_missing_or_foreign')
        # Creation, not a newer stage on the same guard, owns this dispatch.
        with StageGuard.bind(self.binding):
            self.binding.guard.assert_allowed(OperationClass.FRIDA)
            self.process.start()
            self.child.close()
            try:
                self.monitor = start_device_thread(self._supervise, name='device-worker-authority')
            except BaseException:
                self._stop_and_join()
                self.parent.close()
                raise


def device_process(context, *, target, args, daemon=True):
    if StageGuard.for_device_call() is None:
        return context.Process(target=target, args=args, daemon=daemon)
    return _ManagedProcess(context, target=target, args=args, daemon=daemon)
