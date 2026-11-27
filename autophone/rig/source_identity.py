"""磁盘源码身份与常驻进程启动基线（BL-151、BL-209）。

source_identity() 读取磁盘；process_source_identity() 返回包首次导入时的源码摘要。
后者在 paths、version 和应用模块加载前冻结，避免构造 Agent 或首次访问 /health 时
把磁盘的新代码误记为驻留代码。进程身份文件、Agent 握手与控制面健康响应共用该基线。

摘要自动覆盖 autophone 包内 Python、JavaScript、SQL、Shell、Swift、TypeScript
源码，以及 `apps/` 下按包名导入的 App Python（Operation 执行器、状态驱动、Recipe 注册），
包含未提交和未跟踪源码，文件新增、删除、重命名均改变摘要。不扫描运行产物、文档与
用例/Recipe 等 YAML 业务资产（使用时重新读取），不包含字节码、缓存或第三方依赖。
同一完整包在 checkout 与 site-packages 中使用相同算法；安装组件集合不同会不一致。
该摘要不证明外部插件、部署资产或第三方依赖相同，也不提供运行中源码热更新保证。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from autophone._source_digest import PROCESS_SOURCE_DIGEST, package_digest, source_paths
from autophone.paths import CODE_ROOT
from autophone.util import write_text_atomic


#: 兼容诊断与现有消费者的启动时路径快照；摘要每次重新枚举，不依赖此清单。
KEY_MODULE_PATHS: tuple[str, ...] = source_paths(CODE_ROOT)

#: 在跑 agent 把自己的身份写在 runtime dir 的这个文件里。
IDENTITY_FILE_NAME = "rig-agent.identity.json"

#: `compare_source_identity()` 的三态。
IDENTITY_MATCH = "match"
IDENTITY_MISMATCH = "mismatch"
IDENTITY_UNKNOWN = "unknown"

#: 派单准入拒绝码（host 身份与 rig 行身份的 `module_digest` 不一致）。
RIG_AGENT_STALE = "rig_agent_stale"

#: 派单准入拒绝码（host 身份与控制面进程 `/health` 自报身份的 `module_digest` 不一致）。
CONTROL_PLANE_STALE = "control_plane_stale"

#: 拒绝与日志共用的重启指引。
RESTART_HINT = (
    "重启 rig agent：跑 scripts/start_vphone.sh，或 kill 掉在跑的 "
    "autophone.rig.main 进程后按原命令重起"
)

#: 控制面进程的重启指引。控制面重启后 rig agent 经 WebSocket 自动重连（2026-09-16
#: 实测约 1 s），不需要一并重启 agent。
CONTROL_RESTART_HINT = (
    "重启控制面：跑 scripts/start_vphone.sh，或 kill 掉 autophone.control.http "
    "后按原命令重起"
)

_GIT_TIMEOUT_S = 5.0


def _code_root(code_root: Path | str | None = None) -> Path:
    return Path(code_root) if code_root is not None else CODE_ROOT


def module_digest(code_root: Path | str | None = None) -> str:
    """当前磁盘上包内源码的摘要；不使用 Git 或人工维护的模块清单。"""
    return package_digest(_code_root(code_root))


def _git_output(root: Path, *args: str) -> str | None:
    """跑一条 git 命令；任何失败都返回 None，不抛错。"""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def git_state(code_root: Path | str | None = None) -> tuple[str | None, bool | None]:
    """返回 `(git_head, dirty)`；不是 git 工作树或 git 不可用时返回 `(None, None)`。

    `dirty` 只看已跟踪文件（`--untracked-files=no`）：`runs/`、`experiments/` 下的
    产物是运行痕迹，把它们算进 dirty 会让这个字段恒为真。该字段只用于诊断。
    """
    root = _code_root(code_root)
    head = (_git_output(root, "rev-parse", "HEAD") or "").strip()
    if not head:
        return None, None
    status = _git_output(root, "status", "--porcelain", "--untracked-files=no")
    dirty = None if status is None else bool(status.strip())
    return head, dirty


def source_identity(code_root: Path | str | None = None) -> dict[str, Any]:
    """当前磁盘源码身份。git 信息取不到时置 null，不抛错。"""
    from autophone import __version__

    head, dirty = git_state(code_root)
    return {
        "package_version": str(__version__),
        "git_head": head,
        "dirty": dirty,
        "module_digest": module_digest(code_root),
    }


@lru_cache(maxsize=1)
def _process_source_identity() -> dict[str, Any]:
    """包首次导入时的源码摘要；Git/版本字段仅作首次查询时的诊断。"""
    from autophone import __version__

    head, dirty = git_state()
    return {
        "package_version": str(__version__),
        "git_head": head,
        "dirty": dirty,
        "module_digest": PROCESS_SOURCE_DIGEST,
    }


class SourceProcessStale(RuntimeError):
    """A resident process must restart before consuming changed package source."""

    code = "host_process_stale"


def process_source_identity() -> dict[str, Any]:
    """Return a copy so callers cannot mutate the shared process identity."""
    return dict(_process_source_identity())


def assert_process_source_current() -> None:
    """Reject a process whose package source has changed since its first import."""
    current = module_digest()
    if current != PROCESS_SOURCE_DIGEST:
        raise SourceProcessStale(
            "进程启动源码与当前磁盘不一致："
            f"process module_digest={PROCESS_SOURCE_DIGEST[:12]}，"
            f"disk module_digest={current[:12]}。请重启该进程。"
        )


def identity_digest(identity: Mapping[str, Any] | None) -> str | None:
    """取出可比较的 `module_digest`；缺字段、空串或形态不对都返回 None。"""
    if not isinstance(identity, Mapping):
        return None
    value = str(identity.get("module_digest") or "").strip()
    return value or None


def short_digest(identity: Mapping[str, Any] | str | None) -> str:
    """诊断用的摘要前 12 位；取不到时返回 `unknown`。"""
    value = identity if isinstance(identity, str) else identity_digest(identity)
    return (value or "unknown")[:12]


def compare_source_identity(
    host: Mapping[str, Any] | None,
    other: Mapping[str, Any] | None,
) -> str:
    """比对两份身份：`match` / `mismatch` / `unknown`。

    只用 `module_digest`。任一侧没有可比较的摘要时返回 `unknown`——调用方按各自的
    兼容口径处理（派单准入放行并告警，bring-up 重启 agent）。
    """
    host_digest = identity_digest(host)
    other_digest = identity_digest(other)
    if host_digest is None or other_digest is None:
        return IDENTITY_UNKNOWN
    return IDENTITY_MATCH if host_digest == other_digest else IDENTITY_MISMATCH


def compare_reported_identity(
    identity: Mapping[str, Any] | None,
    *,
    subject: str = "在跑的进程",
    host_label: str = "本进程",
    code_root: Path | str | None = None,
) -> tuple[str, str]:
    """比对另一个进程自报的身份与本进程身份，返回 `(状态, 中文说明)`。

    唯一的比对入口：`identity_file_status()`（agent 的身份文件）、bring-up 脚本里的
    控制面 `/health` 判定与派单准入都从这里取结论，不各写一份。`subject` 与
    `host_label` 只改说明里的称呼，不改判据。
    """
    host = source_identity(code_root)
    verdict = compare_source_identity(host, identity)
    if verdict == IDENTITY_MATCH:
        return verdict, f"源码身份一致（{describe_identity(host)}）"
    if verdict == IDENTITY_MISMATCH:
        return verdict, (
            f"源码身份不一致：{subject} module_digest={short_digest(identity)}，"
            f"{host_label} module_digest={short_digest(host)}"
        )
    return IDENTITY_UNKNOWN, f"{subject}没有上报可比较的 module_digest"


def describe_identity(identity: Mapping[str, Any] | None) -> str:
    """一行诊断串：摘要前 12 位 + 版本 + git head 前 12 位。"""
    if not isinstance(identity, Mapping):
        return "module_digest=unknown"
    head = str(identity.get("git_head") or "")
    return "module_digest={} package_version={} git_head={}{}".format(
        short_digest(identity),
        str(identity.get("package_version") or "unknown"),
        head[:12] or "unknown",
        "+dirty" if identity.get("dirty") else "",
    )


# ─── 在跑 agent 的身份文件 ───────────────────────────────────────────────────


def identity_file_path(runtime_dir: Path | str) -> Path:
    return Path(runtime_dir) / IDENTITY_FILE_NAME


def write_identity_file(
    runtime_dir: Path | str,
    *,
    pid: int,
    rig_id: str,
    identity: Mapping[str, Any] | None = None,
) -> Path:
    """在跑 agent 把自己的源码身份写到 runtime dir。

    由持有进程锁的那个进程写（`process_guard.agent_process_guard`），所以文件里的
    `pid` 就是在跑 agent 的 pid；host 侧据此确认读到的身份属于**这个**进程。
    """
    record = {
        "pid": int(pid),
        "rig_id": str(rig_id),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_identity": dict(identity if identity is not None else process_source_identity()),
    }
    return write_text_atomic(
        identity_file_path(runtime_dir),
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
    )


def read_identity_file(runtime_dir: Path | str) -> dict[str, Any] | None:
    """读身份文件；文件缺失或内容不成形返回 None。"""
    try:
        raw = json.loads(identity_file_path(runtime_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def identity_file_status(
    runtime_dir: Path | str,
    *,
    pid: int | None = None,
    code_root: Path | str | None = None,
) -> tuple[str, str]:
    """比对在跑 agent 的身份文件与本进程身份，返回 `(状态, 中文说明)`。

    状态与 `compare_source_identity()` 同三态。`unknown` 覆盖「文件不存在」（本条
    落地之前起的 agent）与「文件属于别的 pid」两种情况：两种都无法证明在跑的代码是
    当前代码，bring-up 按需要重启处理。
    """
    record = read_identity_file(runtime_dir)
    if record is None:
        return IDENTITY_UNKNOWN, "在跑的 Agent 没写源码身份文件（本条落地之前起的进程）"
    if pid is not None and int(record.get("pid") or 0) != int(pid):
        return IDENTITY_UNKNOWN, (
            f"源码身份文件记的是 pid={record.get('pid')}，在跑的是 pid={pid}"
        )
    running = record.get("source_identity")
    running_identity = running if isinstance(running, Mapping) else None
    status, reason = compare_reported_identity(
        running_identity,
        subject="在跑的 Agent",
        host_label="本次 bring-up",
        code_root=code_root,
    )
    if status == IDENTITY_UNKNOWN:
        return status, "源码身份文件里没有可比较的 module_digest"
    return status, reason


__all__ = [
    "CONTROL_PLANE_STALE",
    "CONTROL_RESTART_HINT",
    "IDENTITY_FILE_NAME",
    "IDENTITY_MATCH",
    "IDENTITY_MISMATCH",
    "IDENTITY_UNKNOWN",
    "KEY_MODULE_PATHS",
    "RESTART_HINT",
    "RIG_AGENT_STALE",
    "SourceProcessStale",
    "assert_process_source_current",
    "compare_reported_identity",
    "compare_source_identity",
    "describe_identity",
    "git_state",
    "identity_digest",
    "identity_file_path",
    "identity_file_status",
    "module_digest",
    "process_source_identity",
    "read_identity_file",
    "short_digest",
    "source_identity",
    "write_identity_file",
]
