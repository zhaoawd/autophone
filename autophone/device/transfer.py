"""Guest shell commands for publishing a complete transfer atomically.

Also holds the bounded retry for guest SSH authentication rejections shared by
both transfer implementations (``autophone.device.session._ssh_stream_transfer``
and ``autophone.packages.store.scp_to_guest``).
"""
from dataclasses import dataclass, field
import logging
import re
import shlex
import subprocess
import time
from typing import Callable
import uuid

logger = logging.getLogger(__name__)

# guest dropbear 偶发整连接拒绝认证：ssh 以 255 退出，stderr 为
# `<user>@<host>: Permission denied (publickey,password).`。2026-09-24 vm 实测 3 次
# 均在约 6s 后的下一次尝试成功。判定为「退出码属于该命令的认证失败退出码 + ssh 客户端的
# 这句格式」：ssh 命令只认 255；scp 经 ssh 传输认证失败时自身以 1 退出，scp 命令认 1 或 255
# （`SCP_AUTH_EXIT_CODES`）。guest 上命令自身的 `bash: <path>: Permission denied` 与
# `scp: <path>: Permission denied` 不带括号内的认证方式列表，不重试。
AUTH_RETRY_INTERVAL_S = 6.0
AUTH_RETRY_LIMIT = 3
# 从首次尝试开始计：下一次重试的开始时刻超过该时限时不再重试。
AUTH_RETRY_WINDOW_S = 30.0
SSH_AUTH_EXIT_CODES = frozenset({255})
SCP_AUTH_EXIT_CODES = frozenset({1, 255})
_AUTH_REJECTED = re.compile(r"Permission denied \([a-z,-]+\)")


def ssh_auth_rejected(
    completed: subprocess.CompletedProcess, *, exit_codes: frozenset[int] = SSH_AUTH_EXIT_CODES,
) -> bool:
    """ssh / scp 客户端是否因认证被拒退出（远端命令未执行）。"""
    return (completed.returncode in exit_codes
            and _AUTH_REJECTED.search(completed.stderr or "") is not None)


def run_with_auth_retry(
    attempt: Callable[[], subprocess.CompletedProcess],
    *,
    what: str,
    exit_codes: frozenset[int] = SSH_AUTH_EXIT_CODES,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> subprocess.CompletedProcess:
    """执行一次 SSH 调用；认证被拒时间隔 ``AUTH_RETRY_INTERVAL_S`` 重试。

    最多重试 ``AUTH_RETRY_LIMIT`` 次，且每次重试的开始时刻不晚于首次尝试后
    ``AUTH_RETRY_WINDOW_S`` 秒。其他失败（非零退出、超时、OSError）原样返回或抛出，
    不重试。每次重试写 WARNING；重试用尽仍被拒写 ERROR，并返回最后一次结果，
    由调用方按原有逻辑报错。``attempt`` 每次调用须重新准备 stdin 等一次性资源。
    """
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    started = monotonic()
    retries = 0
    while True:
        completed = attempt()
        if not ssh_auth_rejected(completed, exit_codes=exit_codes):
            if retries:
                logger.info("%s: SSH authentication accepted after %d retr%s",
                            what, retries, "y" if retries == 1 else "ies")
            return completed
        excerpt = (completed.stderr or "").strip()[:200]
        next_start = monotonic() + AUTH_RETRY_INTERVAL_S
        if retries >= AUTH_RETRY_LIMIT or next_start - started > AUTH_RETRY_WINDOW_S:
            logger.error(
                "%s: SSH authentication rejected %d time(s) within %.1fs; giving up: %s",
                what, retries + 1, monotonic() - started, excerpt)
            return completed
        retries += 1
        logger.warning(
            "%s: SSH authentication rejected (%s); retry %d/%d in %.0fs",
            what, excerpt, retries, AUTH_RETRY_LIMIT, AUTH_RETRY_INTERVAL_S)
        sleep(AUTH_RETRY_INTERVAL_S)


_GUEST_PATH = (
    "export PATH=/var/jb/usr/bin:/var/jb/bin:/iosbinpack64/usr/bin:"
    "/iosbinpack64/bin:/usr/bin:/bin; "
)


@dataclass(frozen=True)
class GuestTransfer:
    remote: str
    size: int
    token: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def staging(self) -> str:
        # A sibling keeps rename on the destination filesystem.
        return f"{self.remote}.autophone-{self.token}.part"

    def _prepare(self) -> str:
        return (
            _GUEST_PATH
            + f"tmp={shlex.quote(self.staging)}; dest={shlex.quote(self.remote)}; "
            + "command -v rm >/dev/null && command -v mv >/dev/null "
            + "&& command -v wc >/dev/null || exit 127; "
            + "trap 'rc=$?; rm -f -- \"$tmp\"; exit \"$rc\"' 0; "
            + "trap 'exit 129' 1; trap 'exit 130' 2; trap 'exit 143' 15; "
            + "test ! -d \"$dest\" || exit 1; "
        )

    def _publish(self) -> str:
        return (
            'bytes=$(wc -c < "$tmp") || exit $?; '
            + f'test "$bytes" -eq {self.size} || '
            + "{ printf '%s\\n' 'transfer size mismatch' >&2; exit 1; }; "
            + 'mv -f -- "$tmp" "$dest"'
        )

    def stream_command(self, helper: str) -> str:
        return (
            self._prepare()
            # Avoid following an accidentally pre-existing staging symlink.
            + '(set -C; : > "$tmp") || exit $?; '
            + f'{shlex.quote(helper)} > "$tmp" || exit $?; '
            + self._publish()
        )

    def publish_command(self) -> str:
        return self._prepare() + self._publish()

    def cleanup_command(self) -> str:
        return _GUEST_PATH + f"rm -f -- {shlex.quote(self.staging)}"


def upstream_transfer(local, remote: str, *, sock_path: str, timeout: float = 120) -> None:
    """Publish verified host bytes through the selected upstream file route once."""
    from autophone.device.uds import request
    from autophone.device.session import DeviceProtocolError

    response, _ = request({"t": "file_put", "load": str(local), "path": remote,
                           "perm": "644", "screen": False}, sock_path=sock_path,
                          protocol="upstream_v1", parse_image=False, timeout=timeout)
    if response.get("ok") is not True:
        raise DeviceProtocolError(f"upstream transfer failed: {response.get('error')}")
