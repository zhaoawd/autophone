"""Emit one Rig's DeviceTarget document from the bring-up environment.

`autophone-rig-agent --target <json>` 需要一份 16 字段的 DeviceTarget JSON，而
`scripts/start_vphone.sh` 手里正好有全部字段的权威取值。

用 Python 而不是在 shell 里拼 JSON，是为了让 `DeviceTarget.__post_init__` 的校验
（绝对路径、端口范围、SSH/Frida 不同端口、UDID 非空）成为写盘的前置条件：字段不
合法就退 2 且不落文件，调用方据此跳过 Control Plane 引导而不是写出一份坏文档。

用法：

    python -m autophone.rig.emit_target --out /tmp/autophone-rigs/vm/device-target.json

取值优先级：命令行参数 > 环境变量 > 少量安全默认值（见 FIELD_SOURCES）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Mapping

from autophone.rig.target import DeviceTarget, TargetConfigurationError


# 字段 → (命令行 dest, 环境变量候选（按顺序取第一个非空）, 默认值 | None 表示必填)
FIELD_SOURCES: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    ("host_id", "host_id", ("AUTOPHONE_HOST_ID",), None),
    ("rig_id", "rig_id", ("AUTOPHONE_RIG_ID", "AP_DEVICE"), None),
    ("rig_generation", "rig_generation", ("AUTOPHONE_RIG_GENERATION",), "1"),
    ("cli_dir", "cli_dir", ("VPHONE_CLI_DIR",), None),
    ("vm_dir", "vm_dir", ("VM_DIR",), None),
    ("socket", "socket", ("VPHONE_SOCK",), None),
    ("expected_usb_udid", "expected_usb_udid", ("AUTOPHONE_EXPECTED_USB_UDID",), None),
    ("ssh_host", "ssh_host", ("AUTOPHONE_SSH_HOST",), "127.0.0.1"),
    ("ssh_port", "ssh_port", ("AUTOPHONE_SSH_PORT",), None),
    ("frida_host", "frida_host", ("AUTOPHONE_FRIDA_HOST_ADDR",), "127.0.0.1"),
    ("frida_port", "frida_port", ("AUTOPHONE_FRIDA_HOST_PORT",), None),
    ("frida_guest_port", "frida_guest_port", ("AUTOPHONE_FRIDA_GUEST_PORT",), None),
    ("log_dir", "log_dir", ("VPHONE_LOG_DIR",), None),
    ("temp_dir", "temp_dir", ("AUTOPHONE_RIG_TEMP_DIR", "AUTOPHONE_RIG_RUNTIME_DIR"), None),
    ("package_cache", "package_cache", ("AUTOPHONE_RIG_PACKAGE_CACHE",), "<derived>"),
    # runtime_dir 显式跟随 AUTOPHONE_RIG_RUNTIME_DIR：健康守护把 health.json / .pid / .log
    # 写在那里，agent 注入给子进程的 VPHONE_HEALTH_* 必须指同一处，否则健康读数恒 missing。
    ("runtime_dir", "runtime_dir", ("AUTOPHONE_RIG_RUNTIME_DIR",), None),
)

# 需要规范化（消掉 `..` / 符号链接）的字段。同一台 VM 从不同 cwd 或不同写法拿到的
# 路径必须产出同一个字符串，否则 rig_targets 的 UNIQUE(host_id, vm_dir) 会把它当成
# 新 target，而 uq_rig_targets_host_usb_udid 又认为 UDID 重复，注册直接失败。
PATH_FIELDS = (
    "cli_dir", "vm_dir", "socket", "log_dir", "temp_dir", "package_cache", "runtime_dir",
)

# temp_dir 已知时 package_cache 的兜底位置（rig 私有，不与别的 Rig 共享）。
# 用哨兵值而不是 `default=None` 表达「无需外部提供」，这样 FIELD_SOURCES 里
# `default is None` 恰好等于「调用方必须提供」，脚本侧的接口契约测试可以直接据此断言。
PACKAGE_CACHE_UNDER_TEMP = "package-cache"
PACKAGE_CACHE_DERIVED = "<derived>"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autophone.rig.emit_target",
        description="write one DeviceTarget JSON from the bring-up environment",
    )
    parser.add_argument("--out", type=Path, required=True, help="输出 JSON 路径")
    for field, dest, env_names, default in FIELD_SOURCES:
        parser.add_argument(
            "--" + field.replace("_", "-"),
            dest=dest,
            default=None,
            help=f"覆盖 {field}（默认取 {' / '.join(env_names)}"
            + (f"，缺省 {default}）" if default is not None else "，必填）"),
        )
    parser.add_argument("--protocol", choices=("legacy", "upstream_v1"))
    parser.add_argument("--core-bundle", type=Path)
    return parser


def _resolve_field(
    field: str,
    env_names: tuple[str, ...],
    default: str | None,
    override: str | None,
    env: Mapping[str, str],
) -> str:
    if override is not None and str(override).strip():
        return str(override).strip()
    for name in env_names:
        value = env.get(name, "").strip()
        if value:
            return value
    if default is not None:
        return default
    raise TargetConfigurationError(
        f"missing {field}: set one of {', '.join(env_names)} or pass "
        f"--{field.replace('_', '-')}"
    )


def target_from_environment(
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str | None] | None = None,
) -> DeviceTarget:
    """Build a DeviceTarget, failing closed on any missing or invalid field."""
    env = os.environ if env is None else env
    overrides = {} if overrides is None else overrides
    protocol = overrides.get("protocol") or env.get("AUTOPHONE_VPHONE_PROTOCOL", "legacy")
    bundle = overrides.get("core_bundle") or env.get("AUTOPHONE_CORE_BUNDLE")
    values: dict[str, str] = {"protocol": protocol}
    for field, dest, env_names, default in FIELD_SOURCES:
        if field == "cli_dir" and protocol == "upstream_v1" and bundle:
            default = str(Path(bundle).expanduser() / "Contents" / "MacOS")
        resolved = _resolve_field(field, env_names, default, overrides.get(dest), env)
        if resolved == PACKAGE_CACHE_DERIVED:
            # temp_dir 此时已解析完（FIELD_SOURCES 有序），拿它派生 rig 私有缓存。
            resolved = str(Path(values["temp_dir"]) / PACKAGE_CACHE_UNDER_TEMP)
        values[field] = resolved
    for field in PATH_FIELDS:
        raw = Path(values[field]).expanduser()
        # 只有绝对路径才规范化：相对路径 resolve() 会悄悄按 cwd 补全，把
        # DeviceTarget 的「必须绝对路径」这道 fail-closed 闸绕过去。
        values[field] = str(Path(os.path.realpath(raw)) if raw.is_absolute() else raw)
    if bundle:
        values["core_bundle"] = str(Path(bundle).expanduser())
    return DeviceTarget.from_mapping(values)


def runtime_dir_drift(target: DeviceTarget, env: Mapping[str, str]) -> str | None:
    """Fail closed when the emitted runtime dir is not the one health files live in.

    健康守护把 `health.json` / `.pid` / `.log` 写在 `AUTOPHONE_RIG_RUNTIME_DIR`；
    rig agent 经 `process_environment()` 注入给子进程的 `VPHONE_HEALTH_STATE_FILE`
    取自 `DeviceTarget.runtime_dir`。两者不是同一个目录时读数恒 `missing`，
    于是 run 记录里的健康证据失真，`rig.supervisor` 的「VM 掉线等自动恢复」分支
    也永远进不去。`runtime_dir` 字段现在直接跟随该环境变量，这个检查是回归护栏。
    """
    configured = env.get("AUTOPHONE_RIG_RUNTIME_DIR", "").strip()
    if not configured:
        return None
    expected = os.path.realpath(Path(configured).expanduser())
    if os.path.realpath(target.runtime_dir) == expected:
        return None
    return (
        f"DeviceTarget runtime_dir={target.runtime_dir} 与 "
        f"AUTOPHONE_RIG_RUNTIME_DIR={configured} 不是同一个目录："
        "rig agent 子进程读到的 VPHONE_HEALTH_STATE_FILE 与健康守护写的不是同一个文件"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides = {dest: getattr(args, dest) for _, dest, _, _ in FIELD_SOURCES}
    overrides.update(protocol=args.protocol, core_bundle=args.core_bundle)
    try:
        target = target_from_environment(os.environ, overrides)
    except TargetConfigurationError as exc:
        print(f"emit_target: {exc}", file=sys.stderr)
        return 2
    drift = runtime_dir_drift(target, os.environ)
    if drift is not None:
        # fail-closed：写出一份健康路径对不上的 target，坏处是运行期才暴露成
        # 「健康读数恒 missing」，比启动时退 2 难查得多。
        print(f"emit_target: {drift}", file=sys.stderr)
        return 2
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(target.public_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    # 先写临时文件再原子替换：读者（rig agent）不会看到半份文档。
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(out)
    print(str(out))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
