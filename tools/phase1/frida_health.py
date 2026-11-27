#!/usr/bin/env python3
"""Bounded Frida infrastructure health probe (enumeration only).

BL-232 阶段 1（R1）：只枚举进程，判据为 frida-server 可达且列表中有目标进程
（缺省 SpringBoard）。本探针不 attach 任何进程，不加载脚本：attach 并 detach
SpringBoard 后会留下 Frida agent 映射残留（backlog §9.69）。注入能力由 Rig 准备
对被测 App 的契约探针证明。
"""
from __future__ import annotations

import argparse
import json

from autophone.device.frida import probe_enumeration
from autophone.device.frida_process import default_frida_host


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=default_frida_host())
    parser.add_argument("--target", default="SpringBoard")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = probe_enumeration(
        host=args.host,
        process_name=args.target,
        timeout_s=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
