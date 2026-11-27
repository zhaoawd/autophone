"""Cross-process locks shared by runtime processes and release updates."""
from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, TextIO

from autophone.paths import STATE_ROOT


class ReleaseLockBusy(RuntimeError):
    pass


RELEASE_LOCK_FD_ENV = "AUTOPHONE_RELEASE_LOCK_FD"


def release_lock_path(state_root: Path = STATE_ROOT) -> Path:
    return Path(state_root) / "runs" / "platform" / ".release-runtime.lock"


def inherited_exclusive_release_lock(
    state_root: Path = STATE_ROOT,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether this process inherited the updater's exclusive lock.

    The environment value is only a descriptor locator.  The descriptor must
    still be open, point at this state root's lock inode, and already permit an
    exclusive non-blocking flock.  A caller cannot bypass the runtime lock by
    setting an environment boolean or by opening an unrelated descriptor.
    """

    source = os.environ if environ is None else environ
    raw = source.get(RELEASE_LOCK_FD_ENV)
    if raw is None or not raw.isascii() or not raw.isdecimal():
        return False
    try:
        fd = int(raw)
    except ValueError:
        return False
    if fd < 0:
        return False
    try:
        inherited = os.fstat(fd)
        expected = release_lock_path(state_root).stat()
        if not stat.S_ISREG(inherited.st_mode):
            return False
        if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
            return False
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError, ValueError):
        return False
    return True


def inherited_lock_pass_fds(environ: Mapping[str, str]) -> tuple[int, ...]:
    """Return the updater-owned descriptor that a direct child must inherit."""

    raw = environ.get(RELEASE_LOCK_FD_ENV)
    if raw is None or not raw.isascii() or not raw.isdecimal():
        return ()
    try:
        fd = int(raw)
    except ValueError:
        return ()
    try:
        os.fstat(fd)
    except OSError:
        return ()
    return (fd,)


@contextmanager
def release_lock(
    state_root: Path = STATE_ROOT,
    *,
    exclusive: bool,
    blocking: bool = False,
) -> Iterator[TextIO]:
    root = Path(state_root)
    path = release_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), operation)
    except BlockingIOError as exc:
        handle.close()
        owner = "runtime" if exclusive else "release update"
        raise ReleaseLockBusy(f"{owner} holds {path}") from exc
    try:
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


__all__ = [
    "RELEASE_LOCK_FD_ENV",
    "ReleaseLockBusy",
    "inherited_exclusive_release_lock",
    "inherited_lock_pass_fds",
    "release_lock",
    "release_lock_path",
]
