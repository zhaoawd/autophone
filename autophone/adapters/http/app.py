from __future__ import annotations

import atexit

from autophone.paths import STATE_ROOT
from autophone.execution.release_lock import (
    inherited_exclusive_release_lock,
    release_lock,
)


_runtime_release_guard = None


def _acquire_runtime_release_lock() -> None:
    """Hold the runtime lease before imports or constructors can mutate state."""

    global _runtime_release_guard
    if _runtime_release_guard is not None:
        return
    if inherited_exclusive_release_lock(STATE_ROOT):
        return
    guard = release_lock(STATE_ROOT, exclusive=False, blocking=True)
    guard.__enter__()
    _runtime_release_guard = guard


def _release_runtime_release_lock() -> None:
    global _runtime_release_guard
    guard = _runtime_release_guard
    _runtime_release_guard = None
    if guard is not None:
        guard.__exit__(None, None, None)


# Direct ``uvicorn autophone.adapters.http.app:app`` imports do not pass through the
# CLI lock.  Acquire before importing stateful platform modules and keep the
# lease until FastAPI finishes its lifespan (or the importing process exits).
_acquire_runtime_release_lock()
atexit.register(_release_runtime_release_lock)


from .composition import build_dependencies


def create_app():
    dependencies = build_dependencies(_acquire_runtime_release_lock, _release_runtime_release_lock)
    dependencies.app.state.dependencies = dependencies
    return dependencies.app


app = create_app()
dependencies = app.state.dependencies
