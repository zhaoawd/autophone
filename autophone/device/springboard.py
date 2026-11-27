"""Guest SpringBoard 进程身份探测。

SpringBoard 崩溃后由 launchd 立即重启，新进程 pid 不同；它退出时前台 App 随之被杀。
启动 App 前后比对 SpringBoard 身份，可把这类设施故障与 App 未就绪区分开。

读法：经 UDS guest shell 执行 ``LC_ALL=C TZ=UTC0 ps -ww -axo pid=,lstart=,comm=``，取可执行名为
``SpringBoard`` 的进程，身份为 ``(pid, 启动时间)``。不用 UDS ``app_list``：该命令按
LaunchServices 登记的 App 枚举，pid 由 ``FBSSystemService`` 向 SpringBoard 查询，
SpringBoard 重启过程中查询本身不可用，且系统进程 SpringBoard 不在 LaunchServices 的
App 枚举中（``autophone.device.ax_session.SYSTEM_PROCESS_NAMES`` 注释记录的 frida
实测结论相同）。``ps`` 读内核进程表，不依赖 SpringBoard。guest shell 中 ``ps`` 的
``-o lstart=`` / ``-o comm=`` 输出格式与 ``autophone.device.runtime_observation`` 使用的
相同；2026-09-24 vm 的 ``ps`` 输出中 SpringBoard 的 COMMAND 列为
``/System/Library/CoreServices/SpringBoard.app/SpringBoard``。

``LC_ALL=C TZ=UTC0`` 固定 ``lstart`` 的语言与时区，身份字符串不随 guest 设置变化；``-ww``
防止 ``comm`` 按终端宽度截断。输出无法按预期格式解析时抛 ``SpringBoardProbeError``（不可判定），
不把格式差异当作「SpringBoard 不在」。

探测直接发 UDS ``shell`` 请求，不经 ``run_shell``：``run_shell`` 的 socket 超时是 guest 执行上限
加 20s，探测需要更短的上限（``SOCKET_TIMEOUT_S``），调用方才能按秒级间隔复核。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

SPRINGBOARD_EXECUTABLE = "SpringBoard"
PS_COMMAND = "LC_ALL=C TZ=UTC0 ps -ww -axo pid=,lstart=,comm="
# guest 侧执行上限与宿主 socket 上限。ps 正常在 1s 内返回。
PROBE_TIMEOUT_MS = 5_000
SOCKET_TIMEOUT_S = 8.0

# `  24796 Thu Sep 24 18:16:37 2026     /System/Library/CoreServices/SpringBoard.app/SpringBoard`
_PS_ROW = re.compile(
    r"^\s*(?P<pid>\d+)\s+"
    r"(?P<started>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2}\s+\d{4})\s+"
    r"(?P<comm>\S.*?)\s*$"
)


class SpringBoardProbeError(RuntimeError):
    """SpringBoard 身份不可判定：shell 失败、非零退出或输出格式不符。"""


@dataclass(frozen=True)
class SpringBoardIdentity:
    pid: int
    started: str

    def describe(self) -> str:
        return f"pid {self.pid} started {self.started}"


def parse_springboard_rows(stdout: str) -> list[SpringBoardIdentity]:
    """从 ``ps -axo pid=,lstart=,comm=`` 输出中取出全部 SpringBoard 进程。

    没有任何一行符合格式时抛 ``SpringBoardProbeError``：此时无法区分「SpringBoard 不在」
    与「ps 输出格式不同」。
    """
    parsed = 0
    matches: list[SpringBoardIdentity] = []
    for line in (stdout or "").splitlines():
        row = _PS_ROW.match(line)
        if row is None:
            continue
        parsed += 1
        comm = row.group("comm")
        if comm.rsplit("/", 1)[-1] == SPRINGBOARD_EXECUTABLE:
            started = " ".join(row.group("started").split())
            matches.append(SpringBoardIdentity(pid=int(row.group("pid")), started=started))
    if parsed == 0:
        raise SpringBoardProbeError("ps output has no row in the 'pid lstart comm' format")
    return matches


def probe_springboard(
    sock_path: str | None = None,
    *,
    request_fn: Callable[..., Any] | None = None,
    timeout_ms: int = PROBE_TIMEOUT_MS,
    socket_timeout_s: float = SOCKET_TIMEOUT_S,
) -> SpringBoardIdentity | None:
    """返回当前 SpringBoard 身份；进程表中没有 SpringBoard 时返回 None。

    请求失败或超时、guest 执行超时或输出截断、非零退出、输出格式不符或出现多个
    SpringBoard 进程时抛 ``SpringBoardProbeError``。
    """
    from autophone.device.shell import JB_PATH_EXPORT, resolve_sock

    if request_fn is None:
        from autophone.device.uds import request as request_fn
    message = {"t": "shell", "cmd": f"{JB_PATH_EXPORT}; {PS_COMMAND}", "screen": False,
               "timeout_ms": int(timeout_ms)}
    try:
        response, _sample = request_fn(message, sock_path=sock_path or resolve_sock(),
                                       timeout=socket_timeout_s, parse_image=False)
    except Exception as exc:  # noqa: BLE001 - 传输失败即不可判定，由调用方决定处理
        raise SpringBoardProbeError(f"guest shell failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        error = response.get("error") if isinstance(response, dict) else response
        raise SpringBoardProbeError(f"guest shell failed: {error!r}")
    if response.get("timed_out") or response.get("truncated"):
        raise SpringBoardProbeError("guest ps timed out or its output was truncated")
    code = response.get("code")
    if code != 0:
        stderr = str(response.get("stderr") or "").strip()[:200]
        raise SpringBoardProbeError(f"ps exited {code}: {stderr!r}")
    matches = parse_springboard_rows(str(response.get("stdout") or ""))
    if len(matches) > 1:
        raise SpringBoardProbeError(
            "more than one SpringBoard process: " + ", ".join(m.describe() for m in matches))
    return matches[0] if matches else None


__all__ = [
    "PS_COMMAND",
    "SpringBoardIdentity",
    "SpringBoardProbeError",
    "parse_springboard_rows",
    "probe_springboard",
]
