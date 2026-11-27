"""Guest 锁屏状态辅助程序的部署、校验与读取（BL-232 阶段 2，承载方式 L1a）。

背景（评估文档 ``docs/platform-springboard-attach-alternatives.md`` §4.2、§5.2、§十一）：锁屏判据原先经
Frida attach SpringBoard 读 ``SBLockScreenManager.isUILocked``；attach/detach 后 SpringBoard 留有
Frida agent 残留映射，与安装后 SpringBoard 重启相关（§9.76、§9.77，相关关系）。阶段 2 改为读
Darwin 通知 ``com.apple.springboard.lockstate`` 与 ``com.apple.springboard.hasBlankedScreen`` 的
状态值，由 autophone 自带的 guest 辅助程序读取，不注入 SpringBoard、不替换 vphoned。

部署（每台宿主、每个 guest 各自完成，不经 ``start_vphone.sh``，也不经 vphoned 推送流程）：

1. 宿主二进制：由外部提供的 `lock_state` 辅助目录（`AUTOPHONE_LOCK_STATE_HELPER_DIR` 或默认
   `support/lock-state/`）包含 `lock_state`、`lock_state.c` 与 `lock_state.build.json`。读取前核对：
   二进制存在；其 SHA-256 等于构建记录；构建记录中的源码 SHA-256 等于当前 `lock_state.c`。任一不符即判不可用。
2. guest 路径：``/var/jb/tmp/autophone-lockstate-<二进制 SHA-256 前 16 位>``，与 ``ax_dump-v6.dylib``
   同目录；文件名随二进制变化，新旧版本不会互相覆盖。
3. 每个宿主进程对每个 socket 首次读取前，经 guest shell ``sha256sum`` 核对该路径：不存在或哈希
   不符时经 UDS ``file_put``（权限 755）推送，推送后再次核对，仍不符即判不可用。核对通过后在
   进程内缓存，后续读取不再核对；执行时报「文件不存在 / 不可执行」则清除缓存重新部署一次。
4. 每次读取核对输出中的协议名与源码 SHA-256，与本机构建记录不符即判不可用。

本模块只读写 ``/var/jb/tmp/autophone-lockstate-*``，不删除其他文件；旧版本文件不自动删除。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from autophone.logging import get_logger

logger = get_logger(__name__)

PROTOCOL = "autophone-lockstate/1"
BUILD_SCHEMA = "autophone.guest-helper-build/v1"
GUEST_DIR = "/var/jb/tmp"
GUEST_PREFIX = "autophone-lockstate-"
NOTIFY_LOCKSTATE = "com.apple.springboard.lockstate"
NOTIFY_BLANKED = "com.apple.springboard.hasBlankedScreen"

#: guest 侧执行上限（毫秒）。辅助程序只做两次 notify 读与两次进程表读取。
RUN_TIMEOUT_MS = 3_000
#: ``sha256sum`` 与 ``file_put`` 的上限。
DEPLOY_SHELL_TIMEOUT_MS = 10_000
DEPLOY_SOCKET_TIMEOUT_S = 30.0
_JB_PATH = "export PATH=/var/jb/usr/bin:/var/jb/bin:$PATH"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

RequestFn = Callable[..., Any]


class HelperUnavailable(RuntimeError):
    """辅助程序不可用或读数不可判定；``code`` 为稳定的小写阶段名。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _code_root() -> Path:
    from autophone.paths import CODE_ROOT

    return Path(CODE_ROOT)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class LocalHelper:
    """宿主侧已校验的辅助程序构建。"""

    binary: Path
    binary_sha256: str
    source_sha256: str

    @property
    def guest_path(self) -> str:
        return f"{GUEST_DIR}/{GUEST_PREFIX}{self.binary_sha256[:16]}"

    def describe(self) -> dict[str, Any]:
        return {
            "binary": str(self.binary),
            "binary_sha256": self.binary_sha256,
            "source_sha256": self.source_sha256,
            "guest_path": self.guest_path,
        }


def local_helper(code_root: Path | None = None) -> LocalHelper:
    """核对宿主二进制、构建记录与当前源码三者一致，返回可部署的构建。"""
    import os

    override_dir = os.environ.get("AUTOPHONE_LOCK_STATE_HELPER_DIR", "").strip()
    if override_dir and code_root is None:
        guest = Path(override_dir)
    else:
        root = Path(code_root) if code_root is not None else _code_root()
        guest = root / "support" / "lock-state"
    source, binary, record_path = guest / "lock_state.c", guest / "lock_state", guest / "lock_state.build.json"
    if not binary.is_file() or not record_path.is_file():
        raise HelperUnavailable(
            "helper_binary_missing",
            f"{binary} or {record_path.name} missing; provide a built lock_state helper directory",
        )
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HelperUnavailable("helper_build_record_invalid", f"{record_path}: {exc}") from exc
    if (
        not isinstance(record, dict)
        or record.get("schema") != BUILD_SCHEMA
        or record.get("protocol") != PROTOCOL
        or not _SHA256.match(str(record.get("binary_sha256") or ""))
        or not _SHA256.match(str(record.get("source_sha256") or ""))
    ):
        raise HelperUnavailable("helper_build_record_invalid", f"{record_path} does not describe {PROTOCOL}")
    binary_sha = _sha256(binary)
    if binary_sha != record["binary_sha256"]:
        raise HelperUnavailable(
            "helper_build_stale", f"{binary} sha256 {binary_sha} differs from its build record")
    if not source.is_file():
        raise HelperUnavailable("helper_source_missing", f"{source} missing")
    source_sha = _sha256(source)
    if source_sha != record["source_sha256"]:
        raise HelperUnavailable(
            "helper_build_stale",
            f"{binary.name} was built from source {record['source_sha256'][:12]}, "
            f"current {source.name} is {source_sha[:12]}; rebuild lock_state helper",
        )
    return LocalHelper(binary=binary, binary_sha256=binary_sha, source_sha256=source_sha)


# (socket realpath, binary sha256) -> guest path，已核对哈希的部署。
_VERIFIED: dict[tuple[str, str], str] = {}
_VERIFIED_LOCK = threading.Lock()


def forget_deployments() -> None:
    """清空进程内的部署缓存（测试与重新部署用）。"""
    with _VERIFIED_LOCK:
        _VERIFIED.clear()


def _response(result: Any) -> dict[str, Any]:
    response = result[0] if isinstance(result, tuple) and result else result
    if not isinstance(response, dict):
        raise HelperUnavailable("uds_protocol_error", f"unexpected UDS response {type(response).__name__}")
    return response


class GuestLockStateHelper:
    """一个 guest（一个 UDS socket）上的锁屏状态辅助程序。"""

    def __init__(self, sock_path: str, *, request_fn: RequestFn | None = None,
                 local: LocalHelper | None = None, timeout_s: float = 5.0) -> None:
        if request_fn is None:
            from autophone.device.uds import request as request_fn
        self.sock_path = str(Path(sock_path).resolve())
        self._request = request_fn
        self._local = local
        self.timeout_s = float(timeout_s)

    @property
    def local(self) -> LocalHelper:
        if self._local is None:
            self._local = local_helper()
        return self._local

    # -------------------------------------------------------------- guest 命令
    def _shell(self, command: str, *, timeout_ms: int, socket_timeout_s: float) -> dict[str, Any]:
        try:
            result = self._request(
                {"t": "shell", "cmd": f"{_JB_PATH}; {command}", "screen": False, "timeout_ms": timeout_ms},
                sock_path=self.sock_path, timeout=socket_timeout_s, parse_image=False,
            )
        except HelperUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failure is "unknown", never "unlocked"
            raise HelperUnavailable("uds_failed", f"shell request failed: {type(exc).__name__}: {exc}") from exc
        response = _response(result)
        if response.get("ok") is not True:
            raise HelperUnavailable("uds_failed", f"shell request rejected: {response.get('error')!r}")
        if response.get("timed_out"):
            raise HelperUnavailable("helper_timeout", f"guest killed {command[:80]!r} after {timeout_ms} ms")
        return response

    def _guest_sha256(self, path: str) -> str | None:
        response = self._shell(
            f"if [ -f '{path}' ]; then sha256sum -- '{path}'; else echo ABSENT; fi",
            timeout_ms=DEPLOY_SHELL_TIMEOUT_MS, socket_timeout_s=DEPLOY_SOCKET_TIMEOUT_S,
        )
        out = str(response.get("stdout") or "").strip()
        if int(response.get("code", -1)) != 0:
            raise HelperUnavailable("helper_deploy_failed", f"sha256sum exited {response.get('code')}: {out[:200]!r}")
        if out == "ABSENT":
            return None
        digest = out.split()[0] if out else ""
        if not _SHA256.match(digest):
            raise HelperUnavailable("helper_deploy_failed", f"unexpected sha256sum output {out[:200]!r}")
        return digest

    def _push(self, path: str) -> None:
        try:
            result = self._request(
                {"t": "file_put", "path": path, "load": str(self.local.binary), "perm": "755", "screen": False},
                sock_path=self.sock_path, timeout=DEPLOY_SOCKET_TIMEOUT_S, parse_image=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise HelperUnavailable("helper_deploy_failed", f"file_put failed: {type(exc).__name__}: {exc}") from exc
        response = _response(result)
        if response.get("ok") is not True:
            raise HelperUnavailable("helper_deploy_failed", f"file_put rejected: {response.get('error')!r}")

    # -------------------------------------------------------------- 部署
    def ensure_deployed(self) -> dict[str, Any]:
        """确保 guest 上存在与本机构建哈希相同的辅助程序，返回部署记录。"""
        local = self.local
        key = (self.sock_path, local.binary_sha256)
        with _VERIFIED_LOCK:
            if key in _VERIFIED:
                return {"action": "cached", **local.describe()}
        path = local.guest_path
        found = self._guest_sha256(path)
        action = "verified"
        if found != local.binary_sha256:
            if found is not None:
                logger.warning("lock-state helper on guest differs from the local build, re-pushing: "
                               "sock=%s path=%s guest_sha256=%s", self.sock_path, path, found)
            self._push(path)
            found = self._guest_sha256(path)
            if found != local.binary_sha256:
                raise HelperUnavailable(
                    "helper_guest_hash_mismatch",
                    f"{path} sha256 {found} after push, expected {local.binary_sha256}",
                )
            action = "pushed"
            logger.info("lock-state helper deployed: sock=%s path=%s sha256=%s",
                        self.sock_path, path, local.binary_sha256)
        with _VERIFIED_LOCK:
            _VERIFIED[key] = path
        return {"action": action, **local.describe()}

    def _forget(self) -> None:
        with _VERIFIED_LOCK:
            _VERIFIED.pop((self.sock_path, self.local.binary_sha256), None)

    # -------------------------------------------------------------- 读取
    def read(self) -> dict[str, Any]:
        """执行辅助程序一次，返回已校验协议与源码身份的原始读数。

        返回 ``{"output": <辅助程序 JSON>, "deploy": <部署记录>, "elapsed_ms": int}``；不可用时抛
        ``HelperUnavailable``。本方法不判读锁屏状态，判读见
        ``autophone.observation.vphone_native_state.judge_lock_state``。
        """
        started = time.monotonic()
        deploy = self.ensure_deployed()
        response = self._run()
        if int(response.get("code", -1)) in (126, 127):
            # 文件被 guest 清理（如 /var/jb/tmp 被清空）或失去执行权限：清缓存重部署一次。
            logger.warning("lock-state helper not executable on guest (exit %s), redeploying: sock=%s",
                           response.get("code"), self.sock_path)
            self._forget()
            deploy = self.ensure_deployed()
            response = self._run()
        output = self._parse(response)
        return {"output": output, "deploy": deploy, "elapsed_ms": round((time.monotonic() - started) * 1000)}

    def _run(self) -> dict[str, Any]:
        timeout_ms = max(1, min(RUN_TIMEOUT_MS, int(self.timeout_s * 1000)))
        return self._shell(f"'{self.local.guest_path}'", timeout_ms=timeout_ms,
                           socket_timeout_s=max(self.timeout_s, timeout_ms / 1000) + 3.0)

    def _parse(self, response: dict[str, Any]) -> dict[str, Any]:
        code = int(response.get("code", -1))
        stdout = str(response.get("stdout") or "").strip()
        if code not in (0, 2):
            raise HelperUnavailable(
                "helper_run_failed",
                f"exit {code}: {stdout[-200:]!r} {str(response.get('stderr') or '')[-200:]!r}",
            )
        try:
            output = json.loads(stdout.splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise HelperUnavailable("helper_output_invalid", f"not JSON: {stdout[-200:]!r}") from exc
        if not isinstance(output, dict) or output.get("protocol") != PROTOCOL:
            raise HelperUnavailable("helper_protocol_mismatch", f"protocol {output.get('protocol') if isinstance(output, dict) else None!r}")
        if output.get("source_sha256") != self.local.source_sha256:
            raise HelperUnavailable(
                "helper_protocol_mismatch",
                f"guest helper source {str(output.get('source_sha256'))[:12]} differs from local build "
                f"{self.local.source_sha256[:12]}",
            )
        return output


__all__ = [
    "GUEST_DIR",
    "GUEST_PREFIX",
    "GuestLockStateHelper",
    "HelperUnavailable",
    "LocalHelper",
    "NOTIFY_BLANKED",
    "NOTIFY_LOCKSTATE",
    "PROTOCOL",
    "forget_deployments",
    "local_helper",
]
