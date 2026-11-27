"""安装前只读检查：SpringBoard 是否带 Frida agent 残留映射（BL-232 阶段 1，I1）。

背景（backlog §9.69、评估文档 ``docs/platform-springboard-attach-alternatives.md`` §2、§4.7）：
Frida 17.16.1 attach SpringBoard 后出现一个 17.8 MB（``0x11c8000``）、``Tag: 255``、``COW`` 的
区域，detach 后保留为 ``---/---``；其大小、标签、保护属性、共享模式与 25 份历史 ``.ips`` 的故障
区域一致。2026-10-08 被 attach 过、其后未重启的 SpringBoard 上的 11 次安装伴随 11 次重启，
未被 attach 过的 82 次安装 0 次重启（相关关系，根因链未验证）。

用户裁定（2026-10-09，评估文档 §七第 2 项甲）：检出残留即拒绝安装。本模块只做检查与判定，
不重启 SpringBoard，不 attach 任何进程。

读法：

1. 经 guest shell 执行 ``ps``（``autophone.device.springboard.PS_COMMAND``），取 SpringBoard 的
   pid 与启动时间。
2. 只对该 pid 执行 ``procexp <pid> regions``。procexp 只对 SpringBoard 运行：对
   osanalyticshelper、frida-server 执行其他子命令时 procexp 自身曾因 ``EXC_GUARD`` 被结束（§9.74）。
3. 再执行一次 ``ps``，SpringBoard 身份（pid、启动时间）须与第 1 步相同，否则本次读数不可判定。

判据（2026-10-09 用户裁定放宽，回执 ``springboard-residue-check.v2.json``）：出现任一标签为
``Tag: 255`` 的区域即判为残留，不再要求保护属性 ``---/---`` 与大小 ``0x11c8000``。依据：attach 前的
SpringBoard 没有 ``Tag: 255`` 区域（§9.69 两份、§9.84、§9.86 的读数均为 0 个）；attach 期间与 detach
后都出现多组 ``Tag: 255`` 区域（17M 区域在 attach 期间为 ``r-x/rwx``，detach 后为 ``---/---``，另有
16K、112K 区域）。阶段 1 的 v1 判据（三条件同时成立）只适用于 v1 记录。

检查本身失败（shell 失败、非零退出、输出无法解析、SpringBoard 不在或检查期间重启）时判为
``check_failed``，调用方按 fail-closed 拒绝安装：此时无法证明 SpringBoard 不带残留。
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any, Callable

from autophone.logging import get_logger
from autophone.device.springboard import (
    PS_COMMAND,
    SpringBoardIdentity,
    SpringBoardProbeError,
    parse_springboard_rows,
)

logger = get_logger(__name__)

SCHEMA = "autophone.springboard-residue-check/v2"
SCHEMA_FILE = "springboard-residue-check.v2.json"

# 拒绝安装的两个错误码。
RESIDUE_CODE = "springboard_frida_residue"
CHECK_FAILED_CODE = "springboard_residue_check_failed"

PROCEXP = "/iosbinpack64/usr/local/bin/procexp"
REGION_PROBE = "/var/jb/usr/local/bin/autophone-springboard-regions"
REGION_PROBE_ENV = "AUTOPHONE_SPRINGBOARD_REGION_PROBE"
RESIDUE_TAG = "Tag: 255"
#: v2 判据：任一 ``Tag: 255`` 区域。
RESIDUE_MATCH = "any_region"
#: v1 判据中的保护属性与大小，只用于在记录中标出 detach 后的 17.8 MB 区域（``frida_detach_region``）。
DETACHED_PROTECTION = "---/---"
DETACHED_SIZE_BYTES = 0x11C8000

RESIDUE_REMEDY = "SpringBoard 带有 Frida 残留映射，需重启 SpringBoard 后重试"
CHECK_FAILED_REMEDY = "无法确认 SpringBoard 不带 Frida 残留映射，按 fail-closed 拒绝安装；核对 guest shell 与区域检查程序后重试"

# `Tag: 255               0x843291d8 000000010280c000-00000001039d4000 [  17M]---/--- COW `
_REGION_ROW = re.compile(
    r"^(?P<tag>\S.*?)\s+0x[0-9a-fA-F]+\s+"
    r"(?P<start>[0-9a-fA-F]+)-(?P<end>[0-9a-fA-F]+)\s+"
    r"\[\s*(?P<size_label>[^\]]*?)\s*\]"
    r"(?P<protection>[r-][w-][x-]/[r-][w-][x-])\s+"
    r"(?P<share>\S+)"
)

ShellFn = Callable[[str], "tuple[int, str]"]


@dataclass(frozen=True)
class Region:
    tag: str
    start: int
    end: int
    protection: str
    share: str

    @property
    def size(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "start": f"0x{self.start:x}",
            "end": f"0x{self.end:x}",
            "size": self.size,
            "protection": self.protection,
            "share": self.share,
        }


class ResidueCheckError(RuntimeError):
    """检查本身不可判定。"""


def parse_regions(output: str) -> list[Region]:
    """解析 ``procexp <pid> regions`` 输出中的区域行；不符合格式的行（如 stderr ``Error 0``）跳过。"""
    regions: list[Region] = []
    for line in (output or "").splitlines():
        row = _REGION_ROW.match(line)
        if row is None:
            continue
        start, end = int(row.group("start"), 16), int(row.group("end"), 16)
        if end <= start:
            continue
        regions.append(Region(
            tag=row.group("tag").strip(),
            start=start,
            end=end,
            protection=row.group("protection"),
            share=row.group("share"),
        ))
    return regions


def is_residue(region: Region) -> bool:
    """v2 判据：标签为 ``Tag: 255`` 即判为残留区域。"""
    return region.tag == RESIDUE_TAG


def is_detached_agent_region(region: Region) -> bool:
    """v1 三条件：detach 后保留的 17.8 MB 区域（§9.69）。只用于记录，不参与 v2 判定。"""
    return (
        region.tag == RESIDUE_TAG
        and region.protection == DETACHED_PROTECTION
        and region.size == DETACHED_SIZE_BYTES
    )


def _springboard(shell: ShellFn) -> SpringBoardIdentity:
    code, output = shell(PS_COMMAND)
    if code != 0:
        raise ResidueCheckError(f"ps exited {code}: {str(output).strip()[:200]!r}")
    try:
        matches = parse_springboard_rows(str(output))
    except SpringBoardProbeError as exc:
        raise ResidueCheckError(str(exc)) from exc
    if not matches:
        raise ResidueCheckError("SpringBoard is not in the guest process table")
    if len(matches) > 1:
        raise ResidueCheckError(
            "more than one SpringBoard process: " + ", ".join(m.describe() for m in matches))
    return matches[0]


def _criterion() -> dict[str, Any]:
    return {"tag": RESIDUE_TAG, "match": RESIDUE_MATCH}


_PROBE_ROW = re.compile(r"tag255 ([0-9a-f]{16}) ([0-9a-f]{16}) ([r-][w-][x-]/[r-][w-][x-]) (COW|PRV|OTHER)")
_PROBE_DONE = re.compile(r"done ([1-9][0-9]*) ([1-9][0-9]*)")


def _probe_regions(output: str, pid: int) -> tuple[int, list[Region]]:
    lines = output.splitlines()
    done = _PROBE_DONE.fullmatch(lines[-1]) if lines else None
    if done is None or int(done[1]) != pid:
        raise ResidueCheckError("region probe did not complete for the selected SpringBoard")
    count = int(done[2])
    if not len(lines) - 1 <= count <= 100000:
        raise ResidueCheckError("region probe reported an inconsistent region count")
    regions = []
    previous_end = 0
    for line in lines[:-1]:
        row = _PROBE_ROW.fullmatch(line)
        if row is None:
            raise ResidueCheckError("invalid region from the SpringBoard probe")
        start, end = int(row[1], 16), int(row[2], 16)
        if start < previous_end or end <= start:
            raise ResidueCheckError("invalid region boundaries from the SpringBoard probe")
        regions.append(Region(RESIDUE_TAG, start, end, row[3], row[4]))
        previous_end = end
    return count, regions


def _read_regions(shell: ShellFn, pid: int, record: dict[str, Any]) -> tuple[int, list[Region]]:
    probe = os.environ.get(REGION_PROBE_ENV, "").strip()
    if not probe:
        command = f"{PROCEXP} {pid} regions"
        record["command"] = command
        code, output = shell(command)
        if code == 0:
            regions = parse_regions(str(output))
            if not regions:
                raise ResidueCheckError("procexp regions output has no row in the expected format")
            return len(regions), regions
        if code != 127 or PROCEXP not in output or "not found" not in output:
            raise ResidueCheckError(f"procexp exited {code}: {str(output).strip()[:200]!r}")
        probe = REGION_PROBE
    path = PurePosixPath(probe)
    if not path.is_absolute() or ".." in path.parts or not probe.isprintable():
        raise ResidueCheckError(f"{REGION_PROBE_ENV} must be an absolute guest path")
    command = f"{shlex.quote(probe)} {pid}"
    record["command"] = command
    code, output = shell(command)
    if code != 0:
        raise ResidueCheckError(f"region probe exited {code}: {str(output).strip()[:200]!r}")
    return _probe_regions(str(output), pid)


def inspect_springboard_residue(shell: ShellFn) -> dict[str, Any]:
    """读取 SpringBoard 区域表并判定；不抛异常，结果写在返回记录的 ``verdict`` 中。

    ``shell(command) -> (exit_code, output)`` 在 guest 内执行命令（安装链路已有的 guest shell）。
    返回记录符合 ``springboard-residue-check.v2.json``：``verdict`` 为 ``clean``、``residue``
    或 ``check_failed``；后两者带 ``code`` 与 ``detail``，调用方据此拒绝安装。
    """
    started = time.monotonic()
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "verdict": "check_failed",
        "code": CHECK_FAILED_CODE,
        "detail": "",
        "criterion": _criterion(),
        "command": None,
        "springboard": None,
        "regions_parsed": 0,
        "tag255_regions": [],
        "residue_regions": [],
        "detached_agent_regions": 0,
        "elapsed_ms": 0,
    }
    try:
        before = _springboard(shell)
        record["springboard"] = {"pid": before.pid, "started": before.started}
        count, regions = _read_regions(shell, before.pid, record)
        record["regions_parsed"] = count
        after = _springboard(shell)
        if after != before:
            raise ResidueCheckError(
                f"SpringBoard changed during the check: {before.describe()} -> {after.describe()}")
        tag255 = [region for region in regions if region.tag == RESIDUE_TAG]
        residue = [region for region in tag255 if is_residue(region)]
        record["tag255_regions"] = [region.to_dict() for region in tag255]
        record["residue_regions"] = [region.to_dict() for region in residue]
        record["detached_agent_regions"] = sum(1 for region in tag255 if is_detached_agent_region(region))
        if residue:
            starts = [r.to_dict()["start"] for r in residue]
            shown = ", ".join(starts[:3]) + (f" 等 {len(starts)} 个" if len(starts) > 3 else "")
            record.update(
                verdict="residue",
                code=RESIDUE_CODE,
                detail=(
                    f"{RESIDUE_REMEDY}（SpringBoard {before.describe()}，"
                    f"Tag: 255 区域 {shown}；其中 detach 后的 17.8 MB 区域 "
                    f"{record['detached_agent_regions']} 个）"
                ),
            )
        else:
            record.update(verdict="clean", code=None, detail="no Frida residue region")
    except Exception as exc:  # noqa: BLE001 - 检查失败即不可判定，由调用方 fail-closed
        record.update(
            verdict="check_failed",
            code=CHECK_FAILED_CODE,
            detail=f"{CHECK_FAILED_REMEDY}：{type(exc).__name__}: {exc}",
        )
    record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    if record["verdict"] == "clean":
        logger.info(
            "SpringBoard residue check clean: pid=%s tag255=%d elapsed_ms=%d",
            (record["springboard"] or {}).get("pid"), len(record["tag255_regions"]),
            record["elapsed_ms"],
        )
    else:
        logger.error(
            "SpringBoard residue check refused install: code=%s detail=%s",
            record["code"], record["detail"],
        )
    return record


__all__ = [
    "CHECK_FAILED_CODE",
    "DETACHED_SIZE_BYTES",
    "PROCEXP",
    "RESIDUE_CODE",
    "Region",
    "SCHEMA",
    "SCHEMA_FILE",
    "inspect_springboard_residue",
    "is_detached_agent_region",
    "is_residue",
    "parse_regions",
]
