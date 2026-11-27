"""Start a device thread only after its complete call is reserved by StageGuard."""
from __future__ import annotations

from contextvars import copy_context
from threading import Event, Thread
from typing import Callable

from .stage_guard import OperationClass, StageGuard


def start_device_thread(
    target: Callable[[], None], *, name: str,
    operation: OperationClass = OperationClass.FRIDA, **identity,
) -> Thread:
    """Carry the immutable context and wait for admission, not completion.

    The caller may bound its own wait after this returns. The guard still owns
    the in-flight reservation until the target actually exits. Admission errors
    propagate to the caller before it can report a successful start.
    """
    context = copy_context()
    admitted = Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with StageGuard.operation_if_bound(operation, **identity):
                admitted.set()
                target()
        except BaseException as exc:
            if admitted.is_set():
                raise
            errors.append(exc)
        finally:
            admitted.set()

    worker = Thread(target=lambda: context.run(run), name=name, daemon=True)
    worker.start()
    admitted.wait()
    if errors:
        worker.join()
        raise errors[0]
    return worker
