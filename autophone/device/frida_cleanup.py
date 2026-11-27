"""Bound native release waits without claiming that cancellation released a handle."""
from __future__ import annotations

from threading import Timer
from typing import Callable, TypeVar

T = TypeVar("T")
FRIDA_CLEANUP_TIMEOUT_S = 15.0


def release_call(call: Callable[[], T]) -> T:
    try:
        import frida
    except ImportError:
        # A supplied transport can be used without the optional native SDK.
        return call()

    # Injected test transports need no native cancellable. Real Frida exposes it.
    factory = getattr(frida, "Cancellable", None)
    if factory is None:
        return call()
    cancellable = factory()
    timer = Timer(FRIDA_CLEANUP_TIMEOUT_S, cancellable.cancel)
    timer.daemon = True
    with cancellable:
        timer.start()
        try:
            return call()
        finally:
            timer.cancel()
            timer.join()
