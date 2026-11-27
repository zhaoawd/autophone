"""Stage authority for managed device calls, independent of transport adapters.

A ContextVar selects an immutable token. The issuing guard owns revocation and
checks the live lease on every device operation, including reads. A copied
context cannot restore an earlier stage or a revoked producer.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from inspect import signature
from pathlib import Path
import threading
import time
from typing import Callable, Iterator
import uuid


class OperationClass(str, Enum):
    HOST = 'host'
    READ = 'device_read'
    WRITE = 'device_write'
    SHELL = 'guest_shell'
    TRANSFER = 'device_file'
    FRIDA = 'frida'
    APP = 'app_lifecycle'
    LOCATION = 'location'
    VM = 'vm_lifecycle'


_DEVICE = frozenset(OperationClass) - {OperationClass.HOST, OperationClass.VM}
_POLICY = {
    stage: _DEVICE | {OperationClass.HOST}
    for stage in ('preflight', 'provisioning', 'execute', 'cleanup')
}
_POLICY['collect'] = frozenset({OperationClass.HOST})
_POLICY['recovery'] = frozenset(OperationClass)


class StageAccessDenied(RuntimeError):
    pass


class StageQuiesceError(StageAccessDenied):
    def __init__(self, failures):
        self.failures = tuple(failures)
        super().__init__('device_quiesce_unverified: ' + '; '.join(self.failures))


@dataclass(frozen=True)
class StageToken:
    authority_id: str
    owner_id: str
    rig_id: str
    generation: int
    stage: str
    epoch: int
    expires_at: float
    producer_id: str | None = None
    operations: frozenset[OperationClass] | None = None
    ancestors: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageBinding:
    guard: 'StageGuard'
    token: StageToken


_CURRENT: ContextVar[StageBinding | None] = ContextVar('device_stage_binding', default=None)
_DISPOSING: ContextVar['StageGuard | None'] = ContextVar('device_resource_disposal', default=None)
_ACTIVE_LOCK = threading.RLock()
_ACTIVE: set[str] = set()


class StageGuard:
    def __init__(
        self, *, owner_id: str, rig_id: str, generation: int,
        check_lease: Callable[[], None], recovery: bool = False,
        expires_at: float = float('inf'), clock: Callable[[], float] = time.monotonic,
        socket_path: str | Path | None = None, frida_endpoint: str | None = None,
        ssh_endpoint: tuple[str, int] | None = None,
        quarantine: Callable[[tuple[str, ...]], None] | None = None,
        check_release: Callable[[], None] | None = None,
    ) -> None:
        """`check_lease` admits every new device operation. `check_release` admits
        only the disposal of handles this guard already owns during quiesce; it
        defaults to `check_lease`. An owner may pass a narrower check that still
        refuses a lost lease or stale fencing but does not refuse cleanup after
        cooperative cancellation (BL-220)."""
        if not owner_id or not rig_id or type(generation) is not int or generation < 0 or not callable(check_lease):
            raise StageAccessDenied("stage_authority_identity_invalid")
        if check_release is not None and not callable(check_release):
            raise StageAccessDenied("stage_authority_identity_invalid")
        self.socket_path = str(Path(socket_path).resolve()) if socket_path is not None else None
        self.frida_endpoint = frida_endpoint
        self.ssh_endpoint = ssh_endpoint
        self.owner_id, self.rig_id, self.generation = owner_id, rig_id, generation
        self._check_lease, self._clock = check_lease, clock
        self._check_release = check_release if check_release is not None else check_lease
        self._recovery, self._expires_at = recovery, expires_at
        self._id = uuid.uuid4().hex
        self._epoch = 0
        self._stage = 'collect'
        self._closed = False
        self._scope_open = False
        self._revoked: set[str] = set()
        self._mutex = threading.RLock()
        self._condition = threading.Condition(self._mutex)
        self._inflight: dict[int, int] = {}
        self._issued: set[StageToken] = set()
        self._owner_thread = threading.get_ident()
        self._resources: dict[str, Callable[[], None]] = {}
        self._local_resources: set[str] = set()
        self._quiesce_failures: tuple[str, ...] = ()
        self._quarantine = quarantine

    def register_resource(self, close: Callable[[], None], *, requires_lease: bool = True) -> Callable[[], None]:
        """Retain a disposer even if the adapter drops its native handle.

        An already admitted native attach may return after revocation. It must
        still register its newly acquired handle before leaving the reservation.
        """
        with self._condition:
            binding = self.binding()
            if binding is None or binding.guard is not self:
                raise StageAccessDenied('stage_context_missing_or_foreign')
            if not self._inflight.get(threading.get_ident()):
                self.assert_allowed(OperationClass.FRIDA)
            key = uuid.uuid4().hex
            self._resources[key] = close
            if not requires_lease:
                self._local_resources.add(key)

        def released():
            with self._condition:
                self._resources.pop(key, None)
                self._local_resources.discard(key)
        return released

    def _quiesce(self, *, report_existing: bool = True) -> None:
        interruption: KeyboardInterrupt | SystemExit | None = None
        with self._condition:
            self._epoch += 1
            self._issued.clear()
            try:
                self._wait_idle()
            except (KeyboardInterrupt, SystemExit) as exc:
                # _wait_idle drains admitted calls before propagating an interrupt.
                # Dispose owned resources before allowing the ScreenLock to exit.
                interruption = exc
            if self._quiesce_failures:
                if report_existing:
                    raise StageQuiesceError(self._quiesce_failures)
                return
            resources = list(self._resources.items())
        failures = []
        for key, close in reversed(resources):
            try:
                # Only dispose owned handles; never issue a new device token.
                # A lost lease cannot authorize fresh cleanup I/O. Releasing an
                # owned handle is cleanup, so cancellation alone does not refuse it.
                if key not in self._local_resources:
                    self._check_release()
                disposal = _DISPOSING.set(self)
                try:
                    close()
                finally:
                    _DISPOSING.reset(disposal)
            except (Exception, KeyboardInterrupt, SystemExit) as exc:
                # Interrupted disposal leaves release unverified. Retain it and
                # finish other owned disposers before reporting quarantine.
                failures.append(f'{type(exc).__name__}: {exc}')
                if isinstance(exc, (KeyboardInterrupt, SystemExit)) and interruption is None:
                    interruption = exc
            else:
                with self._condition:
                    self._resources.pop(key, None)
                    self._local_resources.discard(key)
        if failures:
            self._quiesce_failures += tuple(failures)
            if self._quarantine is not None:
                try:
                    self._quarantine(self._quiesce_failures)
                except Exception as exc:
                    raise StageQuiesceError(self._quiesce_failures + (
                        f'quarantine persistence failed: {type(exc).__name__}: {exc}',
                    )) from (interruption if interruption is not None else exc)
        if failures or (report_existing and self._quiesce_failures):
            error = StageQuiesceError(self._quiesce_failures)
            if interruption is not None:
                raise error from interruption
            raise error
        if interruption is not None:
            raise interruption

    @property
    def quiesce_failures(self) -> tuple[str, ...]:
        """本 guard 已记录的 quiesce 失败（未确认释放的资源）；非空时后续切换都会拒绝。"""
        with self._condition:
            return self._quiesce_failures

    @classmethod
    def current(cls) -> 'StageGuard | None':
        binding = _CURRENT.get()
        return binding.guard if binding is not None else None

    def disposing_resources(self) -> bool:
        return _DISPOSING.get() is self

    @classmethod
    def binding(cls) -> StageBinding | None:
        return _CURRENT.get()

    @property
    def authority_id(self) -> str:
        """本 guard 的作用域身份；只读，供宿主侧记录（如执行侧最近帧）引用。"""
        return self._id

    @classmethod
    def active_in_process(cls) -> bool:
        """本进程是否存在活动 Stage 作用域。只读查询，不放宽 `for_device_call` 的拒绝规则。"""
        with _ACTIVE_LOCK:
            return bool(_ACTIVE)

    @classmethod
    def authority_active(cls, authority_id: str) -> bool:
        """给定作用域身份是否仍处于活动状态（作用域退出后为 False）。"""
        with _ACTIVE_LOCK:
            return authority_id in _ACTIVE

    @classmethod
    def for_device_call(cls) -> 'StageGuard | None':
        guard = cls.current()
        if guard is None:
            with _ACTIVE_LOCK:
                if _ACTIVE:
                    raise StageAccessDenied('stage_context_missing_or_foreign')
        return guard

    def transition(self, stage: str) -> StageToken:
        if not self._scope_open:
            raise StageAccessDenied("stage_scope_not_active")
        if threading.get_ident() != self._owner_thread:
            raise StageAccessDenied('stage_transition_requires_owner_thread')
        if stage not in _POLICY or (stage == 'recovery' and not self._recovery):
            raise StageAccessDenied('stage_not_authorized')
        with self._condition:
            if self._closed:
                raise StageAccessDenied('stage_authority_closed')
        self._quiesce()
        with self._condition:
            self._stage = stage
            token = StageToken(
                self._id, self.owner_id, self.rig_id, self.generation,
                stage, self._epoch, self._expires_at,
            )
            self._issued = {token}
            _CURRENT.set(StageBinding(self, token))
            return token

    @contextmanager
    def scope(self, stage: str) -> Iterator['StageGuard']:
        if self._scope_open or self._closed:
            raise StageAccessDenied("stage_scope_already_used")
        self._scope_open = True
        with _ACTIVE_LOCK:
            _ACTIVE.add(self._id)
        previous = _CURRENT.set(None)
        try:
            self.transition(stage)
            yield self
        finally:
            try:
                try:
                    self._quiesce(report_existing=False)
                finally:
                    with self._condition:
                        self._closed = True
                        self._scope_open = False
            finally:
                _CURRENT.reset(previous)
                with _ACTIVE_LOCK:
                    _ACTIVE.discard(self._id)

    @staticmethod
    @contextmanager
    def bind(binding: StageBinding | None):
        context = _CURRENT.set(binding)
        try:
            yield
        finally:
            _CURRENT.reset(context)

    def _validate(self, token: StageToken) -> None:
        if self._closed or token.authority_id != self._id or token.epoch != self._epoch:
            raise StageAccessDenied('stage_token_revoked')
        if (token.owner_id, token.rig_id, token.generation, token.stage) != (
            self.owner_id, self.rig_id, self.generation, self._stage,
        ):
            raise StageAccessDenied('stage_token_identity_mismatch')
        if self._clock() >= min(token.expires_at, self._expires_at):
            raise StageAccessDenied('stage_token_expired')
        if token not in self._issued:
            raise StageAccessDenied("stage_token_not_issued")
        if token.producer_id in self._revoked or any(value in self._revoked for value in token.ancestors):
            raise StageAccessDenied('producer_token_revoked')

    def assert_allowed(
        self, operation_class: OperationClass | str, *,
        rig_id: str | None = None, generation: int | None = None,
        socket_path: str | Path | None = None, frida_endpoint: str | None = None,
        ssh_endpoint: tuple[str, int] | None = None,
    ) -> None:
        binding = _CURRENT.get()
        if binding is None or binding.guard is not self:
            raise StageAccessDenied('stage_context_missing_or_foreign')
        try:
            operation = OperationClass(operation_class)
        except ValueError as exc:
            raise StageAccessDenied('operation_class_unknown') from exc
        with self._mutex:
            token = binding.token
            self._validate(token)
            if (rig_id is not None and rig_id != token.rig_id) or (
                generation is not None and generation != token.generation
            ):
                raise StageAccessDenied('stage_target_mismatch')
            if socket_path is not None and self.socket_path is not None and str(Path(socket_path).resolve()) != self.socket_path:
                raise StageAccessDenied('stage_target_mismatch')
            if frida_endpoint is not None and self.frida_endpoint is not None and frida_endpoint != self.frida_endpoint:
                raise StageAccessDenied('stage_target_mismatch')
            if ssh_endpoint is not None and self.ssh_endpoint is not None and ssh_endpoint != self.ssh_endpoint:
                raise StageAccessDenied('stage_target_mismatch')
            if operation not in _POLICY[token.stage] or (
                token.operations is not None and operation not in token.operations
            ):
                raise StageAccessDenied(f'operation_not_allowed_in_stage: {token.stage}/{operation.value}')
        if operation != OperationClass.HOST:
            self._check_lease()
            # The lease check may block while the owner advances the stage.
            with self._mutex:
                self._validate(token)

    def wait_for_idle(self, timeout_s: float) -> bool:
        """Wait without revoking the owner; this does not verify resource release.

        Run finalization may still need its live token to terminate its own App
        when native detach waits. A subsequent transition must verify release.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            self.assert_allowed(OperationClass.HOST)
            if self._inflight.get(threading.get_ident()):
                raise StageAccessDenied("stage_transition_during_device_operation")
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(.1, remaining))
            return True

    def _wait_idle(self) -> None:
        if self._inflight.get(threading.get_ident()):
            raise StageAccessDenied("stage_transition_during_device_operation")
        interrupted = None
        while self._inflight:
            try:
                self._condition.wait(timeout=0.1)
            except BaseException as exc:
                interrupted = exc
        if interrupted is not None:
            raise interrupted

    @contextmanager
    def operation(self, operation_class: OperationClass | str, **identity):
        """Reserve the complete transport call; stage transitions wait for it."""
        thread = threading.get_ident()
        with self._condition:
            self.assert_allowed(operation_class, **identity)
            self._inflight[thread] = self._inflight.get(thread, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                count = self._inflight[thread] - 1
                if count:
                    self._inflight[thread] = count
                else:
                    del self._inflight[thread]
                self._condition.notify_all()

    @classmethod
    @contextmanager
    def operation_if_bound(cls, operation_class: OperationClass | str, **identity):
        guard = cls.current() if operation_class == OperationClass.HOST else cls.for_device_call()
        if guard is None:
            yield
        else:
            with guard.operation(operation_class, **identity):
                yield

    @classmethod
    def check_if_bound(cls, operation_class: OperationClass | str) -> None:
        """Legacy bridge: enforce an existing managed scope; do not mint one."""
        guard = cls.for_device_call()
        if guard is not None:
            guard.assert_allowed(operation_class)

    @classmethod
    def producer_binding(cls, operations: frozenset[OperationClass]) -> StageBinding | None:
        binding = _CURRENT.get()
        if binding is None:
            return None
        guard = binding.guard
        # Validate the parent before deriving a restricted token.
        for operation in operations:
            guard.assert_allowed(operation)
        from dataclasses import replace
        parent = binding.token
        allowed = operations if parent.operations is None else operations & parent.operations
        token = replace(
            parent, producer_id=uuid.uuid4().hex, operations=allowed,
            ancestors=parent.ancestors + ((parent.producer_id,) if parent.producer_id else ()),
        )
        with guard._mutex:
            guard._validate(parent)
            guard._issued.add(token)
        return StageBinding(guard, token)

    def revoke_producer(self, binding: StageBinding) -> None:
        if binding.guard is not self or binding.token.producer_id is None:
            raise StageAccessDenied('producer_token_identity_mismatch')
        with self._mutex:
            self._revoked.add(binding.token.producer_id)


def uds_operation(command: str) -> OperationClass:
    if command in {'screenshot', 'app_list', 'ping', 'runtime_info', 'capabilities'}:
        return OperationClass.READ
    if command in {'file_get', 'file_put'}:
        return OperationClass.TRANSFER
    if command == 'shell':
        return OperationClass.SHELL
    if command in {'app_launch', 'app_terminate', 'open_url', 'ipa_install'}:
        return OperationClass.APP
    if command.startswith('location'):
        return OperationClass.LOCATION
    return OperationClass.WRITE


def guarded_device_operation(operation: OperationClass, *, identity=None):
    """Keep a synchronous adapter call admitted until its transport returns.

    Identity receives bound arguments, including defaults, so positional and
    keyword calls undergo the same target check. Host-only helpers stay outside
    this decorator.
    """
    def decorate(function):
        parameters = signature(function)

        @wraps(function)
        def call(*args, **kwargs):
            arguments = parameters.bind(*args, **kwargs)
            arguments.apply_defaults()
            target = identity(arguments.arguments) if identity is not None else {}
            instance = arguments.arguments.get('self')
            binding = getattr(instance, '_device_stage_binding', None)
            guard = binding.guard if binding is not None else StageGuard.for_device_call()
            if guard is None:
                return function(*args, **kwargs)
            if instance is not None and binding is None:
                instance._device_stage_binding = StageGuard.binding()
            with guard.operation(operation, **target):
                return function(*args, **kwargs)

        return call
    return decorate
