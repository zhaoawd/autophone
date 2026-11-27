"""Local Agent ownership and PID inspection, independent of device execution."""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
from contextlib import contextmanager
from typing import Iterator

from autophone.util import write_text_atomic
from .source_identity import identity_file_path, write_identity_file
from .target import DeviceTarget


class AgentProcessError(RuntimeError):
    pass


def process_arguments(pid: int) -> list[str]:
    """Read argv boundaries; ps command text cannot preserve paths containing spaces."""
    if pid <= 0:
        return []
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return []
    except PermissionError as exc:
        raise AgentProcessError(f"cannot inspect Agent PID {pid}") from exc
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
        size = ctypes.c_size_t()
        def _sysctl_exited() -> bool:
            err = ctypes.get_errno()
            if err == errno.ESRCH:
                return True
            if err == errno.EINVAL:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                except PermissionError:
                    return False
            return False

        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0):
            if _sysctl_exited():
                return []
            raise AgentProcessError(f"cannot inspect Agent PID {pid}")
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0):
            if _sysctl_exited():
                return []
            raise AgentProcessError(f"cannot inspect Agent PID {pid}")
        data = buf.raw[:size.value]
        try:
            argc = struct.unpack("i", data[:4])[0]
            pos = data.index(b"\0", 4) + 1
            while data[pos] == 0:
                pos += 1
            arguments = []
            for _ in range(argc):
                end = data.index(b"\0", pos)
                arguments.append(os.fsdecode(data[pos:end]))
                pos = end + 1
            return arguments
        except (ValueError, IndexError, struct.error) as exc:
            raise AgentProcessError(f"invalid argv for PID {pid}") from exc
    if sys.platform.startswith("linux"):
        try:
            return [os.fsdecode(arg) for arg in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if arg]
        except FileNotFoundError:
            return []
    raise AgentProcessError("Agent PID inspection requires macOS or Linux")


def _agent_target(arguments: list[str]) -> Path | None:
    module = any(a == "-m" and b == "autophone.rig.main" for a, b in zip(arguments, arguments[1:]))
    entrypoint = any(Path(a).name == "autophone-rig-agent" for a in arguments[:2])
    if not (module or entrypoint):
        return None
    for index, argument in enumerate(arguments):
        if argument == "--target" and index + 1 < len(arguments):
            return Path(arguments[index + 1])
        if argument.startswith("--target="):
            return Path(argument.split("=", 1)[1])
    raise AgentProcessError("running Agent has no explicit target")


def _target_spec(target: DeviceTarget) -> dict:
    return {key: str(value.resolve()) if isinstance(value, Path) else value
            for key, value in vars(target).items()}


def _lock_path(rig_id: str) -> Path:
    # Fixed Unix location: TMPDIR, checkout and target directory must not change
    # the ownership namespace. Never unlink the flock inode, including on exit.
    root = Path(f"/tmp/autophone-agent-locks-{os.getuid()}")
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise AgentProcessError("Agent lock directory is not owned by this user")
    return root / (hashlib.sha256(rig_id.encode()).hexdigest() + ".lock")


def _recorded_pid(target_file: Path, pid_file: Path, *, own_pid: int | None = None) -> int | None:
    try:
        raw = pid_file.read_text().strip()
    except FileNotFoundError:
        return None
    if not raw.isdecimal() or int(raw) <= 0:
        return None
    pid = int(raw)
    if pid == own_pid:
        return None
    actual = _agent_target(process_arguments(pid))
    if actual is None:
        return None
    if not actual.is_absolute():
        raise AgentProcessError(f"Agent PID {pid} has a relative target; cannot verify identity")
    if actual.resolve() == target_file.resolve():
        return pid
    expected = DeviceTarget.load(target_file)
    observed = DeviceTarget.load(actual)
    if observed.rig_id != expected.rig_id:
        raise AgentProcessError(f"PID record belongs to a different Rig: {pid}")
    if _target_spec(observed) != _target_spec(expected):
        raise AgentProcessError(f"Agent PID {pid} has a conflicting target for {expected.rig_id}")
    return pid


def running_agent_pid(target_file: Path, pid_file: Path) -> int | None:
    """Return an existing owner; uncertain identity fails closed without deleting PID files."""
    target = DeviceTarget.load(target_file)
    with _lock_path(target.rig_id).open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            try:
                owner = json.load(handle)
                pid = int(owner["pid"])
                if owner["target"] != _target_spec(target) or pid <= 0:
                    raise AgentProcessError(f"Agent target conflicts with existing owner of {target.rig_id}")
            except (ValueError, KeyError, TypeError) as exc:
                raise AgentProcessError(f"Agent owner is not ready for {target.rig_id}") from exc
            actual = _agent_target(process_arguments(pid))
            if actual is None or not actual.is_absolute():
                raise AgentProcessError(f"cannot verify lock owner PID {pid}")
            if _target_spec(DeviceTarget.load(actual)) != owner["target"]:
                raise AgentProcessError(f"lock owner PID {pid} has a conflicting target")
            return pid
        # A legacy Agent may still own the PID file without holding the new lock.
        return _recorded_pid(target_file, pid_file)


@contextmanager
def agent_process_guard(target: DeviceTarget, target_file: Path) -> Iterator[None]:
    """Hold one OS-user/Rig lock before executor construction or either WebSocket."""
    pid_file = target.runtime_dir / "rig-agent.pid"
    identity_file = identity_file_path(target.runtime_dir)
    with _lock_path(target.rig_id).open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AgentProcessError(f"rig_agent_already_running: {target.rig_id}") from exc
        legacy = _recorded_pid(target_file, pid_file, own_pid=os.getpid())
        if legacy is not None:
            raise AgentProcessError(f"rig_agent_already_running: {target.rig_id} pid={legacy}")
        handle.seek(0)
        handle.truncate()
        json.dump({"pid": os.getpid(), "target": _target_spec(target)}, handle)
        handle.flush()
        write_text_atomic(pid_file, str(os.getpid()) + "\n")
        # BL-151：进程内代码是启动时快照，所以身份在这里写一次就不再变。写在持有
        # 进程锁之后、执行器构造之前，`scripts/start_vphone.sh` 因此可以只凭 runtime
        # dir 判断在跑的 Agent 是不是当前代码，不依赖控制面可达。
        write_identity_file(target.runtime_dir, pid=os.getpid(), rig_id=target.rig_id)
        try:
            yield
        finally:
            try:
                if pid_file.read_text().strip() == str(os.getpid()):
                    pid_file.unlink()
                    # 身份文件跟着 PID 文件走：留下它会让下一次 bring-up 拿一个
                    # 已经退出的进程的身份当成「在跑的 Agent 身份」。
                    identity_file.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
