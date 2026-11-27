from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from autophone.paths import STATE_ROOT
from autophone.util import now_iso as _now
from autophone.util import write_json_atomic

_ADMISSION = ContextVar('screen_lock_admission', default=None)


@contextmanager
def screen_admission(callback):
    """Bind a creation-time admission check to the actual acquired ScreenLock."""
    token = _ADMISSION.set(callback)
    try:
        yield
    finally:
        _ADMISSION.reset(token)


@dataclass(frozen=True)
class LockState:
    device: str
    locked: bool
    run_id: str | None = None
    acquired_at: str | None = None
    pid: int | None = None
    owner_kind: str | None = None
    owner_id: str | None = None


class ScreenLockBusy(RuntimeError):
    """非阻塞 acquire 时设备已被另一碰屏工作流持有。"""


class ScreenQuarantined(ScreenLockBusy):
    """Unconfirmed device cleanup blocks subsequent compatibility owners."""


class ScreenQuarantineMarkerPresent(ScreenQuarantined):
    """取 lease 时隔离标记已存在，lease 未取得（BL-224）。

    与 ``ScreenQuarantined`` 的另一来源（本 owner 取得 lease 后 quiesce 未确认、隔离回执未落盘）
    区分：抛出本异常时调用方没有在这把锁下访问过设备。
    """

    def __init__(self, device: str, path: Path) -> None:
        super().__init__(f"device={device!r} quarantined: device_quiesce_unverified")
        self.device = device
        self.path = path


class QuarantineReleaseRefused(RuntimeError):
    """解除 ScreenLock 隔离标记的前置或核对不满足；标记保持原位（BL-223）。"""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


QUARANTINE_RELEASE_SCHEMA = "screen-quarantine-release/2"
# 设备名作文件名的一段：不含路径分隔符；`vphone:vm` 这类历史名带冒号。
_DEVICE_NAME = re.compile(r"[A-Za-z0-9._:-]+")


def _fsync_dir(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _parse_quarantine_marker(raw: bytes, *, device: str) -> dict[str, Any]:
    """校验 ``quarantine()`` 写出的字段；``pid`` 自 BL-223 起写入，旧标记没有。"""
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuarantineReleaseRefused("quarantine_marker_unparsable", f"not JSON: {exc}") from exc
    problems = []
    if not isinstance(marker, dict):
        raise QuarantineReleaseRefused("quarantine_marker_unparsable", "marker is not a JSON object")
    if marker.get("device") != device:
        problems.append(f"device={marker.get('device')!r}")
    for key in ("owner_id", "at", "reason"):
        if not isinstance(marker.get(key), str) or not marker[key].strip():
            problems.append(f"{key} missing")
    failures = marker.get("failures")
    if not isinstance(failures, list) or not all(isinstance(item, str) for item in failures):
        problems.append("failures is not a list of strings")
    if "pid" in marker and (type(marker["pid"]) is not int or marker["pid"] <= 0):
        problems.append("pid is not a positive integer")
    if problems:
        raise QuarantineReleaseRefused(
            "quarantine_marker_unparsable", "; ".join(problems), details={"marker": marker},
        )
    return marker


def process_started_at(pid: int) -> datetime | None:
    """进程启动时间（本机时区）；读不到返回 ``None``。用于识别 pid 复用。"""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=5, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    text = " ".join(result.stdout.split())
    if result.returncode != 0 or not text:
        return None
    try:
        return datetime.strptime(text, "%a %b %d %H:%M:%S %Y").astimezone()
    except ValueError:
        return None


# 人正实时占着设备的三种方式：浏览器录制、浏览器纯远程操作、桌面 Tk 录制。
# 都不排队——run 见到它们立即 fail（人还在设备前面，排队等于让 run 在无人预期时抢屏）。
HUMAN_OWNER_KINDS = frozenset({"web_recording", "web_control", "desktop_recording"})
OWNER_KINDS = frozenset({"run"}) | HUMAN_OWNER_KINDS
# Keep the flock descriptor reachable if its isolation receipt could not be
# persisted. Process-crash recovery remains the responsibility of C0/R0.
_QUARANTINE_HOLDS: dict[tuple[str, str], "ScreenLease"] = {}


def default_screen_lock_root() -> Path:
    """让标准 linked worktree 与主 checkout 共享同一组设备锁。

    ``.git`` 文件也用于 ``--separate-git-dir`` checkout 和 submodule；这两种
    合法布局不表示当前目录是 linked worktree，继续使用当前 ``STATE_ROOT``。
    语法损坏或指向不存在目录的 metadata 无法安全分类，保持 fail-closed。
    """
    local_root = STATE_ROOT / "runs" / ".locks"
    if str(os.environ.get("AUTOPHONE_STATE_DIR") or "").strip():
        return local_root
    git_entry = STATE_ROOT / ".git"
    if git_entry.is_dir() or not git_entry.exists():
        return local_root
    try:
        line = git_entry.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Git metadata is unreadable: {git_entry}") from exc
    prefix = "gitdir: "
    if not line.startswith(prefix) or "\n" in line:
        raise RuntimeError(f"Git worktree metadata is malformed: {git_entry}")
    raw_git_dir = Path(line[len(prefix):])
    git_dir = raw_git_dir if raw_git_dir.is_absolute() else STATE_ROOT / raw_git_dir
    try:
        git_dir = git_dir.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Git worktree directory is unavailable: {git_dir}") from exc
    if not git_dir.is_dir():
        raise RuntimeError(f"Git worktree directory is unavailable: {git_dir}")
    if git_dir.parent.name != "worktrees":
        # A well-formed .git file can point at a separate Git directory or a
        # superproject's .git/modules/<name>.  Neither layout identifies a
        # linked-worktree common checkout, so preserve the historical local root.
        return local_root
    common_git_dir = git_dir.parent.parent
    if not common_git_dir.is_dir():
        raise RuntimeError(
            f"Git common directory has an unexpected layout: {common_git_dir}",
        )
    if common_git_dir.name != ".git":
        # A linked worktree created from a --separate-git-dir checkout has no
        # directory-layout mapping back to the main checkout.  Treat it like
        # the other valid non-standard layouts and keep the lock local.
        return local_root
    return common_git_dir.parent / "runs" / ".locks"


@dataclass
class ScreenLease:
    """A held ScreenLock fd.  The caller owns it until ``release``."""

    lock: "ScreenLock"
    device: str
    owner_kind: str
    owner_id: str
    fh: Any
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.lock._release_lease(self)
        self.released = True

    def fileno(self) -> int:
        """底层 fd —— 交给 ``subprocess`` 的 ``pass_fds`` 让子进程继承这把锁。"""
        return self.fh.fileno()

    def detach(self) -> None:
        """把锁交给已经继承了本 fd 的子进程（**必须在 spawn 之后调用**）。

        flock 锁在**打开文件描述**上，不在进程上：父子两个 fd 指向同一个描述，
        全部关闭才释放。所以交接 = 父进程只关自己那份，**绝不能** `LOCK_UN`
        （解锁任一 fd 会把整个描述的锁解掉），也不能改状态文件——设备并没有空出来，
        只是换了个进程按着。子进程侧用 ``ScreenLock.adopt_lease`` 接管。
        """
        if self.released:
            raise RuntimeError("lease already released")
        self.released = True   # 父进程从此不再拥有它：别再 release/detach 第二次
        self.fh.close()


class ScreenLock:
    """设备屏幕锁。**构造无文件系统副作用**：只记住锁根，不建目录。

    构造期建目录会让「只是构造了一个 ScreenLock」与「这台机器上确实有锁活动」在
    文件系统上无法区分：默认锁根是主 checkout 的 `runs/.locks`（生产上刻意让 linked
    worktree 与主 checkout 共用同一组设备锁），任何在 import 期构造 `ScreenLock` 的
    组合根都会凭空在别人的工作树里建出这个目录（BL-122）。

    锁根改由每个**写入点**在写之前建（`_ensure_root`）；只读方法在锁根不存在时按
    「无锁」返回，不建目录也不抛错。
    """

    def __init__(self, *, root: Path | None = None) -> None:
        self.root = root or default_screen_lock_root()
        self._quarantine_pending: set[str] = set()

    def _ensure_root(self) -> Path:
        """写入前建锁根。所有会往锁根里写文件的方法都必须先调它。"""
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def _lock_path(self, device: str) -> Path:
        return self.root / f"{device}.lock"

    def _state_path(self, device: str) -> Path:
        return self.root / f"{device}.json"

    def _recording_path(self, device: str) -> Path:
        return self.root / f"{device}.recording.json"

    def _quarantine_path(self, device: str) -> Path:
        return self.root / f"{device}.quarantine.json"

    def quarantine(self, *, device: str, owner_id: str, failures: tuple[str, ...]) -> None:
        self._ensure_root()
        self._quarantine_pending.add(device)
        # pid 记录写标记的进程：未确认释放的 Frida 会话等资源留在该进程里，解除前要求它已退出。
        path = write_json_atomic(self._quarantine_path(device), {
            "device": device, "owner_id": owner_id, "at": _now(),
            "reason": "device_quiesce_unverified", "failures": list(failures),
            "pid": os.getpid(),
        })
        with path.open('rb') as stream:
            os.fsync(stream.fileno())
        _fsync_dir(self.root)
        self._quarantine_pending.discard(device)

    def quarantine_status(self, *, device: str) -> dict[str, Any] | None:
        """只读投影：标记不存在返回 ``None``；存在时给出路径、sha256 与解析结果，不抛错。"""
        path = self._quarantine_path(device)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            return {"device": device, "path": str(path), "error": f"{type(exc).__name__}: {exc}"}
        status: dict[str, Any] = {
            "device": device, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
        }
        try:
            status["marker"] = _parse_quarantine_marker(raw, device=device)
        except QuarantineReleaseRefused as exc:
            status["error"] = exc.message
        return status

    def release_quarantine(
        self, *, device: str, actor: str, reason: str,
        verify: Callable[[dict[str, Any]], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """在持有该设备 flock 时核对并归档隔离标记，写解除回执（BL-223）。

        持 flock 期间 ``try_lease`` 拿不到锁，所以核对与归档之间没有 Run 能取锁；
        ``verify`` 抛 ``QuarantineReleaseRefused`` 时标记保持原位。标记不删除：
        原子移入 ``archive/<device>/``，回执与之同目录。回执写失败时把标记移回原位。
        """
        if not _DEVICE_NAME.fullmatch(device or "") or device in {".", ".."}:
            raise QuarantineReleaseRefused("invalid_device", f"device={device!r}")
        if not str(actor or "").strip() or not str(reason or "").strip():
            raise QuarantineReleaseRefused("actor_and_reason_required", "actor and reason must be non-empty")
        marker_path = self._quarantine_path(device)
        if not marker_path.exists():
            raise QuarantineReleaseRefused("quarantine_marker_missing", f"no marker at {marker_path}")
        with self._lock_path(device).open("a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise QuarantineReleaseRefused(
                    "screen_lock_held", f"ScreenLock for {device!r} is held",
                    details={"state": self.status(device=device)},
                ) from None
            try:
                return self._release_quarantine_locked(
                    device=device, actor=actor.strip(), reason=reason.strip(),
                    marker_path=marker_path, verify=verify,
                )
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _release_quarantine_locked(self, *, device, actor, reason, marker_path, verify):
        try:
            raw = marker_path.read_bytes()
        except FileNotFoundError:
            raise QuarantineReleaseRefused("quarantine_marker_missing", f"no marker at {marker_path}") from None
        digest = hashlib.sha256(raw).hexdigest()
        marker = _parse_quarantine_marker(raw, device=device)
        checks = dict(verify(dict(marker)))
        if marker_path.read_bytes() != raw:
            raise QuarantineReleaseRefused("quarantine_marker_changed", "marker changed during checks")
        archive_dir = self.root / "archive" / device
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archived = archive_dir / f"{stamp}-quarantine.json"
        receipt_path = archive_dir / f"{stamp}-release.json"
        if archived.exists() or receipt_path.exists():
            raise QuarantineReleaseRefused("archive_collision", f"{archived} exists")
        os.replace(marker_path, archived)
        _fsync_dir(self.root)
        _fsync_dir(archive_dir)
        receipt = {
            "schema": QUARANTINE_RELEASE_SCHEMA,
            "device": device,
            "actor": actor,
            "reason": reason,
            "released_at": _now(),
            "releaser_pid": os.getpid(),
            "marker_sha256": digest,
            "marker": marker,
            "marker_archived_path": str(archived),
            "checks": checks,
        }
        try:
            write_json_atomic(receipt_path, receipt)
            with receipt_path.open("rb") as stream:
                os.fsync(stream.fileno())
            _fsync_dir(archive_dir)
        except Exception as exc:
            os.replace(archived, marker_path)
            _fsync_dir(self.root)
            raise QuarantineReleaseRefused(
                "receipt_persist_failed", f"{type(exc).__name__}: {exc}; marker restored",
            ) from exc
        return {**receipt, "receipt_path": str(receipt_path)}

    @contextmanager
    def acquire(
        self, *, device: str, run_id: str, blocking: bool = True
    ) -> Iterator[None]:
        lease: ScreenLease | None = None
        while lease is None:
            lease = self.try_lease(
                device=device, owner_kind="run", owner_id=run_id,
            )
            if lease is not None:
                break
            state = self.status(device=device)
            if not blocking or state.get("owner_kind") in HUMAN_OWNER_KINDS:
                owner = state.get("owner_id") or state.get("run_id") or "unknown"
                raise ScreenLockBusy(
                    f"device={device!r} 已被 {state.get('owner_kind') or 'workflow'} {owner!r} 占用"
                )
            # NB retry preserves run→run queueing while re-reading the owner on
            # every turn.  A human surface (web recording / web control) that
            # wins between turns is observed and rejected instead of silently
            # queueing behind it.
            time.sleep(0.05)
        after = None
        try:
            callback = _ADMISSION.get()
            if callback is not None:
                after = callback(device=device, run_id=run_id)
            yield
        finally:
            try:
                if after is not None:
                    after()
            except Exception as exc:
                from .stage_guard import StageQuiesceError
                if isinstance(exc, StageQuiesceError):
                    self.quarantine(device=device, owner_id=run_id, failures=exc.failures)
                raise
            finally:
                lease.release()

    def try_lease(
        self, *, device: str, owner_kind: str, owner_id: str,
    ) -> ScreenLease | None:
        if owner_kind not in OWNER_KINDS:
            raise ValueError(f"unsupported owner_kind: {owner_kind!r}")
        self._ensure_root()
        lock_path = self._lock_path(device)
        fh = lock_path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return None
        # Check while holding flock: a waiting owner must observe quarantine
        # written by the preceding owner before releasing its lease.
        if self._quarantine_path(device).exists():
            fh.close()
            raise ScreenQuarantineMarkerPresent(device, self._quarantine_path(device))
        self._write_state(device, LockState(
            device=device,
            locked=True,
            run_id=owner_id if owner_kind == "run" else None,
            acquired_at=_now(),
            pid=os.getpid(),
            owner_kind=owner_kind,
            owner_id=owner_id,
        ))
        return ScreenLease(self, device, owner_kind, owner_id, fh)

    def adopt_lease(
        self, fd: int, *, device: str, owner_kind: str, owner_id: str,
    ) -> ScreenLease:
        """子进程侧：接管父进程 spawn 前拿到、经 ``pass_fds`` 继承来的那把锁。

        为什么不是子进程自己 `try_lease`：那样父进程要在「已经把录制目录建好、
        进程也起了」之后才知道设备其实被占，只能事后收尾；而且父子之间还要多一次
        握手。父进程先拿锁 → 拿不到就 409、什么都不建；拿到了再 spawn，锁随 fd
        进子进程，全程没有窗口期。

        接管后重写状态文件，把 pid 换成**真正按着锁的那个进程**——不然状态里写的是
        server 的 pid，`live_status` 判活会永远说「还锁着」。
        """
        if owner_kind not in OWNER_KINDS:
            raise ValueError(f"unsupported owner_kind: {owner_kind!r}")
        fh = os.fdopen(fd, "a+")
        self._write_state(device, LockState(
            device=device,
            locked=True,
            run_id=None,
            acquired_at=_now(),
            pid=os.getpid(),
            owner_kind=owner_kind,
            owner_id=owner_id,
        ))
        return ScreenLease(self, device, owner_kind, owner_id, fh)

    def verify_held(self, *, device: str) -> bool:
        """这台设备的锁**此刻真的被按着**吗（不问是谁）。

        用第二个独立 fd 试 NB 锁：拿得到 = 没人按着。子进程接管后自检用——
        「以为自己持锁其实没有」比「拿不到锁」危险得多，后者至少会报错。
        """
        self._ensure_root()
        path = self._lock_path(device)
        with path.open("a+") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return False

    def _release_lease(self, lease: ScreenLease) -> None:
        if lease.device in self._quarantine_pending:
            _QUARANTINE_HOLDS[(str(self.root.resolve()), lease.device)] = lease
            raise ScreenQuarantined('quarantine receipt not persisted; ScreenLock retained')
        state = self.status(device=lease.device)
        if state.get("owner_kind") == lease.owner_kind and state.get("owner_id") == lease.owner_id:
            self._write_state(lease.device, LockState(device=lease.device, locked=False))
        try:
            fcntl.flock(lease.fh.fileno(), fcntl.LOCK_UN)
        finally:
            lease.fh.close()

    def status(self, *, device: str) -> dict:
        path = self._state_path(device)
        if not path.exists():
            # 锁根尚未建出来时走的也是这条：只读查询按「无锁」返回，不建目录、不抛错。
            return LockState(device=device, locked=False).__dict__
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return LockState(device=device, locked=False).__dict__

    def live_status(self, *, device: str) -> dict:
        """状态文件 + 持有者进程判活，dead pid 一律当没锁着（并就地自愈）。

        状态文件是持有者写的，进程被 SIGKILL / SIGTERM 打断时它来不及写回
        `locked:false`——OS 会释放 flock，文件却留着「还锁着」。桌面录制的持有者是
        子进程，被杀是常态，所以对外的判断一律走这里，别读裸 `status()`。
        """
        state = self.status(device=device)
        if not state.get("locked"):
            return state
        pid = state.get("pid")
        try:
            ipid = int(pid)
        except (TypeError, ValueError):
            ipid = 0
        if ipid > 0:
            try:
                os.kill(ipid, 0)
                return state
            except ProcessLookupError:
                pass
            except PermissionError:
                return state  # 不是自己能签名的进程，但它活着
        free = LockState(device=device, locked=False)
        self._write_state(device, free)
        return free.__dict__

    def read_human_holder(self, *, device: str) -> dict | None:
        """Read a human holder without repairing state or probing the device.

        Missing state means no holder. Unreadable or malformed state is unknown,
        so callers must refuse admission rather than treat it as an empty lock.
        """
        try:
            state = json.loads(self._state_path(device).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if (not isinstance(state, dict) or state.get("device") != device
                or type(state.get("locked")) is not bool):
            raise ValueError("invalid ScreenLock state")
        if not state["locked"]:
            return None
        if (state.get("owner_kind") not in OWNER_KINDS
                or not isinstance(state.get("owner_id"), str) or not state["owner_id"]
                or type(state.get("pid")) is not int or state["pid"] <= 0):
            raise ValueError("invalid ScreenLock holder")
        try:
            os.kill(state["pid"], 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass  # A live process owned by another user is still a holder.
        return state if state["owner_kind"] in HUMAN_OWNER_KINDS else None

    def human_holder(self, *, device: str) -> dict | None:
        """有没有人正实时占着这台设备（浏览器录制 / 纯远程操作 / 桌面录制）。

        提交 run、重跑、批量回放、开远程操作会话前的统一早退闸：**互斥本身由 lease
        保证**，这里只是为了在人还按着设备时早点给一句人话，而不是让 run 排进队列
        再在执行期炸 `ScreenLockBusy`。
        """
        state = self.live_status(device=device)
        if state.get("locked") and state.get("owner_kind") in HUMAN_OWNER_KINDS:
            return state
        return None

    def recording_holder(self, *, device: str) -> dict[str, Any] | None:
        path = self._recording_path(device)
        if not path.exists():
            return None
        try:
            holder = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None
        pid = holder.get("pid")
        try:
            ipid = int(pid)
        except (TypeError, ValueError):
            path.unlink(missing_ok=True)
            return None
        if ipid <= 0:
            path.unlink(missing_ok=True)
            return None
        try:
            os.kill(ipid, 0)
        except ProcessLookupError:
            path.unlink(missing_ok=True)
            return None
        except PermissionError:
            pass
        return holder

    def mark_recording(
        self, *, device: str, recording_id: str, pid: int, surface: str = "desktop",
    ) -> dict[str, Any]:
        holder = {
            "device": device,
            "recording_id": recording_id,
            "pid": int(pid),
            "started_at": _now(),
            "surface": surface,
        }
        self._ensure_root()
        path = self._recording_path(device)
        tmp = path.with_suffix(".recording.tmp")
        tmp.write_text(json.dumps(holder, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return holder

    def clear_recording(self, *, device: str, recording_id: str | None = None) -> bool:
        path = self._recording_path(device)
        if not path.exists():
            return False
        if recording_id is not None:
            try:
                holder = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                holder = {}
            if holder.get("recording_id") != recording_id:
                return False
        path.unlink(missing_ok=True)
        return True

    def _write_state(self, device: str, state: LockState) -> None:
        self._ensure_root()
        path = self._state_path(device)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
