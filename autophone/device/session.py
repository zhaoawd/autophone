"""Target-bound DeviceSession hiding UDS, shell, SSH and Frida endpoints."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import plistlib
import re
import tempfile
import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
from functools import partial, wraps
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


from .ssh_auth import guest_ssh_password, run_password_command
from .transfer import SCP_AUTH_EXIT_CODES, SSH_AUTH_EXIT_CODES, GuestTransfer, run_with_auth_retry
from .stage_guard import OperationClass, StageGuard, guarded_device_operation, uds_operation

logger = logging.getLogger(__name__)


class DeviceProtocolError(RuntimeError):
    pass


class DeviceEndpoint(Protocol):
    """设备层用到的 Target 字段，结构化声明而不 import `autophone.rig.target`。

    `autophone.rig.target.DeviceTarget` 是本 Protocol 的实现；这里只列设备层实际
    读到的字段，让 `device` 不再反向依赖 `rig`（BL-003 判据 A）。
    """

    host_id: str
    rig_id: str
    rig_generation: int
    socket: Path
    ssh_host: str
    ssh_port: int

    @property
    def frida_endpoint(self) -> str: ...


class WriteFence(Protocol):
    """写栅栏：动作执行前校验本次 Assignment 仍持有设备写权。

    `autophone.rig.agent.DeviceWriteFence` 是本 Protocol 的实现。设备层只需要
    「校验」与「带校验执行」两个动作，不需要 Assignment / 传输层的字段。
    """

    def check(self) -> None: ...

    def run(self, action: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any: ...


# `install_build` 的注入点：设备层提供 transfer/shell/installer 三个原语，
# 由上层（`autophone.execution.handlers` 这个装配根）把包仓库的安装实现传进来。
# 签名：`fn(build_ref: Mapping, **device_options) -> Install Receipt`。build_ref 是
# Build Catalog 已解析好的完整 Build Ref（app/slot/build_id/artifact_sha256…）；
# Device Control 只执行这一个精确动作，不做 slot → Build 的解析（目标架构 §5.4）。
InstallPackageFn = Callable[..., Mapping[str, Any]]
# 读包仓库中某 Build 的 IPA 内构建标记字节；IPA 不含标记时返回 None（BL-237）。
BuildMarkerFn = Callable[..., "bytes | None"]


@dataclass(frozen=True)
class InstalledBuild:
    """设备回读到的已安装 App 身份；build_id / artifact_sha256 只有 Provisioning 把回读
    与已执行的精确安装动作绑定后才会填上（设备本身报不出 Build Record）。"""

    app: str
    bundle_id: str
    short_version: str | None
    build_version: str | None
    slot: str | None = None
    sha256: str | None = None
    build_id: str | None = None
    # BL-241 第一步：设备 Info.plist 字节的 sha256，只作观测记录。不参与相等比较，
    # 也不进入 to_dict()，Provisioning 观测与 world identity 的 device_observation 保持原样。
    info_plist_sha256: str | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("info_plist_sha256", None)
        return data


class DeviceSession(Protocol):
    target: DeviceEndpoint
    write_fence: WriteFence

    def screenshot(self) -> bytes: ...
    def tap(self, x: int, y: int) -> None: ...
    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, ms: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def shell(self, command: str, *, write: bool = False, timeout_ms: int = 30_000) -> Any: ...
    def app_list(self, *, timeout_s: float = 15.0) -> list[dict[str, Any]]: ...
    def launch_app(self, bundle_id: str) -> None: ...
    def terminate_app(self, bundle_id: str) -> None: ...
    def install_build(self, build_ref: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def stored_build_marker(self, build_ref: Mapping[str, Any]) -> bytes | None: ...
    def put_file(self, local: Path, remote: str, *, permission: str = "644") -> None: ...
    def get_file(self, remote: str, local: Path) -> Path: ...
    def read_installed_build(self, app: str, bundle_id: str) -> InstalledBuild | None: ...

    def read_build_marker(self, bundle_id: str) -> bytes: ...
    def probe_uds(self) -> dict[str, Any]: ...
    def runtime_info(self, *, timeout_s: float = 3.0) -> dict[str, Any]: ...
    def frida_probe(self, app_name: str) -> dict[str, Any]: ...
    def set_location(
        self,
        latitude: float,
        longitude: float,
        *,
        speed: float,
        course: float,
        altitude: float = 0.0,
        horizontal_accuracy: float = 5.0,
        vertical_accuracy: float = 5.0,
    ) -> None: ...
    def close(self) -> Mapping[str, Any]: ...


RequestFn = Callable[..., tuple[dict[str, Any], Any]]
ShellFn = Callable[..., Any]
TransferFn = Callable[[Path, str, DeviceEndpoint], None]


def _default_request(message: Mapping[str, Any], **kwargs: Any) -> tuple[dict[str, Any], Any]:
    from autophone.device.uds import request

    return request(dict(message), **kwargs)


UDS_UNREACHABLE = "unreachable"
UDS_HOST_ONLY = "host_only"
UDS_GUEST_READY = "guest_ready"


@guarded_device_operation(OperationClass.READ, identity=lambda a: {'socket_path': a['sock_path']})
def probe_uds_state(
    sock_path: str | Path,
    *,
    request_fn: RequestFn = _default_request,
    timeout: float = 3.0,
    protocol: str | None = None,
) -> dict[str, Any]:
    """Classify one UDS endpoint into unreachable / host_only / guest_ready.

    This is the single UDS reachability probe for the whole repo; both the Rig
    path (``DeviceSession.probe_uds`` → ``RigMaintenance`` → Assignment
    preflight) and the compatibility path (``RunService._probe_vm_uds`` and
    ``_check_device_online``) call it, so "is this device usable" cannot get two
    different answers at the same instant again.

    Three states, because "the socket answered" and "the device works" are not
    the same claim:

    ``unreachable``
        The transport itself failed — socket missing, connection refused,
        timeout, unparsable reply. Nothing is serving.
    ``host_only``
        vphone-cli's host UDS face answered with a well-formed protocol-level
        rejection (``ok=false``), but the guest did not complete the round
        trip — typically ``guest not connected`` while vphoned/vsock is down,
        or ``unknown command`` on a build that lacks the probe verb. The host
        process is alive; the device is not usable.
    ``guest_ready``
        ``app_list`` came back ``ok=true`` with an ``apps`` list, which is only
        produced after the request crosses vsock into vphoned. This is the only
        state that sets ``available=True``.

    In upstream_v1 mode, capabilities first proves host reachability and reports
    guest connectivity. Failure of the subsequent apps probe remains host_only.

    Legacy command choice: ``app_list`` is the read-only verb the guest actually
    implements (there is no ``ping`` on this UDS face — the host answers it
    ``ok=false unknown command`` without ever touching the guest, so a ``ping``
    round trip cannot separate ``host_only`` from ``guest_ready``). It takes no
    write fence and does not perturb the screen or the clipboard.
    ``tools/phase1/vsock_health.probe_vsock`` uses it for exactly this
    reason; this function is the same judgement in library form.

    A protocol-level ``ok=false`` is classified, never raised: unlike
    ``DeviceSession._request`` (fail-closed for real device actions), a probe
    whose whole job is to report unavailability must be able to report it.
    """
    socket = str(sock_path)
    from .target import resolve_protocol
    mode = resolve_protocol(socket, protocol)
    host_answered = False
    send = partial(request_fn, protocol=mode) if mode != "legacy" and request_fn is _default_request else request_fn
    try:
        if mode == "upstream_v1":
            capabilities, _ = send({"t": "capabilities", "screen": False}, sock_path=socket,
                                   timeout=timeout, parse_image=False)
            host_answered = True
            if (capabilities.get("ok") is not True or capabilities.get("protocol") != "upstream_v1"
                    or capabilities.get("guest_connected") is not True):
                return {"state": UDS_HOST_ONLY, "available": False,
                        "evidence": f"uds={socket} host answered; upstream guest/protocol not ready"}
        response, _ = send(
            {"t": "app_list", "filter": "running", "screen": False},
            sock_path=socket,
            timeout=timeout,
            parse_image=False,
        )
    except Exception as exc:  # noqa: BLE001 - transport failure is a probe verdict
        return {
            "state": UDS_HOST_ONLY if host_answered else UDS_UNREACHABLE,
            "available": False,
            "evidence": f"uds={socket} guest/transport unavailable: {type(exc).__name__}: {exc}",
        }
    apps = response.get("apps")
    if response.get("ok") is True and isinstance(apps, list):
        return {
            "state": UDS_GUEST_READY,
            "available": True,
            "evidence": f"uds={socket} app_list ok, {len(apps)} running",
        }
    detail = response.get("error") or response.get("detail") or "ok=false without apps"
    return {
        "state": UDS_HOST_ONLY,
        "available": False,
        "evidence": f"uds={socket} host answered, guest not ready: {detail}",
    }


def _default_shell(command: str, **kwargs: Any) -> Any:
    from autophone.device.shell import run_shell

    return run_shell(command, **kwargs)


# guest SSH 端点的固定账户与传输参数。`GUEST_STREAM_CAT` 用绝对路径：guest 的
# SSH 登录 PATH 是 `/usr/bin:/bin`，不含越狱前缀，`cat` 不可按名解析。该路径没有
# 自动探测，镜像布局不同时用 `AUTOPHONE_GUEST_STREAM_CAT` 覆写（在
# `.claude/skills/vm-bringup/references/device-interface.md` 登记）。
GUEST_SSH_USER = "mobile"
ENV_GUEST_STREAM_CAT = "AUTOPHONE_GUEST_STREAM_CAT"
GUEST_STREAM_CAT = (
    str(os.environ.get(ENV_GUEST_STREAM_CAT) or "").strip() or "/iosbinpack64/bin/cat"
)
# 与 `store.scp_to_guest` 统一到 600 s：同一 iproxy 隧道、同一 IPA 体量，直连路径一直用
# 600 s，设备层没有更短的依据；短超时只会在慢链路上把可完成的传输判失败。
TRANSFER_TIMEOUT_S = 600.0
# 只用密码认证：否则 ssh 先递 ssh-agent / 默认私钥，guest 在密码输入前
# 就到 MaxAuthTries（"Too many authentication failures"）。与 store.scp_to_guest 一致。
_SSH_OPTIONS: tuple[str, ...] = (
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "PreferredAuthentications=password",
    "-o", "PubkeyAuthentication=no",
    "-o", "IdentitiesOnly=yes",
    "-o", "NumberOfPasswordPrompts=1",
)
# guest 侧执行不到传输助手时的 stderr 特征。只有这些特征**和** `GUEST_STREAM_CAT`
# 路径同时出现才判定助手缺失：`bash: <remote>: No such file or directory`（目标目录
# 不存在）用的是同一句式，按特征词单独判会把它误判成助手缺失并白跑两次 scp。
_REMOTE_HELPER_MISSING = ("command not found", "no such file or directory")
# 本机 scp 不认 `-O` 时的 stderr 特征。
_SCP_FLAG_REJECTED = re.compile(
    r"^(?:scp: )?(?:unknown|illegal|invalid) option (?:-- )?['\"]?-?O['\"]?[ \t]*\r?$",
    re.MULTILINE,
)
# 错误文案里保留的 stderr 长度；判定一律按全文匹配，截断只发生在文案。
_STDERR_EXCERPT = 300


def _stderr_matches(stderr: str | None, markers: tuple[str, ...]) -> bool:
    text = (stderr or "").lower()
    return any(marker in text for marker in markers)


def _stream_helper_missing(stderr: str | None) -> bool:
    """stderr 是否表示 guest 上执行不到 `GUEST_STREAM_CAT` 本身。"""
    text = (stderr or "").lower()
    helper = GUEST_STREAM_CAT.lower()
    if helper in text and _stderr_matches(text, _REMOTE_HELPER_MISSING):
        return True
    return any(line.endswith(f": {helper}: not found") for line in text.splitlines())


def _excerpt(stderr: str | None) -> str:
    return (stderr or "").strip()[:_STDERR_EXCERPT]


def _ssh_command(command: str, target: DeviceEndpoint) -> list[str]:
    return [
        "ssh", "-T", "-p", str(target.ssh_port),
        *_SSH_OPTIONS,
        f"{GUEST_SSH_USER}@{target.ssh_host}",
        command,
    ]


def _scp_command(
    local: Path, remote: str, target: DeviceEndpoint, *, legacy_protocol: bool,
) -> list[str]:
    command = ["scp"]
    if legacy_protocol:
        command.append("-O")
    command += [
        "-P", str(target.ssh_port),
        *_SSH_OPTIONS,
        str(local),
        # scp 的远端路径由 guest 的登录 shell 解释，和流式分支一样要转义。
        f"{GUEST_SSH_USER}@{target.ssh_host}:{shlex.quote(remote)}",
    ]
    return command


def _run_transfer(
    command: list[str], *, password: str, source: Path | None = None, what: str = "package transfer",
) -> subprocess.CompletedProcess:
    """执行一次传输相关的 SSH/scp 调用；guest 认证被拒时按 `run_with_auth_retry` 有界重试。

    `source` 给定时作为 stdin，每次尝试重新打开，重试从文件开头发送。scp 命令经 ssh 传输
    认证失败时以 1 退出，按 `SCP_AUTH_EXIT_CODES` 判定。
    """
    exit_codes = SCP_AUTH_EXIT_CODES if "scp" in command else SSH_AUTH_EXIT_CODES

    def attempt() -> subprocess.CompletedProcess:
        if source is None:
            return run_password_command(
                command, password=password, capture_output=True, text=True,
                timeout=TRANSFER_TIMEOUT_S,
            )
        with source.open("rb") as payload:
            return run_password_command(
                command, password=password, stdin=payload, capture_output=True, text=True,
                timeout=TRANSFER_TIMEOUT_S,
            )

    return run_with_auth_retry(attempt, what=what, exit_codes=exit_codes)


@guarded_device_operation(OperationClass.TRANSFER, identity=lambda a: {
    "rig_id": a["target"].rig_id, "generation": a["target"].rig_generation,
    "socket_path": a["target"].socket,
    "ssh_endpoint": (a["target"].ssh_host, a["target"].ssh_port),
})
def _ssh_stream_transfer(local: Path, remote: str, target: DeviceEndpoint) -> None:
    """把宿主文件写到 guest 的 `remote` 路径，失败 fail-closed。

    首选通道通过 SSH 写入同目录临时文件，校验字节数后原子替换目标，与
    `autophone.packages.store.scp_to_guest` 同一手段（设备层不反向 import 包仓库，
    所以两条实现复用设备层的命令构造器）。两条实现的异常契约已统一（BL-129）：本函数抛
    `DeviceProtocolError`，`scp_to_guest` 抛的 `TransferError` 是
    `DeviceProtocolError` 的子类，调用方按 `DeviceProtocolError` 捕获即覆盖两条实现。
    不用 scp 作首选的依据（2026-09-13 在 vm 实测）：

    - 宿主 OpenSSH 10.2 的 scp 默认走 SFTP，客户端在 guest 上执行
      `/usr/libexec/sftp-server`，而 guest 只有 `/var/jb/usr/libexec/sftp-server`，
      报 `bash: /usr/libexec/sftp-server: No such file or directory`；
    - `scp -O` 的 legacy SCP 协议要求 guest 登录 PATH 里有 `scp`，实测该 PATH
      是 `/usr/bin:/bin`，scp 只在 `/var/jb/usr/bin`，报 `bash: scp: command not found`。

    `scp -O` 保留为回退，只在 stderr 同时出现 `GUEST_STREAM_CAT` 路径与「找不到」
    时使用（回退写 WARNING）；本机 scp 不认 `-O` 的旧 OpenSSH 再回退到不带该标志的
    原命令。目标目录不存在这类失败不回退，直接抛错。两条通道都失败时错误文案同时
    带流式与 scp 两段 stderr。

    每次 SSH/scp 调用在 guest 认证被拒（ssh 退出码 255、scp 退出码 1 或 255，且 stderr 为
    `Permission denied (...)`）时按 `autophone.device.transfer.run_with_auth_retry`
    间隔重试，其他失败不重试。
    """
    source = Path(local)
    if not source.is_file():
        raise DeviceProtocolError(f"package transfer failed: local file missing: {source}")

    try:
        password = guest_ssh_password()
    except ValueError as exc:
        raise DeviceProtocolError(str(exc)) from exc
    transfer = GuestTransfer(remote, source.stat().st_size)
    try:
        completed = _run_transfer(
            _ssh_command(transfer.stream_command(GUEST_STREAM_CAT), target), password=password, source=source,
            what=f"package transfer to {target.ssh_host}:{target.ssh_port}",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _cleanup_transfer(transfer, target, password)
        detail = "SSH transfer timed out" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
        raise DeviceProtocolError(f"package transfer failed: {detail}") from exc
    if completed.returncode == 0:
        return
    # 判定按 stderr 全文，截断只用于错误文案：助手缺失那一行可能排在很长的输出之后。
    stream_stderr = completed.stderr or ""
    if not _stream_helper_missing(stream_stderr):
        raise DeviceProtocolError(f"package transfer failed: {_excerpt(stream_stderr)}")

    logger.warning(
        "guest stream transfer could not run %s on %s:%s (%s); "
        "falling back to scp -O (legacy SCP protocol)",
        GUEST_STREAM_CAT, target.ssh_host, target.ssh_port, _excerpt(stream_stderr),
    )
    try:
        completed = _run_transfer(_scp_command(source, transfer.staging, target, legacy_protocol=True), password=password)
        if completed.returncode != 0 and _SCP_FLAG_REJECTED.search(completed.stderr or ""):
            logger.warning(
                "local scp does not accept -O (%s); retrying without the legacy protocol flag",
                _excerpt(completed.stderr),
            )
            completed = _run_transfer(_scp_command(source, transfer.staging, target, legacy_protocol=False), password=password)
        if completed.returncode != 0:
            raise DeviceProtocolError(
                "package transfer failed: "
                f"stream via {GUEST_STREAM_CAT} ({ENV_GUEST_STREAM_CAT} overrides it): "
                f"{_excerpt(stream_stderr)}; scp fallback: {_excerpt(completed.stderr)}"
            )
        completed = _run_transfer(_ssh_command(transfer.publish_command(), target), password=password)
        if completed.returncode != 0:
            raise DeviceProtocolError(f"package transfer publish failed: {_excerpt(completed.stderr)}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _cleanup_transfer(transfer, target, password)
        detail = "SSH transfer timed out" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
        raise DeviceProtocolError(f"package transfer failed: {detail}") from exc
    except DeviceProtocolError:
        _cleanup_transfer(transfer, target, password)
        raise


def _cleanup_transfer(transfer: GuestTransfer, target: DeviceEndpoint, password: str) -> None:
    try:
        result = run_password_command(
            _ssh_command(transfer.cleanup_command(), target), password=password,
            capture_output=True, text=True,
            timeout=min(TRANSFER_TIMEOUT_S, 15.0),
        )
        if result.returncode != 0:
            logger.warning("guest transfer cleanup failed: %s", _excerpt(result.stderr))
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("guest transfer cleanup unavailable: %s", type(exc).__name__)


def _stage_operation(operation: OperationClass):
    def decorate(function):
        @wraps(function)
        def call(self, *args, **kwargs):
            with self._operation(operation):
                return function(self, *args, **kwargs)
        return call
    return decorate


class VPhoneDeviceSession:
    """Use one explicit device target for every device protocol call."""

    def __init__(
        self,
        target: DeviceEndpoint,
        write_fence: WriteFence,
        *,
        request_fn: RequestFn = _default_request,
        shell_fn: ShellFn = _default_shell,
        transfer_fn: TransferFn = _ssh_stream_transfer,
        install_package_fn: InstallPackageFn | None = None,
        build_marker_fn: BuildMarkerFn | None = None,
        package_root: Path | None = None,
        code_root: Path | None = None,
    ) -> None:
        self.target = target
        self.stage_guard = StageGuard.current()
        self._closed = False
        self.write_fence = write_fence
        mode = getattr(target, "protocol", "legacy")
        self._request_fn = partial(request_fn, protocol=mode) if mode != "legacy" and request_fn is _default_request else request_fn
        self._shell_fn = partial(shell_fn, protocol=mode) if mode != "legacy" and shell_fn is _default_shell else shell_fn
        if mode == "upstream_v1" and transfer_fn is _ssh_stream_transfer:
            from .transfer import upstream_transfer
            self._transfer_fn = lambda local, remote, selected: upstream_transfer(
                local, remote, sock_path=str(selected.socket),
            )
        else:
            self._transfer_fn = transfer_fn
        self._install_package_fn = install_package_fn
        self._build_marker_fn = build_marker_fn
        self.package_root = package_root
        self.code_root = code_root

    @contextmanager
    def _operation(self, operation: OperationClass):
        if self._closed:
            raise DeviceProtocolError("device_session_closed")
        guard = self.stage_guard or StageGuard.for_device_call()
        if guard is None:
            yield
        else:
            with guard.operation(
                operation, rig_id=self.target.rig_id, generation=self.target.rig_generation,
                socket_path=self.target.socket, frida_endpoint=self.target.frida_endpoint,
                ssh_endpoint=(self.target.ssh_host, self.target.ssh_port),
            ):
                yield

    def _request(
        self,
        message: Mapping[str, Any],
        *,
        write: bool,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        with self._operation(uds_operation(str(message.get("t") or ""))):
            if write and self.stage_guard is None:
                self.write_fence.check()
            response, _ = self._request_fn(
                dict(message),
                sock_path=str(self.target.socket),
                timeout=timeout,
                parse_image=message.get("t") == "screenshot",
            )
            if response.get("ok") is not True:
                raise DeviceProtocolError(
                    f"{message.get('t')} failed: {response.get('error') or response.get('detail')}"
                )
            return response

    def screenshot(self) -> bytes:
        response = self._request({"t": "screenshot", "color": True}, write=False)
        image = response.get("image")
        if not image:
            raise DeviceProtocolError("screenshot response has no image")
        return base64.b64decode(image)

    def tap(self, x: int, y: int) -> None:
        self._request({"t": "tap", "x": x, "y": y}, write=True)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, ms: int) -> None:
        self._request(
            {"t": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms},
            write=True,
        )

    def type_text(self, text: str) -> None:
        self._request({"t": "type", "text": text, "screen": False}, write=True)

    @_stage_operation(OperationClass.SHELL)
    def shell(self, command: str, *, write: bool = False, timeout_ms: int = 30_000) -> Any:
        if write and self.stage_guard is None:
            self.write_fence.check()
        return self._shell_fn(
            command,
            timeout_ms=timeout_ms,
            sock_path=str(self.target.socket),
        )

    def app_list(self, *, timeout_s: float = 15.0) -> list[dict[str, Any]]:
        response = self._request({"t": "app_list", "screen": False}, write=False, timeout=timeout_s)
        if not isinstance(response.get('apps'), list) or not all(isinstance(x, dict) for x in response['apps']):
            raise DeviceProtocolError('app_list response has invalid apps')
        return [dict(item) for item in response.get("apps") or [] if isinstance(item, dict)]

    @_stage_operation(OperationClass.READ)
    def runtime_info(self, *, timeout_s: float = 3.0) -> dict[str, Any]:
        from .runtime_observation import observe_runtime

        return observe_runtime(self, timeout_s=timeout_s)

    def launch_app(self, bundle_id: str) -> None:
        self._request({"t": "app_launch", "bundle_id": bundle_id, "screen": False}, write=True)

    def terminate_app(self, bundle_id: str) -> None:
        self._request({"t": "app_terminate", "bundle_id": bundle_id, "screen": False}, write=True)

    @_stage_operation(OperationClass.READ)
    def probe_uds(self) -> dict[str, Any]:
        """Report this target's UDS as unreachable / host_only / guest_ready.

        See ``probe_uds_state`` for the state semantics. Only ``guest_ready``
        yields ``available=True``, which is what Assignment preflight gates on.
        """
        return probe_uds_state(self.target.socket, request_fn=self._request_fn,
                               protocol=getattr(self.target, "protocol", "legacy"))

    @_stage_operation(OperationClass.APP)
    def install_build(self, build_ref: Mapping[str, Any]) -> Mapping[str, Any]:
        """Execute one precise install action and return its Install Receipt.

        设备层只提供 transfer / shell / installer 三个设备原语；`build_ref` 必须是
        Build Catalog 已解析的完整 Build Ref（至少 app / slot / build_id /
        artifact_sha256），本方法不做任何 slot → Build 的解析（BL-011）。返回的
        Install Receipt 可供兼容、审计与诊断，不是权威安装状态——权威状态由
        Provisioning 回读后发布。未注入 `install_package_fn` 时 fail-closed。
        """
        if self._install_package_fn is None:
            raise DeviceProtocolError(
                "install_build requires install_package_fn on this DeviceSession"
            )
        missing = [
            key for key in ("app", "slot", "build_id", "artifact_sha256")
            if not str((build_ref or {}).get(key) or "").strip()
        ]
        if missing:
            raise DeviceProtocolError(
                "install_build needs a complete Build Ref; missing: " + ", ".join(missing)
            )

        def transfer(local: Path, remote: str) -> None:
            with self._operation(OperationClass.TRANSFER):
                if self.stage_guard is None:
                    self.write_fence.check()
                self._transfer_fn(local, remote, self.target)

        def shell(command: str) -> tuple[int, str]:
            result = self.shell(command, write=True)
            return int(result.code), f"{result.stdout}{result.stderr}"

        def installer(remote: str) -> dict[str, Any]:
            response = self._request(
                {"t": "ipa_install", "path": remote, "registration": "User"},
                write=True,
                timeout=300.0,
            )
            return {
                "bundle_id": response.get("bundle_id"),
                "rt_ms": 0.0,
            }

        # `package_root` 缺省时不传 `root`，让包仓库实现用它自己的默认根，与本方法
        # 改成注入之前的 `self.package_root or packages.ROOT` 等价。
        options: dict[str, Any] = {
            "code_root": self.code_root,
            "transfer": transfer,
            "shell": shell,
            "installer": installer,
        }
        if self.package_root is not None:
            options["root"] = self.package_root
        return self._install_package_fn(dict(build_ref), **options)

    def stored_build_marker(self, build_ref: Mapping[str, Any]) -> bytes | None:
        """返回该 Build 在包仓库 IPA 内的构建标记字节；IPA 不含标记时返回 None。

        与 `install_build` 读同一包仓库（同一 `package_root` / `code_root`）。只读宿主侧
        文件，不访问设备。未注入 `build_marker_fn` 时 fail-closed（BL-237）。
        """
        if self._build_marker_fn is None:
            raise DeviceProtocolError(
                "stored_build_marker requires build_marker_fn on this DeviceSession"
            )
        options: dict[str, Any] = {"code_root": self.code_root}
        if self.package_root is not None:
            options["root"] = self.package_root
        return self._build_marker_fn(dict(build_ref), **options)

    def put_file(self, local: Path, remote: str, *, permission: str = "644") -> None:
        source = local.resolve()
        if not source.is_file():
            raise DeviceProtocolError(f"local file does not exist: {source}")
        self._request(
            {
                "t": "file_put",
                "path": remote,
                "load": str(source),
                "perm": permission,
                "screen": False,
            },
            write=True,
            timeout=120.0,
        )

    def get_file(self, remote: str, local: Path) -> Path:
        destination = local.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._request(
            {
                "t": "file_get",
                "path": remote,
                "save": str(destination),
                "screen": False,
            },
            write=False,
            timeout=120.0,
        )
        if not destination.is_file():
            raise DeviceProtocolError(f"file_get did not create local file: {destination}")
        return destination

    def read_build_marker(self, bundle_id: str) -> bytes:
        """Read the marker from the registered bundle and reject registration drift."""
        def registered():
            rows = [row for row in self.app_list()
                    if row.get("bundle_id", row.get("bundleIdentifier")) == bundle_id]
            if len(rows) != 1:
                raise DeviceProtocolError("installed_app_registration_ambiguous")
            return rows[0]
        before = registered()
        remote = before.get("path")
        if not isinstance(remote, str) or not remote.startswith("/") or "\0" in remote:
            raise DeviceProtocolError("installed_app_path_invalid")
        with tempfile.TemporaryDirectory(prefix="autophone-build-marker-") as temporary:
            local = self.get_file(remote.rstrip("/") + "/autophone-ui-eval-build-marker.json",
                                  Path(temporary) / "marker.json")
            marker = local.read_bytes()
        if registered() != before:
            raise DeviceProtocolError("installed_app_identity_drift")
        return marker

    def read_installed_build(self, app: str, bundle_id: str) -> InstalledBuild | None:
        row = next(
            (
                item
                for item in self.app_list()
                if str(item.get("bundle_id") or item.get("bundleIdentifier") or "") == bundle_id
            ),
            None,
        )
        if row is None:
            return None
        short_version = str(row.get("short_version") or row.get("version") or "") or None
        build_version = str(row.get("build_version") or "") or None
        info_plist_sha256: str | None = None
        remote = row.get("path")
        if remote:
            if not isinstance(remote, str) or not remote.startswith("/") or "\0" in remote:
                raise DeviceProtocolError("installed_app_path_invalid")
            with tempfile.TemporaryDirectory(prefix="autophone-installed-build-") as temporary:
                local = self.get_file(remote.rstrip("/") + "/Info.plist", Path(temporary) / "Info.plist")
                raw_info = local.read_bytes()
                info_plist_sha256 = hashlib.sha256(raw_info).hexdigest()
                try:
                    info = plistlib.loads(raw_info)
                except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
                    raise DeviceProtocolError("installed_app_plist_invalid") from exc
            if not isinstance(info, dict) or info.get("CFBundleIdentifier") != bundle_id:
                raise DeviceProtocolError("installed_app_bundle_mismatch")
            actual_short = info.get("CFBundleShortVersionString")
            actual_build = info.get("CFBundleVersion")
            if not all(isinstance(value, str) and value for value in (actual_short, actual_build)):
                raise DeviceProtocolError("installed_app_version_missing")
            if ((short_version is not None and short_version != actual_short)
                    or (build_version is not None and build_version != actual_build)):
                raise DeviceProtocolError("installed_app_version_mismatch")
            after = next((item for item in self.app_list()
                          if item.get("bundle_id", item.get("bundleIdentifier")) == bundle_id), None)
            fields = ("path", "version", "short_version", "build_version")
            if after is None or any(after.get(key) != row.get(key) for key in fields):
                raise DeviceProtocolError("installed_app_identity_drift")
            short_version, build_version = actual_short, actual_build
        return InstalledBuild(app=app, bundle_id=bundle_id,
                              short_version=short_version, build_version=build_version,
                              info_plist_sha256=info_plist_sha256)

    @_stage_operation(OperationClass.FRIDA)
    def frida_probe(self, app_name: str) -> dict[str, Any]:
        from autophone.device.frida import probe_contract

        return dict(probe_contract(
            host=self.target.frida_endpoint,
            app_name=app_name,
            timeout_s=8.0,
        ))

    def set_location(
        self,
        latitude: float,
        longitude: float,
        *,
        speed: float,
        course: float,
        altitude: float = 0.0,
        horizontal_accuracy: float = 5.0,
        vertical_accuracy: float = 5.0,
    ) -> None:
        self._request({
            "t": "location",
            "lat": latitude,
            "lon": longitude,
            "alt": altitude,
            "hacc": horizontal_accuracy,
            "vacc": vertical_accuracy,
            "speed": speed,
            "course": course,
            "screen": False,
        }, write=True)

    def close(self) -> Mapping[str, Any]:
        self._closed = True
        return {
            "status": "closed",
            "host_id": self.target.host_id,
            "rig_id": self.target.rig_id,
            "rig_generation": self.target.rig_generation,
        }
